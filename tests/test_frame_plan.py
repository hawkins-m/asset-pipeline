import math

import numpy as np

from asset_pipeline import frame_plan as fp


def _cam(pos, yaw_deg, pitch_deg):
    """A Blender camera matrix at pos looking along yaw (from +Y towards +X... as Blender's -Z)."""
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    f = np.array([math.sin(y) * math.cos(p), math.cos(y) * math.cos(p), math.sin(p)])
    r = np.cross(f, [0, 0, 1.0]); r /= np.linalg.norm(r)
    u = np.cross(r, f)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = r, u, -f, pos
    return m


def test_rays_and_inverse_warp_agree_on_the_ground_plane():
    m = _cam([10.0, -40.0, 1.6], 20, -4)
    size = (320, 180)
    o, d = fp.rays(m, 24, 36, size)
    pts, t = fp.plane_hits(o, d, 0.0)
    assert np.isinf(t[0, 0]) and np.isfinite(t[-1, 160])               # sky row misses, ground row hits
    assert abs(pts[-1, 160, 2]) < 1e-9
    # a frame painted with a water square on the ground: forward hits and inverse warp both find it
    cls = np.zeros(size[::-1], np.uint8)
    inside = np.isfinite(t) & (np.abs(pts[..., 0] - 14) < 3) & (np.abs(pts[..., 1] - (-20)) < 3)
    cls[np.isfinite(t)] = fp.CLASSES.index("paving")
    cls[inside] = fp.CLASSES.index("water")
    grid = fp.plan_inverse(cls, m, 24, 36, 0.0, (0, -40, 30, -5), 0.5, 100)
    w = fp.regions(grid, "water", (0, -40, 30, -5), 0.5, 1)
    assert len(w) == 1 and abs(w[0]["area_m2"] - 36) < 8
    assert np.allclose(w[0]["centre"], [14, -20], atol=1.0)


def test_classes_map_ade_names_and_sam_wins():
    names = {"0": "wall", "1": "building", "2": "sky", "3": "floor", "9": "grass", "21": "water"}
    lab = np.array([[1, 2], [3, 9]])
    sam = {"water": np.array([[False, False], [True, False]])}
    c = fp.classes(lab, names, sam)
    assert [fp.CLASSES[v] for v in c.ravel()] == ["building", "sky", "water", "grass"]
