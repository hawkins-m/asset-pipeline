"""Look scenes (look.py): frame reads (tints, ridge), ground maps, placements' view test, plan overlap."""
import math

import numpy as np
import pytest

from asset_pipeline import frame_plan, look

CAM = np.array([[1.0, 0, 0, 0], [0, 0, -1.0, 0], [0, 1.0, 0, 10.0], [0, 0, 0, 1.0]])   # at z 10 looking along +Y
SHOT = {"matrix": CAM.tolist(), "lens_mm": 36.0, "sensor_mm": 36.0, "resolution": [200, 100]}


def test_ridge_follows_the_hill_class_and_bridges_hidden_columns():
    sky, hill, bld = (frame_plan.CLASSES.index(c) for c in ("sky", "hill", "building"))
    cls = np.full((100, 200), sky, np.uint8)
    cls[40:, :] = hill                                     # a flat crest 10 rows above the horizon
    cls[20:, 80:120] = bld                                  # a building hides the middle
    cr = look.ridge_from_frame(cls, CAM, 36.0, 36.0, 1000.0, 10.0, step=10)
    zs = [z for _, z in cr]
    assert len(cr) == 20 and max(zs) - min(zs) < 15         # the hidden stretch is interpolated, not the roof
    assert np.mean(zs) == pytest.approx(10 + 1000 * 9.5 / 200, abs=6)
    bearings = [b for b, _ in cr]
    assert bearings[0] > 90 > bearings[-1]                 # left of the view is +X turned towards -X... CCW


def test_frame_tint_uses_lit_pixels_and_scales_to_albedo():
    pav = frame_plan.CLASSES.index("paving")
    cls = np.full((40, 40), pav, np.uint8)
    img = np.zeros((40, 40, 3))
    img[:20] = (200, 190, 180)                             # sunlit
    img[20:] = (90, 100, 120)                              # in shadow: bluish
    t = look.frame_tint(img, cls, "paving", albedo=0.5)
    assert t[0] > t[1] > t[2]                              # the sunlit hue, not the shadow's
    assert float(np.dot(t, look.LUMA)) == pytest.approx(0.5, abs=0.01)


def test_paving_maps_tile_seamlessly(tmp_path):
    from PIL import Image
    m = look.paving_maps(look.DEFAULTS["paving"], tmp_path)
    assert m["tile_m"] == [2.4, 2.4]
    a = np.asarray(Image.open(m["color"]).convert("L"), float)
    assert abs(a[:, 0] - a[:, -1]).mean() < 25 and abs(a[0] - a[-1]).mean() < 25
    assert a.min() < 0.7 * np.median(a)                    # dark joints


def test_in_view():
    assert look.in_view(SHOT, np.array([[0.0, 50.0, 10.0]]))
    assert not look.in_view(SHOT, np.array([[0.0, -50.0, 10.0]]))      # behind
    assert not look.in_view(SHOT, np.array([[500.0, 50.0, 10.0]]))     # far off to the side


def test_overlap_iou_counts_only_what_the_frame_sees():
    w, g, p = (frame_plan.CLASSES.index(c) for c in ("water", "grass", "paving"))
    fr = np.zeros((10, 10), np.uint8)
    fr[:, :5] = g
    fr[:, 5:8] = p                                          # columns 8-9: the frame can't see them
    gb = np.full((10, 10), p, np.uint8)
    gb[:, :4] = g
    gb[:, 8:] = w
    r = frame_plan.overlap(fr, gb, 1.0)
    assert r["iou"]["grass"] == pytest.approx(0.8) and r["iou"]["paving"] == pytest.approx(30 / 40)
    assert r["iou"]["water"] is None and r["greybox_m2"]["water"] == 0


def test_decode_reads_linear_greybox_colours():
    lin = np.array(frame_plan.COLOURS["grass"], float) / 255
    srgb = np.where(lin <= 0.0031308, lin * 12.92, 1.055 * lin ** (1 / 2.4) - 0.055) * 255
    img = np.tile(np.round(srgb).astype(np.uint8), (2, 2, 1))
    assert (frame_plan.decode(img, linear=True) == frame_plan.CLASSES.index("grass")).all()
