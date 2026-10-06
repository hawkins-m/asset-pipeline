"""Hero-tier prototype: orbit analysis on synthetic frames (no Wan)."""
import json
import math

import numpy as np
from PIL import Image

from asset_pipeline.stages import s5_hero


def _frames(tmp_path, n=40, shift_from=None):
    """An asymmetric two-colour block 'turning': its width follows |cos|, its colours
    swap sides at the back, and it comes back to the start at frame n-1."""
    for i in range(n):
        ang = 2 * math.pi * i / (n - 1)
        w = int(20 + 60 * abs(math.cos(ang)))
        px = np.full((200, 200, 3), 250, np.uint8)
        x0 = 100 - w // 2 + (30 if shift_from is not None and i >= shift_from else 0)
        left, right = ((200, 40, 40), (40, 40, 200)) if math.cos(ang) >= 0 else ((40, 40, 200), (200, 40, 40))
        px[60:160, x0:x0 + w // 2] = left
        px[60:160, x0 + w // 2:x0 + w] = right
        px[60:70, x0:x0 + 8] = (20, 160, 20)  # an asymmetric mark
        Image.fromarray(px).save(tmp_path / f"frame_{i:03d}.png")
    return tmp_path


def test_closed_turn_without_drift(tmp_path):
    r = s5_hero.analyse(_frames(tmp_path))
    assert r["drift_frac"] == 0 and r["closed"] and r["closure_frame"] == 39
    assert r["picks"] == {0: 0, 90: 10, 180: 20, 270: 29}
    assert sorted(p.name for p in tmp_path.glob("pick_*.png")) == [
        "pick_000.png", "pick_090.png", "pick_180.png", "pick_270.png"]
    assert (tmp_path / "contact.png").is_file()
    assert json.loads((tmp_path / "analysis.json").read_text())["closed"]


def test_sideways_drift_is_caught(tmp_path):
    r = s5_hero.analyse(_frames(tmp_path, shift_from=20))
    assert r["drift_frames"] == list(range(20, 40)) and not r["usable"]


def test_off_white_background_is_not_object(tmp_path):
    im = Image.new("RGB", (100, 100), (244, 243, 245))      # Wan's off-white
    im.paste((90, 60, 30), (30, 30, 70, 70))
    im.putpixel((99, 0), (120, 120, 120))                    # a VAE edge speck
    m = s5_hero._silhouette(im)
    assert s5_hero._box(m) == (30, 30, 70, 70)


# --- render matching: the tracker itself, on data where frames and renders agree --------

FACES = {"front": (200, 40, 40), "left": (40, 160, 40), "back": (40, 40, 200), "right": (210, 190, 40)}


def _box_view(yaw_deg, size=160, w=80, d=40, h=60, bg=None):
    """A w x d box seen from azimuth yaw: front / left / back / right faces in different
    colours, widths |w cos| and |d sin| (same convention as blender_turntable.py)."""
    a = math.radians(yaw_deg)
    fw, sw = abs(w * math.cos(a)), abs(d * math.sin(a))
    im = Image.new("RGBA" if bg is None else "RGB", (size, size), bg or (0, 0, 0, 0))
    px = np.array(im)
    x = (size - (fw + sw)) / 2
    y0 = (size - h) // 2
    face = FACES["front"] if math.cos(a) >= 0 else FACES["back"]
    side = FACES["left"] if math.sin(a) >= 0 else FACES["right"]
    first, second = ((face, fw), (side, sw)) if math.sin(a) >= 0 else ((side, sw), (face, fw))
    for colour, width in (first, second):
        x1 = x + width
        if int(x1) > int(x):
            px[y0:y0 + h, int(x):int(x1)] = (*colour, 255) if bg is None else colour
        x = x1
    return Image.fromarray(px)


def test_match_angles_recovers_a_full_turn(tmp_path):
    tt, orbit = tmp_path / "tt", tmp_path / "orbit"
    tt.mkdir(), orbit.mkdir()
    renders = []
    for az in range(0, 360, 5):
        _box_view(az).save(tt / f"e10_a{az:03d}.png")
        renders.append({"file": f"e10_a{az:03d}.png", "azimuth": az, "elevation": 10})
    (tt / "renders.json").write_text(json.dumps({"step": 5, "renders": renders}))
    # Ease-in / ease-out full turn over 61 frames, off-white background like Wan's.
    true = [180 * (1 - math.cos(math.pi * i / 60)) for i in range(61)]
    for i, yaw in enumerate(true):
        _box_view(yaw, bg=(248, 247, 246)).save(orbit / f"frame_{i:03d}.png")
    r = s5_hero.match_angles(orbit, tt)
    err = [abs((a - t + 180) % 360 - 180) for a, t in zip(r["angles"], true)]
    assert max(err) <= 10, err
    assert abs(r["rotation_deg"] - 360) <= 10
    assert set(r["picks"]) == {"front", "left", "back", "right"}
    assert (orbit / "view_left.png").is_file()


def test_multiview_mesh_drops_unused_views(tmp_path):
    from asset_pipeline.comfy.client import OutputImage
    img = tmp_path / "f.png"
    Image.new("RGB", (8, 8), "white").save(img)

    class Client:
        def upload_image(self, p):
            return p.name

        def run_files(self, graph, outputs):
            self.graph = graph
            return [OutputImage("25", "mesh_00001_.glb", b"glTF")]
    c = Client()
    out = s5_hero.multiview_mesh({"front": img, "back": img}, tmp_path / "m.glb", seed=3, client=c)
    assert out.read_bytes() == b"glTF"
    assert set(c.graph["20"]["inputs"]) == {"front", "back"}
    assert "12" not in c.graph and "16" not in c.graph and c.graph["10"]["inputs"]["image"] == "f.png"
    assert c.graph["22"]["inputs"]["seed"] == 3
