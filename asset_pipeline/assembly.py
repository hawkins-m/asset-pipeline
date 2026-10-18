"""Attached pieces: a library entry can list child entries placed with it (a minimal assembly).

Kept in the library decisions file under the parent entry (sw_library.decisions_path):
    "market-forum-portico": {..., "children": [
        {"entry": "portico-gate", "anchor": "front", "offset": [0, 0, 0], "yaw": 0, "scale": 1}]}

Frame: the parent's local space as its GLB has it in Blender: metres, Z up, front facing -Y.
anchor: where the offset starts, read off the parent's model each time, so a child stays put
when the parent is rebuilt at another size:
    centre   the middle of the parent's footprint, on its base
    front    the middle of its front edge (the -Y side), on its base
offset: x (right), y (back; negative = in front), z (up), metres; yaw: degrees about Z (counter-
clockwise seen from above); scale: on top of the child's own size.

A child is its current version's model (versions.current_glb), with its footprint centred on its
origin and its base at z 0. A TRELLIS model (about 1 unit tall) is scaled to its entry's height,
as cleanup does; a procedural one is in metres already. A child with no model yet is a
placeholder box at its entry's size (review renders) and is left out in Unreal.

Children go everywhere the parent goes: the assembly GLB (3d/library/<parent>/assembly.glb, the
Asset review preview and review renders) and Unreal look scenes (separate actors attached to
the parent's, in its outliner folder). One level only: a child can't have children of its own.
"""
import hashlib
import json
import math
import struct
from pathlib import Path

import numpy as np

from .project import ProjectStore, read_json, write_json

ANCHORS = ("centre", "front")
KEY = "children"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "blender_assembly.py"


# --- GLB bounds without Blender -------------------------------------------------------------

def _gltf(path: Path) -> dict:
    with open(path, "rb") as f:
        magic, _version, _length = struct.unpack("<4sII", f.read(12))
        if magic != b"glTF":
            raise ValueError(f"not a GLB: {path}")
        n, kind = struct.unpack("<I4s", f.read(8))
        if kind != b"JSON":
            raise ValueError(f"GLB without a JSON chunk: {path}")
        return json.loads(f.read(n))


