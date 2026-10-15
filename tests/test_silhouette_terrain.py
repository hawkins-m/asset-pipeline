"""Silhouette check (silhouette.py) and terrain analysis helpers (terrain_plan.py)."""
import math

import numpy as np
import pytest

from asset_pipeline import silhouette as sl
from asset_pipeline import terrain_plan as tp

# a camera at the origin, 1.5 m up, looking along +Y (Blender: -Z forward, +Y up)
CAM = np.array([[1.0, 0, 0, 0], [0, 0, -1.0, 0], [0, 1.0, 0, 1.5], [0, 0, 0, 1.0]])
SHOT = {"matrix": CAM.tolist(), "lens_mm": 36.0, "sensor_mm": 36.0, "resolution": [400, 300]}


def _rect(h, w, x0, y0, x1, y1):
    m = np.zeros((h, w), bool)
    m[y0:y1, x0:x1] = True
    return m


def test_score_is_iou_times_aspect_match_and_ignores_occluders():
    f = _rect(100, 200, 20, 20, 180, 80)
    r = _rect(100, 200, 20, 20, 180, 80)
    assert sl.score(f, r)["score"] == 1.0
    tall = _rect(100, 200, 20, 0, 180, 100)                 # same width, taller: aspect off
    s = sl.score(f, tall)
    assert s["aspect_match"] == pytest.approx(60 / 100, abs=0.02) and s["score"] < s["iou"]
    tree = _rect(100, 200, 90, 30, 110, 100)                # a tree in front: the frame's mask has a hole
    s2 = sl.score(f & ~tree, r, ignore=tree & ~(f & ~tree))
    assert s2["iou"] == 1.0


def _project(pts):
    w, h = SHOT["resolution"]
    f = SHOT["lens_mm"] / SHOT["sensor_mm"] * w
    c = (np.asarray(pts, float) - CAM[:3, 3]) @ CAM[:3, :3]
    return w / 2 + f * c[:, 0] / -c[:, 2] - 0.5, h / 2 - f * c[:, 1] / -c[:, 2] - 0.5


def test_measure_reads_length_and_height_on_the_front_plane():
    # a 30 x 12 m front facing the camera 60 m away (pose: front at local -Y, yaw 0 -> facing -Y)
    pose = {"x": 0.0, "y": 70.0, "yaw": 0.0}
    corners = [(-15, 60, 0), (15, 60, 0), (-15, 60, 12), (15, 60, 12)]
    u, v = _project(corners)
    m = np.zeros((300, 400), bool)
    m[int(round(v[2])):int(round(v[0])) + 1, int(round(u[0])):int(round(u[1])) + 1] = True
    r = sl.measure(SHOT, m, pose, -10.0)
    assert r["length_m"] == pytest.approx(30, rel=0.04) and r["height_m"] == pytest.approx(12, rel=0.06)


def test_crest_height_from_angle_and_distance():
    assert tp.crest_z(10.0, 0.0, 5000.0) == pytest.approx(10 + 5000 ** 2 / (2 * tp.EARTH_R))
    assert tp.crest_z(0.0, 10.0, 1000.0) == pytest.approx(1000 * math.tan(math.radians(10)), abs=0.2)


def test_tile_exit():
    assert tp.tile_exit(np.array([0.0, 0.0]), 0.0, 4000) == pytest.approx(4000)
    assert tp.tile_exit(np.array([1000.0, 0.0]), 180.0, 4000) == pytest.approx(5000)


def test_gate_keeps_only_approved(monkeypatch):
    from asset_pipeline import look
    cands = ([{"entry": "a", "key": "1"}, {"entry": "b", "key": "2", "slot": "s2"}], ["s2"], {"a": "ja", "b": "jb"})
    monkeypatch.setattr(look, "_candidates", lambda *a, **k: cands)
    monkeypatch.setattr(look, "approved", lambda store: {"b"})
    p, drop, ent, held = look.placements(None, {}, 600)
    assert [x["entry"] for x in p] == ["b"] and drop == ["s2"] and ent == {"b": "jb"}
    assert [x["entry"] for x in held] == ["a"]


def test_frame_openings_are_the_dark_pixels_inside_the_outline():
    rgb = np.full((50, 80, 3), 210, np.uint8)
    rgb[10:20, 10:20] = 40                    # a dark opening
    rgb[30:40, 60:70] = 170                   # lit stone in shade: not an opening
    fm = _rect(50, 80, 5, 5, 75, 45)
    o = sl.frame_openings(rgb, fm)
    assert o[10:20, 10:20].all() and not o[30:40, 60:70].any() and o.sum() == 100


def test_colonnade_takes_measured_piers_and_infill():
    from asset_pipeline import building_gen as bg
    c = {"bays": 3, "proj": 1.5, "pier": 1.4, "ledge_h": 1.2, "piers": [-10.0, -4.0, 2.0, 8.6], "infill": [2]}
    ps = bg._projecting_colonnade(20.0, 10.0, 7.4, c)
    piers = [p for p in ps if p["kind"] == "box" and p["z0"] == 0.0 and p["x1"] - p["x0"] == pytest.approx(1.4)]
    assert sorted(round(p["x0"], 2) for p in piers) == [-10.0, -4.0, 2.0, 8.6]
    infill = [p for p in ps if p["kind"] == "box" and p["z0"] == 0.0 and p not in piers]
    assert len(infill) == 1 and infill[0]["x0"] == pytest.approx(3.4) and infill[0]["x1"] == pytest.approx(8.6)


def test_first_sky_run_skips_reflections_and_keeps_arches():
    from asset_pipeline.terrain_build import first_sky_run
    sky = np.zeros((10, 3), bool)
    sky[0:4, 0] = True; sky[8:10, 0] = True        # sky, then a building, then a reflection in water
    sky[3:6, 1] = True                             # sky seen through an arch (building above it)
    start, end = first_sky_run(sky)
    assert list(start) == [0, 3, 0] and list(end) == [4, 6, 0]


def test_spread_eases_a_cap_along_the_crest_and_never_exceeds_it():
    from asset_pipeline.terrain_build import _spread
    req = np.ones(200); req[100] = 0.4
    f = _spread(req, 21, 5.0)
    assert f[100] <= 0.4 + 1e-9 and (f <= req + 1e-9).all()
    assert f[90] < 0.75 and f[60] > 0.95                   # eased over the window, back to full beyond it
    assert np.abs(np.diff(f)).max() < 0.15                  # no step: no cliff along the crest
