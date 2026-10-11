import math
from types import SimpleNamespace

from asset_pipeline import greybox, library_instances
from asset_pipeline.city_types import Part
from asset_pipeline.schema import Plaza


class _SB:
    def __init__(self):
        self.pieces = []

    def add_local(self, piece, prim, x, y, z, rot=0.0):
        self.pieces.append((piece, prim, x, y, z, rot))


def test_basins_plaza_has_two_basins_either_side_of_the_walk_and_a_plinth_on_the_right():
    sb = _SB()
    greybox.basins_pieces(sb, Plaza(id="p", center=(0, 0), radius=30, type="basins", rot=90.0))
    water = [p for p in sb.pieces if p[0] == "pool-water"]
    assert len(water) == 2 and all(p[1]["w"] == 30.0 and p[1]["d"] == 12.0 for p in water)
    # walk along +Y (rot 90): basins either side in x, 10 m apart at their inner edges
    xs = sorted(round(p[2], 3) for p in water)
    assert xs == [-11.0, 11.0]
    plinth = [p for p in sb.pieces if p[0] == "plinth"][0]
    assert round(plinth[2], 3) == 11.0                     # looking along +Y, right is +X
    lawns = [p for p in sb.pieces if p[0] == "lawn"]
    gravel = [p for p in sb.pieces if p[0] == "gravel"]
    assert len(lawns) == 2 and len(gravel) == 2
    assert all(abs(p[1]["d"] - 1.8) < 1e-6 for p in gravel)
    assert {math.copysign(1, p[2]) * math.copysign(1, p[3]) for p in lawns} == {-1.0}   # opposite quarters


def test_instance_footprint_is_in_the_buildings_own_frame():
    b = SimpleNamespace(x=100.0, y=50.0, rot=30.0, parts=[Part("box", 100.0, 50.0, 12.0, 40.0, 14.0, 20.0, 30.0)])
    x, y, z, w, d = library_instances.footprint(b)
    assert abs(x - 100) < 1e-6 and abs(y - 50) < 1e-6 and z == 12.0
    assert abs(w - 40) < 1e-6 and abs(d - 14) < 1e-6
