"""World mode phase 1: layout -> greybox spec, terrain, shot pass decoding; real Blender
build / extract / passes on a small layout."""
import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from asset_pipeline import greybox
from asset_pipeline.project import ProjectStore
from asset_pipeline.schema import (Kit, Plot, Radial, Ring, RingRow, ShotSpec, SiteLayout,
                                   TerrainSpec)
from asset_pipeline.stages import sw_shots, sw_site

REAL_BLENDER = Path("/snap/bin/blender")


def small_layout(**kw) -> SiteLayout:
    return SiteLayout(
        terrain=TerrainSpec(extent_m=1000, resolution=129, city_radius_m=200, city_z=10, shore_m=320,
                            hill_height_m=40),
        kits=[Kit(id="k", module_m=4, storey_m=6, column_d_m=0.9, ring_radii=[20, 80])],
        rings=[Ring(id="avenue", radius=50, width=10)],
        radials=[Radial(id="r0", angle=0, width=12, r_from=30, r_to=180)],
        plots=[Plot(id="rotunda", type="rotunda", center=(0, 0), size=(30, 30, 30), kit="k"),
               Plot(id="stoa-a", type="stoa", arc=(80, 30, 150), kit="k")],
        rows=[RingRow(id="villa", radius=120, count=4, type="villa", size=(30, 30, 8), kit="k")],
        shots=[ShotSpec(id="look", pos=(0, -70, 11.7), look_at=(0, 0, 18), resolution=(320, 180))],
        **kw)


def spec_for(layout):
    return greybox.build_spec(layout, greybox.make_terrain(layout.terrain))


# --- layout -> spec ---------------------------------------------------------------------------

def test_starter_layout_builds_a_spec():
    layout = sw_site.starter_layout()
    spec = spec_for(SiteLayout.model_validate_json(layout.model_dump_json()))  # survives a JSON round-trip
    counts = greybox.piece_counts(spec)
    assert counts["colonnade:entablature"] > 20 and len(layout.shots) == 2
    assert {s["type"] for s in spec["slots"]} >= {"rotunda", "stoa", "block", "villa", "radial", "garden"}


def test_ring_rows_skip_radials_and_face_the_centre():
    layout = small_layout()
    plots = [p for p in greybox.expand_rows(layout) if p.id.startswith("villa")]
    assert [p.id for p in plots] == ["villa-01", "villa-02", "villa-03"]  # villa-00 sits on radial r0
    for p in plots:
        a = math.degrees(math.atan2(p.center[1], p.center[0]))
        front = greybox._rot(0, -1, p.rot)          # local -Y in world
        to_centre = (-math.cos(math.radians(a)), -math.sin(math.radians(a)))
        assert front[0] * to_centre[0] + front[1] * to_centre[1] == pytest.approx(1, abs=1e-6)


def test_kit_pieces_repeat_exactly():
    """Every kit piece type is one primitive (one shared mesh -> one instanced mesh later)."""
    spec = spec_for(small_layout())
    prims: dict[str, set] = {}
    for s in spec["slots"]:
        for p in s["pieces"]:
            if p["piece"].split(":")[0] in ("k",) or p["piece"].startswith("k-d"):
                prims.setdefault(p["piece"], set()).add(json.dumps(p["prim"], sort_keys=True))
    assert prims and all(len(v) == 1 for v in prims.values()), {k: len(v) for k, v in prims.items()}
    assert any(k.startswith("k-d") and "column-shaft" in k for k in prims)   # the rotunda's own order
    assert "k:stoa-entablature-r80" in prims


def test_pieces_are_stored_relative_to_their_slot():
    spec = spec_for(small_layout())
    rot = next(s for s in spec["slots"] if s["id"] == "rotunda")
    shafts = [p for p in rot["pieces"] if "column-shaft" in p["piece"]]
    radii = {round(math.hypot(p["loc"][0], p["loc"][1]), 2) for p in shafts}
    assert len(radii) == 1 and len(shafts) == 16          # paired columns at the octagon's 8 corners
    assert 15 < radii.pop() < 20                          # just outside the 15 m corner radius
    stoa = next(s for s in spec["slots"] if s["id"] == "stoa-a")
    # back to world: rotate by the slot's rot_z, add its loc -> columns on the r=80 arc
    for p in [p for p in stoa["pieces"] if "column-shaft" in p["piece"]]:
        wx, wy = greybox._rot(p["loc"][0], p["loc"][1], stoa["rot_z"])
        assert math.hypot(wx + stoa["loc"][0], wy + stoa["loc"][1]) == pytest.approx(80, abs=1e-3)


def test_duplicate_slot_ids_and_unknown_kits_are_rejected():
    layout = small_layout()
    layout.plots.append(Plot(id="rotunda", type="temple", center=(0, 300)))
    with pytest.raises(ValueError, match="duplicate slot id"):
        spec_for(layout)
    layout = small_layout()
    layout.plots[0].kit = "nope"
    with pytest.raises(ValueError, match="unknown kit"):
        spec_for(layout)


