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
along.
"""
import math
import os
import shutil
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


# --- from the project ------------------------------------------------------------------------

def planned_per_slot(store: ProjectStore, gb: dict) -> dict[str, int]:
    """{typed slot id: how many planned buildings stand in it}."""
    from . import city, library_instances
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.plan_terrain(store, layout))
    typed = [s for s in gb["slots"] if s.get("typology")]
    n: dict[str, int] = {}
    for b in P.typed:
        x, y, *_ = library_instances.footprint(b)
        sid = slot_of_point(typed, b.typology, x, y)
        if sid:
            n[sid] = n.get(sid, 0) + 1
    return n


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
        try:
            os.link(src, dst)
        except OSError:
            shutil.copyfile(src, dst)
        assets[aid] = {"id": aid, "kind": "library", "entry": p["entry"], "glb": f"{LIB_DIR}/{aid}.glb",
                       "source": p["glb"]}
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
    held_entries = sorted({h["entry"] for h in pl["held"]})
    m["library"] = {"use_active": use_active, "placed": len(pl["placed"]), "parents": pl["parents"],
                    "slots_replaced": len(pl["replaced"]), "held": len(pl["held"]), "held_entries": held_entries,
                    "missing_children": pl["missing_children"]}
    log(f"library: {pl['parents']} builds + {len(pl['placed']) - pl['parents']} attached pieces in "
        f"{len(pl['replaced'])} slots; {len(pl['held'])} held back as greybox")
    return m["library"]
