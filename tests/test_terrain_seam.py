"""The outer ring meets the far field without a gap, hangs a skirt under its inner edge, and
sinks under the sea before its outer edge."""
import importlib.util

import numpy as np
import pytest

from asset_pipeline import config, terrain_build as tb

spec = importlib.util.spec_from_file_location("heightgrid", config.REPO_ROOT / "scripts/heightgrid.py")
heightgrid = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heightgrid)

SEA = 0.0


def far_field(n=601, cell=40.0):
    x0 = -cell * (n - 1) / 2
    X, Y = np.meshgrid(x0 + np.arange(n) * cell, x0 + np.arange(n) * cell)
    return 40.0 + 300.0 * np.exp(-((X - 9000) ** 2 + (Y + 4000) ** 2) / 2e6), x0, cell, X, Y   # a hill near the edge


@pytest.fixture(scope="module")
def ring():
    H, x0, cell, X, Y = far_field()
    ox, O = tb.outer_ring(H, x0, cell, lambda X, Y: np.full(X.shape, 12.0), SEA)
    return H, x0, cell, ox, O


def test_outer_grid_shares_the_far_border(ring):
    H, x0, cell, ox, O = ring
    oc = 400.0
    g = ox + np.arange(O.shape[0]) * oc
    OX, OY = np.meshgrid(g, g)
    border = np.isfinite(O) & (np.maximum(np.abs(OX), np.abs(OY)) == 12000.0)
    assert border.sum() == 4 * 60                                  # every 400 m along the 24 km square
    fx, fy = ((OX[border] - x0) / cell).astype(int), ((OY[border] - x0) / cell).astype(int)
    assert np.allclose(O[border], H[fy, fx], atol=1e-6)             # same heights on the shared line
    verts, quads = heightgrid.grid_mesh(O, ox, ox, oc)
    inner = np.abs(verts[quads][:, :, :2]).max(axis=2).min(axis=1)  # each quad's nearest corner, Chebyshev
    assert inner.min() == 12000.0                                    # quads start at the border: no gap
    assert len(quads) == 160 ** 2 - 60 ** 2                          # and cover the whole annulus


def test_outer_ring_sinks_under_the_sea_before_its_edge(ring):
    _, _, _, ox, O = ring
    g = ox + np.arange(O.shape[0]) * 400.0
    R = np.hypot(*np.meshgrid(g, g))
    assert np.all(O[R >= tb.OUTER_TAPER_M[1]] == pytest.approx(SEA - tb.OUTER_SINK_M))
    edge = np.r_[O[0], O[-1], O[:, 0], O[:, -1]]
    assert edge.max() < SEA                                          # the mesh ends under water
    near = np.isfinite(O) & (R < tb.OUTER_TAPER_M[0])
    assert O[near].min() >= SEA                                      # land as before inside the taper


def test_misaligned_outer_cell_is_refused():
    H, x0, cell, _, _ = far_field()
    with pytest.raises(ValueError, match="doesn't divide"):
        tb.outer_ring(H, x0, cell, lambda X, Y: np.zeros(X.shape), SEA, outer_cell=320.0)


def test_skirt_hangs_from_every_boundary_edge_facing_out():
    h = np.full((6, 6), 10.0)
    h[2:4, 2:4] = np.nan                                             # a hole: 3x3 quads missing in the middle
    v0, q0 = heightgrid.grid_mesh(h, 0.0, 0.0, 1.0)
    v, q = heightgrid.grid_mesh(h, 0.0, 0.0, 1.0, skirt=5.0)
    assert len(q0) == 25 - 9 and np.array_equal(q[:len(q0)], q0)
    sk = q[len(q0):]
    assert len(sk) == 4 * 5 + 4 * 3                                  # outer rim 20 edges, hole rim 12
    k = (len(v) - len(v0)) // 2
    assert np.allclose(v[len(v0):len(v0) + k, 2], 10.0)              # top copies at the edge's height
    assert np.allclose(v[len(v0) + k:, 2], 5.0)                      # bottoms 5 m below the 10 m edge
    assert not set(sk.ravel()) & set(q0.ravel())                     # own vertices: the surface's normals stay clean
    for quad in sk:
        p = v[quad]
        n = np.cross(p[1] - p[0], p[2] - p[1])
        c = p[:, :2].mean(axis=0)
        out = c - np.array([2.5, 2.5])                               # the grid's centre is the hole's
        hole_rim = np.abs(c - 2.5).max() <= 1.5 + 1e-9                # the hole spans 1..4
        assert np.dot(n[:2], -out if hole_rim else out) > 0          # facing into the hole / out of the grid


def test_no_skirt_without_boundary_or_when_off():
    h = np.zeros((3, 3))
    assert len(heightgrid.grid_mesh(h, 0, 0, 1)[1]) == 4
    assert len(heightgrid.grid_mesh(h, 0, 0, 1, skirt=2.0)[1]) == 4 + 8


def test_core_edge_blends_into_the_far_field_only_on_land():
    n, cell, sea = 101, 40.0, 0.0
    core = np.full((n, n), -40.0)                                    # seabed in the core
    far = np.full((n, n), 0.3)                                       # the far field's water: a 0.3 m skin
    far[:, :20] = 120.0                                              # a far hill on the west border
    out = tb.match_edge(core, far, sea, cell)
    assert np.allclose(out[:, 0][far[:, 0] > 1], 120.0)              # the border matches the far land
    assert np.allclose(out[:, 50], -40.0)                            # open water and the interior untouched
    assert np.allclose(out[50, 10:], -40.0) and (out[50, :10] > -40.0).all()   # the band is 400 m = 10 cells
    assert (out[10:-10, 10:-10] == -40.0).all()


def test_district_ties_are_deterministic(monkeypatch):
    from asset_pipeline import city
    near = {"x": "forum"}
    monkeypatch.setattr(city, "nearest_node", lambda c, x, y: near["x"])
    assert city.majority_district(None, [], 0, 0) is None
    assert city.majority_district(None, ["harbour", "forum", "forum"], 0, 0) == "forum"
    assert city.majority_district(None, ["harbour", "forum"], 0, 0) == "forum"        # tie: the slot's own district
    near["x"] = "harbour"
    assert city.majority_district(None, ["forum", "harbour"], 0, 0) == "harbour"
    near["x"] = "academy"
    assert city.majority_district(None, ["harbour", "forum"], 0, 0) == "forum"        # neither: first by name