def test_terrain_plateau_sea_and_png16_roundtrip():
    t = small_layout().terrain
    h = greybox.make_terrain(t)
    hs = greybox.Heights(h, t.extent_m)
    assert hs(0, 0) == pytest.approx(t.city_z) and hs(150, 50) == pytest.approx(t.city_z)
    assert hs(0, -480) < 0 < hs(0, 450)               # sea to the south (sea_dir -90), land north
    back = greybox.from_png16(greybox.to_png16(h, t.z_range), t.z_range)
    assert np.abs(back - h).max() < (t.z_range[1] - t.z_range[0]) / 65535


# --- pass decoding ------------------------------------------------------------------------------

def test_id_colours_are_unique_and_decode_exactly():
    ids = [f"s{i}" for i in range(5000)]
    colors = sw_shots.colors_for(ids)
    assert len({tuple(c) for c in colors.values()}) == len(colors) and [0, 0, 0] not in colors.values()
    rgb = np.zeros((2, 3, 3), np.uint8)
    rgb[0, 1] = colors["s7"]
    rgb[1, 2] = colors["s4999"]
    rgb[1, 0] = [1, 2, 3]                              # a colour nobody has
    idx, names, unknown = sw_shots.decode_ids(rgb, colors)
    assert names[idx[0, 1]] == "s7" and names[idx[1, 2]] == "s4999"
    assert idx[0, 0] == -1 and unknown == 1


def test_depth_control_is_inverse_depth_near_white_sky_black():
    z = np.array([[5.0, 10.0, 100.0, 0.0]])
    hit = z > 0
    d = sw_shots.depth_control(z, hit)
    assert d[0, 0] > d[0, 1] > d[0, 2] and d[0, 3] == 0



def _view_from_above(h=96, w=160):
    """A ground plane seen from above at an angle (far at the top; a plane's inverse depth
    is linear in the image), with 8 m boxes on it and sky in the top rows: the depth ramp
    dwarfs the boxes."""
    z = 1 / np.linspace(1 / 2000, 1 / 300, h)[:, None] * np.ones((1, w))
    boxes = np.zeros((h, w), bool)
    for y0, x0 in ((30, 20), (50, 70), (70, 120)):
        boxes[y0:y0 + 10, x0:x0 + 16] = True
    z[boxes] -= 8.0
    hit = np.ones((h, w), bool)
    hit[:8] = False
    z[:8] = 0
    return z, hit, boxes


def test_relief_depth_brings_out_buildings_on_a_ramp_and_keeps_near_light():
    z, hit, boxes = _view_from_above()
    ring = np.zeros_like(boxes)          # the ground beside each box, in the same rows
    ys, xs = np.nonzero(boxes)
    for y, x in zip(ys, xs):
        ring[y, max(x - 6, 0):x + 7] = True
    ring &= ~boxes & hit
    plain, relief = sw_shots.depth_control(z, hit), sw_shots.depth_relief(z, hit)
    contrast = lambda img: img[boxes].mean() - img[ring].mean()   # noqa: E731
    assert contrast(relief) > 4 * contrast(plain) > 0
    assert relief[~hit].max() == 0 and 0 <= relief.min() and relief.max() <= 1
    assert relief[-10:][hit[-10:]].mean() > relief[10:20][hit[10:20]].mean()   # near still lighter


def test_relief_depth_image_is_made_from_the_raw_pass_and_follows_it(tmp_path, monkeypatch):
    z, hit, _ = _view_from_above()
    d = tmp_path / "shots" / "a"
    d.mkdir(parents=True)
    Image.fromarray((z / sw_shots.DEPTH_SCALE * 65535).round().astype(np.uint16)).save(d / "depth_raw.png")
    monkeypatch.setattr(sw_shots, "shot_dir", lambda store, shot: tmp_path / "shots" / shot)
    assert sw_shots.depth_image(None, "a") == d / "depth.png"
    p = sw_shots.depth_image(None, "a", "relief")
    first = np.array(Image.open(p))
    assert p.name == "depth_relief.png" and first[0].max() == 0 and first.std() > 10
    z2 = z.copy()
    z2[40:60, 40:60] -= 50            # the passes were re-rendered: a new building
    Image.fromarray((z2 / sw_shots.DEPTH_SCALE * 65535).round().astype(np.uint16)).save(d / "depth_raw.png")
    import os
    os.utime(p, (1, 1))
    assert not np.array_equal(np.array(Image.open(sw_shots.depth_image(None, "a", "relief"))), first)
    with pytest.raises(ValueError):
        sw_shots.depth_image(None, "a", "fancy")

def test_edges_mark_slot_changes_and_creases_only():
    idx = np.zeros((4, 6), int)
    idx[:, 3:] = 1
    n = np.zeros((4, 6, 3))
    n[..., 2] = 1
    z = np.full((4, 6), 10.0)
    e = sw_shots.edges(idx, n, z, np.ones((4, 6), bool))
    assert e[:, 2].all() and e.sum() == 4             # one column at the slot boundary
    n[2:, :, :] = [0, 1, 0]                            # a 90 degree crease between rows 1 and 2
    e = sw_shots.edges(np.zeros((4, 6), int), n, z, np.ones((4, 6), bool))
    assert e[1].all() and e.sum() == 6


