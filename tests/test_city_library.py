"""The city library layer for `ap export --assets` / `ap ue build`: slot mapping, the mixed-slot
rule, which greybox pieces stay, and transforms relative to the slot. Pure: no project, no Blender."""
import numpy as np

from asset_pipeline import city_library as cl, ue_coords


def _slot(sid, x, y, typology=None, half=20.0, pieces=()):
    m = np.eye(4)
    m[:3, 3] = [x, y, 0.0]
    return {"id": sid, "typology": typology, "matrix": m.tolist(),
            "bbox": [[x - half, y - half, 0.0], [x + half, y + half, 10.0]], "pieces": list(pieces)}


def _piece(x, y, mesh="box#1"):
    m = np.eye(4)
    m[:3, 3] = [x, y, 0.0]
    return {"mesh": mesh, "piece": "p", "matrix_local": m.tolist()}


def test_a_building_maps_to_the_slot_of_its_typology_around_it():
    slots = [_slot("insula-a", 0, 0, "insula"), _slot("town-a", 0, 0, "townhouses"), _slot("insula-b", 30, 0, "insula")]
    assert cl.slot_of_point(slots, "insula", 2, 3) == "insula-a"
    assert cl.slot_of_point(slots, "townhouses", 2, 3) == "town-a"
    assert cl.slot_of_point(slots, "insula", 18, 0) == "insula-b"          # in both boxes: the nearer centre
    assert cl.slot_of_point(slots, "insula", 200, 0) is None


def _p(key, entry, slot, typed=False):
    return {"key": key, "entry": entry, "slot": slot, "typed": typed, "x": 0.0, "y": 0.0, "z": 0.0, "yaw": 0.0,
            "w": 10.0, "d": 10.0, "glb": f"3d/{key}.glb"}


def test_mixed_slots_a_typed_slot_goes_only_when_every_building_has_a_build():
    cands = [_p("a1", "insula", "block-a", True), _p("a2", "insula", "block-a", True),     # 2 of 2 planned
             _p("b1", "insula", "block-b", True),                                       # 1 of 3 planned
             _p("gate", "gate", "kit-1"), _p("c1", "insula-x", "block-c", True), _p("c2", "insula", "block-c", True)]
    ok = lambda e: e != "insula-x"                                                       # noqa: E731
    placed, replaced, held = cl.choose(cands, {"block-a": 2, "block-b": 3, "block-c": 2}, ok)
    assert sorted(p["key"] for p in placed) == ["a1", "a2", "gate"]
    assert replaced == {"block-a": "footprints", "kit-1": "whole"}
    why = {h["key"]: h["why"] for h in held}
    assert why["b1"] == "1 of 3 buildings in the slot have a build"
    assert why["c1"] == why["c2"] == "not approved"                 # one unapproved build holds the whole slot


def test_a_build_with_no_slot_is_held_and_a_kit_slot_takes_one_build():
    placed, replaced, held = cl.choose([_p("x", "e", None, True), _p("k1", "e", "kit"), _p("k2", "f", "kit")], {}, lambda e: True)
    assert [p["key"] for p in placed] == ["k1"] and replaced == {"kit": "whole"}
    assert {h["key"]: h["why"] for h in held} == {"x": "no greybox slot", "k2": "another build stands in this slot"}


def test_only_pieces_on_placed_footprints_leave_a_typed_slot():
    s = _slot("block", 100, 0, "insula", pieces=[_piece(0, 0), _piece(4.5, 0), _piece(15, 0)])
    feet = [{"x": 100.0, "y": 0.0, "w": 8.0, "d": 8.0, "yaw": 0.0}]
    kept = cl.kept_pieces(s, "footprints", feet)                     # 4.5 m off centre: within 4 + 1 m
    assert [p["matrix_local"][0][3] for p in kept] == [15]
    assert cl.kept_pieces(s, "whole", feet) == [] and cl.kept_pieces(s, None, feet) == s["pieces"]
    turned = [{"x": 100.0, "y": 0.0, "w": 2.0, "d": 30.0, "yaw": 90.0}]  # long along world x once turned
    assert len(cl.kept_pieces(s, "footprints", turned)) == 0


def test_builds_are_placed_relative_to_their_slot():
    s = _slot("block", 100, 50, "insula")
    p = _p("a1", "insula", "block", True) | {"x": 110.0, "y": 50.0, "z": 2.0, "yaw": 90.0}
    row = cl.actor_rows([p], {"block": s})["block"][0]
    assert row["asset"] == "lib_a1" and row["entry"] == "insula"
    assert row["relative"]["loc"] == [1000.0, 0.0, 200.0]             # 10 m along x, 2 m up, in cm
    assert row["world"]["loc"] == [11000.0, -5000.0, 200.0]          # Blender +Y is UE -Y
    np.testing.assert_allclose(row["relative"]["rot"], ue_coords.to_ue(cl.world_matrix(p))["rot"], atol=1e-6)


def test_attached_pieces_use_their_own_matrix():
    m = np.eye(4)
    m[:3, 3] = [1, 2, 3]
    m[0, 0] = 2.0
    assert cl.world_matrix({"matrix": m.tolist()})[0, 0] == 2.0
    assert cl.asset_id("insulamlimestone_m153__child-x") == "lib_insulamlimestone_m153__child_x"
