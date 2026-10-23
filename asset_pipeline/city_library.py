"""The city's library layer for the Unreal manifest (`ap export --assets`, `ap ue build`): which
library builds stand in the city level instead of their greybox.

Placements are those of the look scenes (`look._candidates` with no camera: the whole plan):
typed buildings (one instance build per planned building, no slot id) and kit slots (the entry's
build at the slot's pose). Attached pieces come with their placed parent
(`assembly.child_placements`).

Which builds: Approved in library review (default), or every built asset's Active build
(`use_active`, for previews).

Mixed slots (no partial slots):
  - a kit slot whose build is placed is replaced whole (its greybox goes);
  - a typed slot (a block of several buildings) is replaced only when every planned building in
    it has a placed build: the greybox pieces standing on those footprints (+1 m, as in the look
    scenes) go and the rest of the slot (courts, paving) stays;
  - otherwise the whole slot stays greybox and its builds are not placed (`held`).

Each placed build is an actor of its own in Unreal, attached to its slot's actor: the manifest
carries its transform relative to the slot, so a slot moved by hand in Unreal takes its builds
along. Their GLBs go to export/library/ without embedded textures (`strip_textures`: the
importer gives them the entry's material instances; Interchange would import every map).
"""
import json
import math
import shutil
import struct
from pathlib import Path

import numpy as np

from . import ue_coords
from .project import ProjectStore, read_json

FOOT_MARGIN = 1.0          # m: a greybox piece within this of a placed footprint goes with it
LIB_DIR = "library"        # export/library/<asset id>.glb


# --- pure parts ------------------------------------------------------------------------------

def slot_of_point(slots: list[dict], typology: str, x: float, y: float) -> str | None:
    """The typed slot of a building: of its typology, its bbox around (x, y); the nearest slot
    centre when bboxes overlap."""
    best = None
    for s in slots:
        if s.get("typology") != typology:
            continue
        (x0, y0, _), (x1, y1, _) = s["bbox"]
        if x0 - FOOT_MARGIN <= x <= x1 + FOOT_MARGIN and y0 - FOOT_MARGIN <= y <= y1 + FOOT_MARGIN:
            m = np.asarray(s["matrix"], float)
            d = math.hypot(m[0, 3] - x, m[1, 3] - y)
            if best is None or d < best[0]:
                best = (d, s["id"])
    return best[1] if best else None


def choose(placements: list[dict], planned: dict[str, int], ok) -> tuple[list[dict], dict[str, str], list[dict]]:
    """The mixed-slot rule. placements carry "slot" (kit slots: theirs; typed: slot_of_point) and
    "typed"; planned {typed slot: planned buildings in it}; ok(entry): may this entry's build be
    placed. Returns (placed, {slot: "whole" | "footprints"}, held)."""
    placed, replaced, held = [], {}, []
    by_slot: dict[str, list[dict]] = {}
    for p in placements:
        if p.get("slot") is None:
            held.append(p | {"why": "no greybox slot"})
            continue
        by_slot.setdefault(p["slot"], []).append(p)
    for sid, ps in sorted(by_slot.items()):
        typed = ps[0].get("typed", False)
        good = [p for p in ps if ok(p["entry"])]
        if not typed:
            if good:
                placed += good[:1]                  # one build per kit slot
                replaced[sid] = "whole"
                held += [p | {"why": "another build stands in this slot"} for p in ps if p is not good[0]]
            else:
                held += [p | {"why": "not approved"} for p in ps]
            continue
        need = planned.get(sid, 0)
        if need and len(good) >= need:
            placed += good
            replaced[sid] = "footprints"
        else:
            why = "not approved" if len(good) < len(ps) else f"{len(good)} of {need} buildings in the slot have a build"
            held += [p | {"why": why} for p in ps]
    return placed, replaced, held


def on_footprint(x: float, y: float, foot: dict) -> bool:
    a = math.radians(foot["yaw"])
    dx, dy = x - foot["x"], y - foot["y"]
    u, v = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
    return abs(u) <= foot["w"] / 2 + FOOT_MARGIN and abs(v) <= foot["d"] / 2 + FOOT_MARGIN


def kept_pieces(slot: dict, mode: str | None, feet: list[dict]) -> list[dict]:
    """The slot's greybox pieces that stay: all (not replaced), none ("whole"), or those not
    standing on a placed footprint ("footprints")."""
    if mode is None:
        return slot["pieces"]
    if mode == "whole":
        return []
    S = np.asarray(slot["matrix"], float)
    out = []
    for p in slot["pieces"]:
        x, y = (S @ np.asarray(p["matrix_local"], float))[:2, 3]
        if not any(on_footprint(x, y, f) for f in feet):
            out.append(p)
    return out


