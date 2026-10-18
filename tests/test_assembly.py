"""Attached pieces (assembly.py): children with anchors, offsets, rotation and scale, placed with
the parent in the assembly GLB (Asset review, review renders) and Unreal look scenes."""
import json
import math
import struct
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from asset_pipeline import assembly, filestore, kit_proc, versions
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.stages import sw_library, sw_site
from asset_pipeline.ui import app as ui_app

REAL_BLENDER = Path("/snap/bin/blender")


def glb_box(path: Path, lo, hi):
    """A GLB of one box-shaped triangle soup with these bounds, given in Blender's frame (Z up)."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    corners = np.array(np.meshgrid(*zip(lo, hi))).T.reshape(-1, 3)
    pts = np.stack([corners[:, 0], corners[:, 2], -corners[:, 1]], 1).astype(np.float32)   # -> glTF Y up
    idx = np.array([0, 1, 2, 2, 3, 0, 4, 5, 6, 6, 7, 4, 0, 1, 5, 5, 4, 0], np.uint16)
    vb, ib = pts.tobytes(), idx.tobytes()
    ib += b"\0" * (-len(ib) % 4)
    gl = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
          "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
          "buffers": [{"byteLength": len(vb) + len(ib)}],
          "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(vb), "target": 34962},
                          {"buffer": 0, "byteOffset": len(vb), "byteLength": len(idx) * 2, "target": 34963}],
          "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(pts), "type": "VEC3",
                         "min": pts.min(0).tolist(), "max": pts.max(0).tolist()},
                        {"bufferView": 1, "componentType": 5123, "count": len(idx), "type": "SCALAR"}]}
    js = json.dumps(gl).encode()
    js += b" " * (-len(js) % 4)
    body = vb + ib
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(js) + 8 + len(body))
                     + struct.pack("<I4s", len(js), b"JSON") + js + struct.pack("<I4s", len(body), b"BIN\0") + body)
    return path


def _entry(eid, kind="building", size=(20.0, 10.0, 8.0), route="procedural", slots=()):
    return {"id": eid, "tier": "A", "kind": kind, "typology": eid, "part": eid, "material": None, "instances": len(slots),
            "districts": {}, "district": "core", "mood": [], "flags": [], "best": None, "share": 0.0,
            "description_auto": eid, "description": None, "size_m": list(size), "route_auto": route, "slot_ids": list(slots)}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("p")
    write_json(sw_site.site_dir(s) / "library.json", {
        "min_share": 0.005, "sources": {}, "mood": {}, "candidates": [], "checked": True, "surfaces": {},
        "entries": [], "unseen": [_entry("portico", slots=["s1"]), _entry("gate", "structure", (10.0, 6.0, 12.0), "trellis"),
                                  _entry("statue", "prop", (2.0, 2.0, 5.0), "trellis")]})
    return s


def _procedural(store, eid, lo, hi):
    glb = glb_box(kit_proc.out_dir(store, eid) / "procedural.glb", lo, hi)
    write_json(kit_proc.spec_path(store, eid), {"kind": "building", "form": "cube", "dims": [hi[0] - lo[0], hi[1] - lo[1], hi[2]]})
    write_json(glb.with_name("procedural.json"), {"brief_sha": None})
    versions.record_procedural(store, eid, {"faces": 12})
    return glb


def _trellis(store, eid, lo, hi):
    out = kit_proc.out_dir(store, eid)
    glb = glb_box(out / "a_1024_cascade_s1.glb", lo, hi)
    write_json(out / "a_1024_cascade_s1.json", {"seed": 1})
    versions.record_trellis(store, "library", eid, {"glb": f"3d/library/{eid}/a_1024_cascade_s1.glb", "input": "x",
                                                    "seed": 1, "mode": "1024_cascade"})
    return glb


def test_glb_bounds_read_from_the_header_in_blenders_frame(tmp_path):
    g = glb_box(tmp_path / "b.glb", [-1, -2, 0], [3, 4, 5])
    lo, hi = assembly.glb_bounds(g)
    assert lo.tolist() == pytest.approx([-1, -2, 0]) and hi.tolist() == pytest.approx([3, 4, 5])


def test_children_validate(store):
    with pytest.raises(ValueError, match="itself"):
        assembly.set_children(store, "portico", [{"entry": "portico"}])
    with pytest.raises(ValueError, match="no library entry"):
        assembly.set_children(store, "portico", [{"entry": "nope"}])
    with pytest.raises(ValueError, match="anchor"):
        assembly.set_children(store, "portico", [{"entry": "gate", "anchor": "roof"}])
    rows = assembly.set_children(store, "portico", [{"entry": "gate", "anchor": "front", "offset": [0, 0, 0], "yaw": -90}])
    assert rows == [{"entry": "gate", "anchor": "front", "offset": [0, 0, 0], "yaw": 270.0, "scale": 1.0}]
    with pytest.raises(ValueError, match="one level"):           # a child can't carry pieces
        assembly.set_children(store, "gate", [{"entry": "statue"}])
    with pytest.raises(ValueError, match="one level"):           # nor be attached to a child
        assembly.set_children(store, "statue", [{"entry": "portico"}])
    assert assembly.parent_of(store, "gate") == "portico"
    assert assembly.set_children(store, "portico", []) == [] and "portico" not in sw_library.decisions(store)


def test_front_anchor_follows_the_parent_and_trellis_children_take_their_entrys_height(store):
    _procedural(store, "portico", [-10, -5, 0], [10, 5, 8])           # 20 x 10 m, centred
    _trellis(store, "gate", [-0.4, -0.25, -0.5], [0.4, 0.25, 0.5])      # TRELLIS: 1 unit tall
    assembly.set_children(store, "portico", [{"entry": "gate", "anchor": "front", "offset": [0, 0, 0]},
                                             {"entry": "statue", "anchor": "front", "offset": [0, -9, 0], "yaw": 90, "scale": 1.5}])
    lay = assembly.layout(store, "portico")
    gate, statue = lay["children"]
    assert gate["size"] == pytest.approx([9.6, 6.0, 12.0])               # fitted to the entry's 12 m height
    m = np.asarray(gate["matrix"])                                     # the gate's base centre lands on the front edge
    assert (m @ [0, 0, -0.5, 1])[:3] == pytest.approx([0, -5, 0])
    assert statue["glb"] is None and statue["size"] == pytest.approx([3, 3, 7.5])   # placeholder at 1.5x
    assert statue["at"] == pytest.approx([0, -14, 0])
    # the parent rebuilt deeper: the gate stays on its (new) front edge
    _procedural(store, "portico", [-10, -8, 0], [10, 8, 8])
    gate = assembly.layout(store, "portico")["children"][0]
    assert (np.asarray(gate["matrix"]) @ [0, 0, -0.5, 1])[:3] == pytest.approx([0, -8, 0])


def test_children_follow_a_placed_parent_into_unreal_with_rotation_and_scale(store):
    _procedural(store, "portico", [-10, -5, 0], [10, 5, 8])
    _trellis(store, "gate", [-0.4, -0.25, -0.5], [0.4, 0.25, 0.5])
    assembly.set_children(store, "portico", [{"entry": "gate", "anchor": "front", "offset": [3, -2, 0], "yaw": 0, "scale": 2},
                                             {"entry": "statue", "anchor": "centre"}])
    pl = {"key": "portico_s1", "entry": "portico", "glb": "3d/library/portico/procedural.glb",
          "x": 100.0, "y": 50.0, "z": 4.0, "yaw": 90.0, "w": 20, "d": 10, "slot": "s1"}
    kids, entries, missing = assembly.child_placements(store, [pl])
    assert missing == ["statue"] and len(kids) == 1 and entries == {}   # no model: left out; TRELLIS: own materials
    k = kids[0]
    assert k["parent"] == "portico_s1" and k["entry"] == "gate" and k["key"] == "portico_s1__gate"
    m = np.asarray(k["matrix"])
    # local (3, -5 - 2) turned 90 deg ccw -> (7, 3), plus the slot
    assert (m @ [0, 0, -0.5, 1])[:3] == pytest.approx([107, 53, 4])
    assert np.linalg.norm(m[:3, 0]) == pytest.approx(2 * 12.0)          # scale 2 x the 12 m fit
    from asset_pipeline import ue
    t = ue._blender_to_ue_xf(m)
    assert t["scale"] == pytest.approx([24, 24, 24])


def test_api_children_and_footprint(store):
    _procedural(store, "portico", [-10, -5, 0], [10, 5, 8])
    client = TestClient(ui_app.create_app(JobQueue()))
    r = client.put("/api/projects/p/library/entries/portico/children",
                   json={"children": [{"entry": "statue", "anchor": "front", "offset": [0, -9, 0]}], "build": False})
    assert r.status_code == 200 and r.json()["children"][0]["at"] == [0, -14, 0]
    g = client.get("/api/projects/p/library/entries/portico/children").json()
    assert g["parent"]["size"] == [20, 10, 8] and g["anchors"]["front"] == [0, -5, 0]
    assert g["assembly"] == {"glb": None, "fresh": False, "missing": []}
    assert client.get("/api/projects/p/library/entries/statue/footprint").json()["size"] == [2, 2, 5]
    bad = client.put("/api/projects/p/library/entries/portico/children", json={"children": [{"entry": "portico"}]})
    assert bad.status_code == 400


@pytest.mark.skipif(not REAL_BLENDER.exists(), reason="needs Blender")
def test_assembly_glb_in_real_blender_with_a_placeholder(store):
    _procedural(store, "portico", [-10, -5, 0], [10, 5, 8])
    _trellis(store, "gate", [-0.4, -0.25, -0.5], [0.4, 0.25, 0.5])
    assembly.set_children(store, "portico", [{"entry": "gate", "anchor": "front"},
                                             {"entry": "statue", "anchor": "front", "offset": [0, -9, 0]}])
    r = assembly.build(store, "portico", log=lambda s: None)
    assert r["missing"] == ["statue"]
    lo, hi = assembly.glb_bounds(store.root / r["glb"])
    # portico 20 x 10 x 8; gate 9.6 x 6 x 12 centred on the front edge (y -8..-2); statue box 2 x 2 x 5 at y -14
    assert lo.tolist() == pytest.approx([-10, -15, 0], abs=1e-3) and hi.tolist() == pytest.approx([10, 5, 12], abs=1e-3)
    st = assembly.assembly_state(store, "portico")
    assert st["fresh"] and filestore.is_ref(store.root / r["glb"])
    assembly.set_children(store, "portico", [{"entry": "gate", "anchor": "centre"}])
    assert not assembly.assembly_state(store, "portico")["fresh"]       # an edit makes it stale
