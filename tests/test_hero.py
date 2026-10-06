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