def _node_matrix(n: dict) -> np.ndarray:
    if "matrix" in n:
        return np.asarray(n["matrix"], float).reshape(4, 4).T          # glTF is column-major
    m = np.eye(4)
    x, y, z, w = n.get("rotation", [0, 0, 0, 1])
    m[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                 [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                 [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    m[:3, :3] = m[:3, :3] * np.asarray(n.get("scale", [1, 1, 1]), float)
    m[:3, 3] = n.get("translation", [0, 0, 0])
    return m


def glb_bounds(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """(lo, hi) of a GLB's meshes in Blender's frame (Z up: glTF (x, y, z) -> (x, -z, y)), from the
    accessors' min/max and the node transforms."""
    g = _gltf(Path(path))
    nodes, meshes, acc = g.get("nodes", []), g.get("meshes", []), g.get("accessors", [])
    scene = (g.get("scenes") or [{"nodes": list(range(len(nodes)))}])[g.get("scene", 0)]
    pts = []

    def walk(i, parent):
        n = nodes[i]
        m = parent @ _node_matrix(n)
        if "mesh" in n:
            for prim in meshes[n["mesh"]].get("primitives", []):
                a = acc[prim["attributes"]["POSITION"]]
                lo, hi = np.asarray(a["min"], float), np.asarray(a["max"], float)
                for c in np.array(np.meshgrid(*zip(lo, hi))).T.reshape(-1, 3):
                    pts.append((m @ np.append(c, 1.0))[:3])
        for k in n.get("children", []):
            walk(k, m)
    for i in scene.get("nodes", []):
        walk(i, np.eye(4))
    if not pts:
        raise ValueError(f"no meshes in {path}")
    p = np.asarray(pts)
    p = np.stack([p[:, 0], -p[:, 2], p[:, 1]], axis=1)
    return p.min(0), p.max(0)


# --- the children -------------------------------------------------------------------------------

def children(store: ProjectStore, eid: str) -> list[dict]:
    from .stages import sw_library
    return [dict(c) for c in (sw_library.decisions(store).get(eid) or {}).get(KEY) or []]


def _clean(store: ProjectStore, eid: str, rows: list[dict]) -> list[dict]:
    from . import library_entries as le
    ents = le._all_entries(store)
    out, seen = [], set()
    for r in rows:
        c = r.get("entry")
        if c not in ents:
            raise ValueError(f"no library entry {c!r}")
        if c == eid:
            raise ValueError("an entry can't be attached to itself")
        if children(store, c):
            raise ValueError(f"{c} has attached pieces of its own: one level only")
        anchor = r.get("anchor") or "centre"
        if anchor not in ANCHORS:
            raise ValueError(f"anchor must be one of {ANCHORS}")
        off = [float(v) for v in (r.get("offset") or [0, 0, 0])]
        if len(off) != 3 or not all(math.isfinite(v) for v in off):
            raise ValueError("offset: x, y, z in metres")
        scale = float(r.get("scale") if r.get("scale") not in (None, "") else 1.0)
        if not (scale > 0 and math.isfinite(scale)):
            raise ValueError("scale must be above 0")
        yaw = float(r.get("yaw") or 0.0)
        out.append({"entry": c, "anchor": anchor, "offset": [round(v, 3) for v in off],
                    "yaw": round(yaw % 360.0, 2), "scale": round(scale, 4)})
        seen.add(c)
    return out


def set_children(store: ProjectStore, eid: str, rows: list[dict]) -> list[dict]:
    """Manage entry… › Attached pieces: the parent's children (an empty list removes them)."""
    from . import library_entries as le
    from .stages import sw_library
    if eid not in le._all_entries(store):
        raise KeyError(f"no library entry {eid}")
    parents = [p for p, d in sw_library.decisions(store).items()
               if not p.startswith("_") and any(c.get("entry") == eid for c in (d or {}).get(KEY) or [])]
    if rows and parents:
        raise ValueError(f"{eid} is attached to {', '.join(parents)}: one level only")
    rows = _clean(store, eid, rows)
    d = sw_library.decisions(store)
    rec = d.setdefault(eid, {})
    if rows:
        rec[KEY] = rows
    else:
        rec.pop(KEY, None)
        if not rec:
            d.pop(eid)
    write_json(sw_library.decisions_path(store), d)
    return rows


def parent_of(store: ProjectStore, eid: str) -> str | None:
    from .stages import sw_library
    return next((p for p, d in sw_library.decisions(store).items()
                 if not p.startswith("_") and any(c.get("entry") == eid for c in (d or {}).get(KEY) or [])), None)


# --- geometry ----------------------------------------------------------------------------------

def model(store: ProjectStore, eid: str) -> dict:
    """An entry's model as a piece: {glb (Path or None), kind, fit (scale to metres), centre (the
    footprint middle on the base, model units), size [w, d, h] in metres}."""
    from . import library_entries as le, versions
    glb = versions.current_glb(store, eid)
    n = versions.current(store, eid) if glb else None
    kind = versions.get(store, eid, n)["kind"] if n else None
    target = (le.form_info(store, eid).get("size_m") or (le._all_entries(store).get(eid) or {}).get("size_m"))
    if glb is None:
        w, d, h = target or [1.0, 1.0, 1.0]
        return {"glb": None, "kind": None, "fit": 1.0, "centre": [0.0, 0.0, 0.0], "size": [w, d, h]}
    lo, hi = glb_bounds(glb)
    ext = hi - lo
    fit = float(target[2] / ext[2]) if kind == "trellis" and target and ext[2] > 0 else 1.0
    return {"glb": glb, "kind": kind, "fit": fit, "centre": [float((lo[0] + hi[0]) / 2), float((lo[1] + hi[1]) / 2), float(lo[2])],
            "size": [float(v) for v in ext * fit]}


def anchors(parent: dict) -> dict:
    """Anchor points in the parent's frame (after its fit): its model's bounds, centred."""
    w, d, _ = parent["size"]
    return {"centre": [0.0, 0.0, 0.0], "front": [0.0, -d / 2, 0.0]}


def _rz(deg: float) -> np.ndarray:
    a = math.radians(deg)
    m = np.eye(4)
    m[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
    return m


def _t(v) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = v
    return m


def _s(k: float) -> np.ndarray:
    return np.diag([k, k, k, 1.0])


def piece_matrix(piece: dict) -> np.ndarray:
    """A model's own units -> its piece frame: footprint centred on the origin, base at 0, metres."""
    return _s(piece["fit"]) @ _t([-v for v in piece["centre"]])


def layout(store: ProjectStore, eid: str) -> dict:
    """Everything an assembly needs: {parent: {glb, matrix, size}, children: [{entry, glb, matrix
    (child model -> parent frame), size (metres, scaled), anchor, offset, yaw, scale, at (the
    child's footprint centre in the parent frame)}], anchors}. The parent frame has the parent's
    footprint centred on the origin, its base at 0."""
    par = model(store, eid)
    an = anchors(par)
    out = []
    for c in children(store, eid):
        ch = model(store, c["entry"])
        at = np.asarray(an[c["anchor"]]) + np.asarray(c["offset"], float)
        m = _t(at) @ _rz(c["yaw"]) @ _s(c["scale"]) @ piece_matrix(ch)
        out.append(c | {"glb": ch["glb"], "kind": ch["kind"], "matrix": m.tolist(),
                        "size": [v * c["scale"] for v in ch["size"]], "at": [float(v) for v in at]})
    return {"parent": {"glb": par["glb"], "kind": par["kind"], "matrix": piece_matrix(par).tolist(), "size": par["size"]},
            "children": out, "anchors": an}


def info(store: ProjectStore, eid: str) -> dict:
    """Manage entry… › Attached pieces: the layout for the top-down view (no model paths)."""
    lay = layout(store, eid)
    rel = lambda p: p.relative_to(store.root).as_posix() if p else None       # noqa: E731
    return {"entry": eid, "parent": lay["parent"] | {"glb": rel(lay["parent"]["glb"])}, "anchors": lay["anchors"],
            "children": [{k: v for k, v in c.items() if k != "matrix"} | {"glb": rel(c["glb"])} for c in lay["children"]],
            "attached_to": parent_of(store, eid), "assembly": assembly_state(store, eid)}


def footprint(store: ProjectStore, eid: str) -> dict:
    m = model(store, eid)
    return {"entry": eid, "size": m["size"], "has_model": m["glb"] is not None, "kind": m["kind"]}


# --- the assembly GLB (Asset review preview, review renders) -----------------------------------

def assembly_path(store: ProjectStore, eid: str) -> Path:
    from . import kit_proc
    return kit_proc.out_dir(store, eid) / "assembly.glb"


def _stamp(store: ProjectStore, lay: dict) -> str:
    parts = []
    for p in [lay["parent"]] + lay["children"]:
        g = p["glb"]
        st = g.stat() if g else None
        parts.append([str(g), st.st_size if st else None, st.st_mtime_ns if st else None, p["matrix"], p.get("size")])
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def assembly_state(store: ProjectStore, eid: str) -> dict | None:
    """{glb, fresh} of the parent's assembly, or None when it has no children."""
    if not children(store, eid):
        return None
    p = assembly_path(store, eid)
    meta = read_json(p.with_suffix(".json"), default=None) or {}
    try:
        fresh = p.is_file() and meta.get("stamp") == _stamp(store, layout(store, eid))
    except (FileNotFoundError, ValueError):
        fresh = False
    return {"glb": p.relative_to(store.root).as_posix() if p.is_file() else None, "fresh": fresh,
            "missing": meta.get("missing", [])}


def build(store: ProjectStore, eid: str, log=print) -> dict:
    """The parent with its children in one GLB (Blender, a few seconds). Children with no model
    are placeholder boxes. Returns {glb, missing}."""
    from . import blender, filestore
    lay = layout(store, eid)
    if lay["parent"]["glb"] is None:
        raise FileNotFoundError(f"{eid} has no 3D model yet: build it first")
    out = assembly_path(store, eid)
    job = {"out": str(out),
           "pieces": [{"name": eid, "glb": str(lay["parent"]["glb"]), "matrix": lay["parent"]["matrix"]}]
           + [{"name": c["entry"], "glb": str(c["glb"]) if c["glb"] else None, "matrix": c["matrix"],
               "box": None if c["glb"] else c["size"], "at": c["at"], "yaw": c["yaw"]} for c in lay["children"]]}
    jp = out.with_name("assembly_job.json")
    write_json(jp, job)
    lg = out.with_name("assembly.log")
    lg.unlink(missing_ok=True)
    filestore.detach(out)
    blender.run(SCRIPT, [jp], lg, f"assembly {eid}")
    if not out.is_file():
        raise RuntimeError("the assembly wasn't written:\n" + "\n".join(lg.read_text().splitlines()[-15:]))
    filestore.intern(store, out)
    missing = [c["entry"] for c in lay["children"] if c["glb"] is None]
    write_json(out.with_suffix(".json"), {"stamp": _stamp(store, lay), "missing": missing,
                                          "children": [{k: c[k] for k in ("entry", "anchor", "offset", "yaw", "scale")}
                                                       for c in lay["children"]]})
    log(f"assembly {eid}: {len(lay['children'])} attached piece(s)" + (f", placeholders for {', '.join(missing)}" if missing else ""))
    return {"glb": out.relative_to(store.root).as_posix(), "missing": missing}


def render_glb(store: ProjectStore, eid: str, log=print) -> Path | None:
    """What review renders take: the assembly (rebuilt when stale) for a parent with children."""
    if not children(store, eid):
        return None
    st = assembly_state(store, eid)
    if not (st and st["fresh"]):
        build(store, eid, log=log)
    return assembly_path(store, eid)


# --- Unreal look scenes --------------------------------------------------------------------------

def child_placements(store: ProjectStore, placements: list[dict]) -> tuple[list[dict], dict, list[str]]:
    """The attached pieces of placed parents, in world space: ([{key, entry, glb, matrix (Blender
    world 4x4, with scale), parent (the parent placement's key), mesh}], {child entry: its
    procedural job.json for its materials}, [children skipped: no model])."""
    from . import kit_proc
    out, entries, missing = [], {}, []
    cache = {}
    for p in placements:
        e = p["entry"]
        if e not in cache:
            cache[e] = layout(store, e) if children(store, e) else None
        lay = cache[e]
        if lay is None:
            continue
        # the placed GLB is the parent's raw model at the slot pose; children are in its piece frame
        W = _t([p["x"], p["y"], p["z"]]) @ _rz(p["yaw"]) @ np.linalg.inv(np.asarray(lay["parent"]["matrix"]))
        for c in lay["children"]:
            if c["glb"] is None:
                missing.append(c["entry"])
                continue
            m = W @ np.asarray(c["matrix"])
            out.append({"key": f"{p['key']}__{c['entry']}".replace("-", "m"), "entry": c["entry"],
                        "glb": c["glb"].relative_to(store.root).as_posix(), "matrix": m.tolist(),
                        "parent": p["key"], "mesh": f"child_{c['entry']}".replace("-", "m")})
            job = kit_proc.out_dir(store, c["entry"]) / "procedural" / "job.json"
            if c["kind"] == "procedural" and job.is_file():
                entries[c["entry"]] = job.relative_to(store.root).as_posix()
    return out, entries, sorted(set(missing))