def world_matrix(p: dict) -> np.ndarray:
    """A placement's Blender world 4x4: an attached piece's own matrix, else (x, y, z, yaw)."""
    if "matrix" in p:
        return np.asarray(p["matrix"], float)
    a = math.radians(p["yaw"])
    B = np.eye(4)
    B[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
    B[:3, 3] = [p["x"], p["y"], p["z"]]
    return B


def asset_id(key: str) -> str:
    return "lib_" + "".join(c if c.isalnum() or c == "_" else "_" for c in key)


def actor_rows(placed: list[dict], slots: dict[str, dict]) -> dict[str, list[dict]]:
    """{slot id: [{key, asset, entry, parent, relative, world}]}: UE transforms of the placed
    builds, relative to their slot's actor and in the world."""
    out: dict[str, list[dict]] = {}
    for p in placed:
        S = np.asarray(slots[p["slot"]]["matrix"], float)
        B = world_matrix(p)
        out.setdefault(p["slot"], []).append({
            "key": p["key"], "asset": asset_id(p.get("mesh") or p["key"]), "entry": p["entry"],
            "parent": p.get("parent"), "relative": ue_coords.to_ue(np.linalg.inv(S) @ B), "world": ue_coords.to_ue(B)})
    return out


TEX_KEYS = ("baseColorTexture", "metallicRoughnessTexture", "normalTexture", "occlusionTexture", "emissiveTexture")


def strip_textures(data: bytes) -> bytes:
    """A GLB without its embedded images: textures, images and samplers go, materials lose their
    texture slots (names and factors stay: the importer assigns its own instances by material
    name), and the binary chunk keeps only the buffer views something else still uses."""
    n = struct.unpack_from("<I", data, 12)[0]
    js = json.loads(data[20:20 + n])
    off = 20 + n
    binb = data[off + 8:off + 8 + struct.unpack_from("<I", data, off)[0]] if off < len(data) else b""
    drop = {im["bufferView"] for im in js.get("images", []) if "bufferView" in im}
    for k in ("images", "textures", "samplers"):
        js.pop(k, None)
    for m in js.get("materials", []):
        pbr = m.get("pbrMetallicRoughness") or {}
        for k in TEX_KEYS:
            pbr.pop(k, None)
            m.pop(k, None)
        m.get("extensions", {}).pop("KHR_materials_emissive_strength", None)
    used = [e for e in js.get("extensionsUsed", []) if e != "KHR_texture_transform"]
    if used:
        js["extensionsUsed"] = used
    else:
        js.pop("extensionsUsed", None)
    js.pop("extensionsRequired", None) if not js.get("extensionsRequired") else None
    views, remap, out = js.get("bufferViews", []), {}, bytearray()
    kept = []
    for i, v in enumerate(views):
        if i in drop:
            continue
        while len(out) % 4:
            out.append(0)
        start = v.get("byteOffset", 0)
        chunk = binb[start:start + v["byteLength"]]
        remap[i] = len(kept)
        kept.append(v | {"byteOffset": len(out)})
        out += chunk
    js["bufferViews"] = kept
    for a in js.get("accessors", []):
        if "bufferView" in a:
            a["bufferView"] = remap[a["bufferView"]]
        for part in ("indices", "values"):
            sp = (a.get("sparse") or {}).get(part)
            if sp:
                sp["bufferView"] = remap[sp["bufferView"]]
    while len(out) % 4:
        out.append(0)
    if js.get("buffers"):
        js["buffers"][0]["byteLength"] = len(out)
    jb = json.dumps(js, separators=(",", ":"), sort_keys=True).encode()
    jb += b" " * (-len(jb) % 4)
    total = 12 + 8 + len(jb) + (8 + len(out) if out else 0)
    glb = struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(jb), 0x4E4F534A) + jb
    if out:
        glb += struct.pack("<II", len(out), 0x004E4942) + bytes(out)
    return glb


# --- from the project ------------------------------------------------------------------------

def count_planned(buildings, typed: list[dict]) -> dict[str, int]:
    """{typed slot id: distinct planned buildings in it}. buildings: (typology, x, y). Two at the
    same spot (rounded to the metre, as library_instances keys its builds) share one build, so
    they count once."""
    seen: dict[str, set] = {}
    for t, x, y in buildings:
        sid = slot_of_point(typed, t, x, y)
        if sid:
            seen.setdefault(sid, set()).add((t, round(x), round(y)))
    return {sid: len(v) for sid, v in seen.items()}


def planned_per_slot(store: ProjectStore, gb: dict) -> dict[str, int]:
    """{typed slot id: how many distinct planned buildings stand in it}."""
    from . import city, library_instances
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.plan_terrain(store, layout))
    typed = [s for s in gb["slots"] if s.get("typology")]
    return count_planned([(b.typology, *library_instances.footprint(b)[:2]) for b in P.typed], typed)