# --- real Blender -----------------------------------------------------------------------------

@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("w")
    path = tmp_path / "layout.json"
    path.write_text(small_layout().model_dump_json())
    sw_site.init(store, path)
    return store


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_build_extract_and_hand_edit_protection(site):
    assert site.load().mode == "world"
    spec = spec_for(sw_site.load_layout(site))
    data = sw_site.build(site)
    assert len(data["slots"]) == len(spec["slots"]) + 2          # + terrain, sea
    got = sw_site.summary(site)["pieces"]
    for piece, n in greybox.piece_counts(spec).items():
        assert got[piece] == n
    assert [s["id"] for s in data["shots"]] == ["look"] and not data["untagged"]
    rot = next(s for s in data["slots"] if s["id"] == "rotunda")
    assert rot["category"] == "building" and rot["kit"] == "k"
    # shared meshes: every shaft of one height uses the same mesh datablock
    shafts = {p["mesh"] for s in data["slots"] for p in s["pieces"] if p["piece"] == "k:column-shaft-h6"}
    assert len(shafts) == 1
    assert (site.root / "site/terrain.png").is_file()
    assert np.array(Image.open(site.root / "site/terrain.png")).dtype == np.uint16

    assert not sw_site.edited(site)
    with sw_site.blend_path(site).open("ab") as f:                 # stand-in for a save in Blender
        f.write(b"\0")
    assert sw_site.edited(site) and sw_site.stale(site)
    with pytest.raises(sw_site.SiteEdited):
        sw_site.build(site)
    sw_site.build(site, force=True)
    assert (site.root / "site/greybox.prev.blend").is_file() and not sw_site.edited(site)


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_add_shot_and_passes_are_exact(site):
    sw_site.build(site)
    z0 = sw_site.load_layout(site).terrain.city_z
    # straight down from 100 m above the ring avenue: depth at the centre must read ~100 m
    # minus the avenue's lift and thickness
    sw_site.add_shot(site, ShotSpec(id="down", pos=(0.5, -50, z0 + 100), look_at=(0.5, -49.999, z0),
                                    resolution=(160, 90), lens_mm=50))
    assert not sw_site.edited(site)                                # pipeline writes aren't hand edits
    assert any(s.id == "down" for s in sw_site.load_layout(site).shots)
    res = sw_shots.render(site)
    assert set(res) == {"look", "down"}
    for r in res.values():
        assert r["stats"]["unknown_pixels"] == 0 and r["stats"]["pass_mismatch"] == 0
        assert not r["warnings"], r["warnings"]
    raw = np.array(Image.open(sw_shots.shot_dir(site, "down") / "depth_raw.png")).astype(float)
    z = raw[45, 80] / 65535 * sw_shots.DEPTH_SCALE
    assert z == pytest.approx(100 - greybox.PAVING_LIFT - 0.1, abs=0.1)
    ids = json.loads((sw_shots.shot_dir(site, "look") / "ids.json").read_text())
    assert "rotunda" in ids["slots"] and ids["slots"]["rotunda"]["frac"] > 0.05
    for f in ("depth.png", "canny.png", "preview.png", "normal.png", "meta.json"):
        assert (sw_shots.shot_dir(site, "look") / f).is_file()
    assert [s["id"] for s in sw_shots.status(site)] == ["down", "look"]


@pytest.mark.skipif(not Path("/snap/bin/blender").exists(), reason="needs Blender")
def test_arched_walls_have_open_arches_in_blender(tmp_path):
    """A notched outline triangulated over its openings once (solid walls in every pass):
    the front faces must cover the wall minus its openings."""
    import subprocess
    script = tmp_path / "check.py"
    script.write_text(
        "import sys, math\n"
        "sys.argv = ['x', '--']\n"
        f"exec(open({str(Path(__file__).parents[1] / 'scripts/blender_greybox.py')!r}).read().split('# --- build')[0])\n"
        "for prim in ({'kind': 'arcade', 'n': 3, 'bay': 4.5, 'span': 3.24, 'h': 4.4, 'd': 0.8, 'spring': 2.18},\n"
        "             {'kind': 'archwall', 'w': 15.0, 'd': 3.0, 'h': 22.0, 'span': 12.0, 'spring': 14.0}):\n"
        "    me = make_mesh(prim, 't')\n"
        "    front = sum(p.area for p in me.polygons if p.normal.y < -0.9)\n"
        "    W = prim.get('w') or prim['n'] * prim['bay']\n"
        "    r, n = prim['span'] / 2, prim.get('n', 1)\n"
        "    print('CHECK', front / (W * prim['h'] - n * (2 * r * prim['spring'] + math.pi * r * r / 2)))\n")
    out = subprocess.run(["/snap/bin/blender", "-b", "--factory-startup", "--python", str(script)],
                         capture_output=True, text=True, timeout=300).stdout
    ratios = [float(x.split()[1]) for x in out.splitlines() if x.startswith("CHECK")]
    assert len(ratios) == 2 and all(0.98 < r < 1.03 for r in ratios), out[-2000:]