def ok_entries(store: ProjectStore, use_active: bool):
    """ok(entry) for `choose`: Approved in library review, or (use_active) any built entry."""
    if use_active:
        return lambda e: True
    from .look import approved
    good = approved(store)
    return lambda e: e in good


def plan(store: ProjectStore, gb: dict, use_active: bool = False) -> dict:
    """{placed (with attached pieces), replaced, held, entries (materials), missing_children}."""
    from . import assembly
    from .look import _candidates
    cands, _, entries = _candidates(store, None, math.inf)
    typed = [s for s in gb["slots"] if s.get("typology")]
    for p in cands:
        if p.get("slot") is None:
            p["typed"] = True
            p["slot"] = slot_of_point(typed, p.get("typology"), p["x"], p["y"])
    placed, replaced, held = choose(cands, planned_per_slot(store, gb), ok_entries(store, use_active))
    kids, kid_entries, missing = assembly.child_placements(store, placed)
    by_key = {p["key"]: p for p in placed}
    kids = [k | {"slot": by_key[k["parent"]]["slot"]} for k in kids]
    used = {p["entry"] for p in placed + kids}
    entries = {e: j for e, j in {**kid_entries, **entries}.items() if e in used}
    return {"placed": placed + kids, "parents": len(placed), "replaced": replaced, "held": held,
            "entries": entries, "missing_children": missing}


OUTER_RING = "/Game/AP/Terrain/SM_OuterRing"            # ap terrain ue (terrain_build.ue_assets)
TERRAIN_MI = "/Game/AP/Terrain/MI_AP_TerrainAuto"


def terrain_section(gb: dict) -> dict | None:
    """The importer's terrain step: the outer ring's greybox slot (it goes when SM_OuterRing is in
    the project) and the terrain slots that take the auto material."""
    ids = [s["id"] for s in gb["slots"] if s["type"] == "terrain"]
    if not ids:
        return None
    outer = next((i for i in ids if i.endswith("outer")), None)
    return {"outer_slot": outer, "material_slots": [i for i in ids if i != outer],
            "outer_ring": OUTER_RING, "auto_material": TERRAIN_MI}


def add_to_manifest(store: ProjectStore, m: dict, gb: dict, index: dict, out: Path, use_active: bool = False,
                    log=print) -> dict:
    """Put the library layer into a stand-in manifest (in place): the placed builds' GLBs under
    export/library/, their assets, each replaced slot's remaining greybox and its builds."""
    from . import ue
    from .stages.s7_export import manifest_components
    pl = plan(store, gb, use_active)
    slots = {s["id"]: s for s in gb["slots"]}
    feet: dict[str, list[dict]] = {}
    for p in pl["placed"]:
        if "matrix" not in p:
            feet.setdefault(p["slot"], []).append(p)
    rows = actor_rows(pl["placed"], slots)
    lib = out / LIB_DIR
    shutil.rmtree(lib, ignore_errors=True)
    lib.mkdir(parents=True)
    assets = {}
    for p in pl["placed"]:
        aid = asset_id(p.get("mesh") or p["key"])
        if aid in assets:
            continue
        dst = lib / f"{aid}.glb"
        src = store.root / p["glb"]
        raw = src.read_bytes()
        dst.write_bytes(strip_textures(raw))         # Unreal would import every embedded map otherwise
        assets[aid] = {"id": aid, "kind": "library", "entry": p["entry"], "glb": f"{LIB_DIR}/{aid}.glb",
                       "source": p["glb"], "stripped_kb": round((len(raw) - dst.stat().st_size) / 1024)}
    for s in m["slots"]:
        mode = pl["replaced"].get(s["id"])
        if mode is None:
            continue
        s["components"] = manifest_components(kept_pieces(slots[s["id"]], mode, feet.get(s["id"], [])), index)
        s["library"] = rows.get(s["id"], [])
        s["replaced"] = mode
    m["assets"] = [a for a in m["assets"] if any(a["id"] == c["asset"] for s in m["slots"] for c in s["components"])]
    m["library_assets"] = sorted(assets.values(), key=lambda a: a["id"])
    m["library_entries"] = {e: ue.library_slots(store.root / j) for e, j in sorted(pl["entries"].items())}
    m["source"] = "library+greybox"
    m["terrain"] = terrain_section(gb)
    held_entries = sorted({h["entry"] for h in pl["held"]})
    m["library"] = {"use_active": use_active, "placed": len(pl["placed"]), "parents": pl["parents"],
                    "slots_replaced": len(pl["replaced"]), "held": len(pl["held"]), "held_entries": held_entries,
                    "missing_children": pl["missing_children"]}
    log(f"library: {pl['parents']} builds + {len(pl['placed']) - pl['parents']} attached pieces in "
        f"{len(pl['replaced'])} slots; {len(pl['held'])} held back as greybox")
    return m["library"]
