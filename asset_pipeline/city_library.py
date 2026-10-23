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

MATERIAL_OVERRIDES = "site/material_overrides.json"   # {entry: {tint, metallic, roughness}}: any asset
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


def footprint_of(W: np.ndarray, lo, hi) -> dict:
    """A placed model's outline on the ground: {x, y (world centre), w, d, yaw} from its world
    matrix (a turn about Z, uniform scale) and its own bounds (lo, hi; Blender frame)."""
    k = float(np.linalg.norm(W[:3, 0]))
    c = W @ np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2], 1.0])
    return {"x": float(c[0]), "y": float(c[1]), "w": float(hi[0] - lo[0]) * k, "d": float(hi[1] - lo[1]) * k,
            "yaw": math.degrees(math.atan2(W[1, 0], W[0, 0])), "base": float(c[2])}


def grounded(W: np.ndarray, lo, hi, ground, rule: str) -> np.ndarray:
    """W moved up or down so the model's base stands on the ground under its outline (rule:
    ground.RULES)."""
    f = footprint_of(W, lo, hi)
    out = np.array(W, float)
    out[2, 3] += ground.under(f["x"], f["y"], f["w"], f["d"], f["yaw"], rule) - f["base"]
    return out


def on_footprint(x: float, y: float, foot: dict) -> bool:
    a = math.radians(foot["yaw"])
    dx, dy = x - foot["x"], y - foot["y"]
    u, v = dx * math.cos(a) + dy * math.sin(a), -dx * math.sin(a) + dy * math.cos(a)
    return abs(u) <= foot["w"] / 2 + FOOT_MARGIN and abs(v) <= foot["d"] / 2 + FOOT_MARGIN


def kept_pieces(slot: dict, mode: str | None, feet: list[dict], drop=()) -> list[dict]:
    """The slot's greybox pieces that stay: all (not replaced), none ("whole"), those not
    standing on a placed footprint ("footprints"), and never the pieces in drop (props replaced
    by their builds, "pieces")."""
    if mode is None:
        return slot["pieces"]
    if mode == "whole":
        return []
    S = np.asarray(slot["matrix"], float)
    out = []
    for i, p in enumerate(slot["pieces"]):
        if i in drop:
            continue
        x, y = (S @ np.asarray(p["matrix_local"], float))[:2, 3]
        if not any(on_footprint(x, y, f) for f in feet):
            out.append(p)
    return out


def piece_pose(slot: dict, piece: dict) -> tuple[float, float, float, float]:
    """A greybox piece's world (x, y, base z, yaw deg): its origin is its base (the stand-in meshes
    have local z from 0)."""
    W = np.asarray(slot["matrix"], float) @ np.asarray(piece["matrix_local"], float)
    return float(W[0, 3]), float(W[1, 3]), float(W[2, 3]), math.degrees(math.atan2(W[1, 0], W[0, 0]))


def prop_groups(slot: dict, prop_pieces: dict[str, str], mains) -> list[tuple[int, str, list[int]]]:
    """The props in a slot: (main piece index, noun, [its other pieces]) per prop, each other
    piece (posts, tables, a hut's roof) given to the nearest main piece of the same noun."""
    S = np.asarray(slot["matrix"], float)
    pos = [(S @ np.asarray(p["matrix_local"], float))[:2, 3] for p in slot["pieces"]]
    groups = {i: (prop_pieces[p["piece"]], []) for i, p in enumerate(slot["pieces"]) if p["piece"] in mains}
    for i, p in enumerate(slot["pieces"]):
        noun = prop_pieces.get(p["piece"])
        if noun is None or i in groups:
            continue
        near = [(float(np.linalg.norm(pos[i] - pos[g])), g) for g, (n, _) in groups.items() if n == noun]
        if near:
            groups[min(near)[1]][1].append(i)
    return [(g, n, sub) for g, (n, sub) in sorted(groups.items())]


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


def _glb_parts(data: bytes) -> tuple[dict, bytes]:
    n = struct.unpack_from("<I", data, 12)[0]
    js = json.loads(data[20:20 + n])
    off = 20 + n
    return js, (data[off + 8:off + 8 + struct.unpack_from("<I", data, off)[0]] if off < len(data) else b"")


def baked_materials(data: bytes) -> list[dict]:
    """A GLB's own materials, in order (the order Unreal gives its material slots): which baked
    maps each has, its metallic and roughness as the importer sets them (glTF factor x the mean of
    the metallic-roughness map's blue / green channel), and its base colour map's mean sRGB colour
    (for the placement report's texture check)."""
    import io

    from PIL import Image
    js, binb = _glb_parts(data)

    def image(tex):
        if tex is None:
            return None
        src = js["textures"][tex["index"]].get("source")
        im = js["images"][src] if src is not None else None
        if not im or "bufferView" not in im:
            return None
        v = js["bufferViews"][im["bufferView"]]
        a = binb[v.get("byteOffset", 0):v.get("byteOffset", 0) + v["byteLength"]]
        return np.asarray(Image.open(io.BytesIO(a)).convert("RGB"), float) / 255
    out = []
    for m in js.get("materials", []):
        pbr = m.get("pbrMetallicRoughness") or {}
        base, mr = image(pbr.get("baseColorTexture")), image(pbr.get("metallicRoughnessTexture"))
        mf, rf = pbr.get("metallicFactor", 1.0), pbr.get("roughnessFactor", 1.0)
        out.append({"maps": sorted(k for k in ("baseColorTexture", "metallicRoughnessTexture") if k in pbr)
                    + ["normalTexture"] * ("normalTexture" in m),
                    "metallic": round(float(mf * (mr[..., 2].mean() if mr is not None else 1.0)), 3),
                    "roughness": round(float(rf), 3),            # the map's green channel carries the rest
                    "base_mean": [round(float(v), 3) for v in base.reshape(-1, 3).mean(0)] if base is not None
                    else [round(float(v), 3) for v in pbr.get("baseColorFactor", [1, 1, 1])[:3]]})
    return out


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


def planned_buildings(store: ProjectStore, gb: dict) -> dict[str, list[tuple]]:
    """{typed slot id: [(typology, x, y, base z, yaw) of each distinct planned building in it]}."""
    from . import city, library_instances
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.plan_terrain(store, layout))
    typed = [s for s in gb["slots"] if s.get("typology")]
    out: dict[str, dict] = {}
    for b in P.typed:
        x, y, z, *_ = library_instances.footprint(b)
        sid = slot_of_point(typed, b.typology, x, y)
        if sid:
            out.setdefault(sid, {})[(b.typology, round(x), round(y))] = (b.typology, x, y, z, b.rot)
    return {sid: list(v.values()) for sid, v in out.items()}


def planned_per_slot(store: ProjectStore, gb: dict) -> dict[str, int]:
    """{typed slot id: how many distinct planned buildings stand in it}."""
    return {sid: len(v) for sid, v in planned_buildings(store, gb).items()}


def representative_placements(store: ProjectStore, gb: dict, cands: list[dict], buildings: dict) -> list[dict]:
    """Typed slots holding at most one building that no instance build covers (a forum portico, a
    market hall away from the shots): the entry's own build at that building's footprint (at the
    slot's pose when the plan has no building there), replacing the slot whole."""
    lib = store.root / "3d" / "library"
    covered = {p["slot"] for p in cands if p.get("slot")}
    slots = {s["id"]: s for s in gb["slots"]}
    out = []
    for ent in (read_json(store.root / "site" / "library.json", default={}) or {}).get("entries", []):
        e = ent["id"]
        glb, job = lib / e / "procedural.glb", lib / e / "procedural" / "job.json"
        brief = read_json(store.root / "library" / "kits" / f"{e}.json", default=None)
        if not (ent.get("slot_ids") and glb.exists() and job.exists() and brief):
            continue                                 # (a building or a kit piece such as a forum gate)
        for sid in ent["slot_ids"]:
            s = slots.get(sid)
            bs = buildings.get(sid, [])
            if s is None or not s.get("typology") or sid in covered or len(bs) > 1:
                continue
            if bs:
                _, x, y, z, yaw = bs[0]
            else:
                m = np.asarray(s["matrix"], float)
                x, y, z, yaw = float(m[0, 3]), float(m[1, 3]), float(m[2, 3]), math.degrees(math.atan2(m[1, 0], m[0, 0]))
            w, d, *_ = brief.get("dims") or ent.get("size_m") or [10.0, 10.0, 10.0]
            out.append({"key": f"{e}_{sid}".replace("-", "m"), "entry": e, "glb": glb.relative_to(store.root).as_posix(),
                        "x": float(x), "y": float(y), "z": float(z), "yaw": float(yaw), "w": w, "d": d, "slot": sid,
                        "typed": False, "representative": True})
    return out


PROP_MAINS = ("market:hut", "market:tent", "market:canopy", "market:stall")


def prop_placements(store: ProjectStore, gb: dict) -> tuple[list[dict], dict[str, set], list[str]]:
    """TRELLIS props on their greybox pieces: each main prop piece becomes its entry's Active model
    (footprint centred, base on the piece's base, fitted to the plan size: assembly.model) at the
    piece's yaw; it and its other pieces leave the greybox. Returns (placements, {slot: piece
    indices they replace}, [prop entries with no model])."""
    from . import assembly
    from .stages.sw_library import PROP_PIECES, _slug
    out, drop, missing, models = [], {}, set(), {}
    for s in gb["slots"]:
        for main, noun, sub in prop_groups(s, PROP_PIECES, PROP_MAINS):
            e = _slug(noun)
            if e not in models:
                models[e] = assembly.model(store, e)
            mdl = models[e]
            if mdl["glb"] is None:
                missing.add(e)
                continue
            x, y, z, yaw = piece_pose(s, s["pieces"][main])
            W = assembly._t([x, y, z]) @ assembly._rz(yaw) @ assembly.piece_matrix(mdl)
            out.append({"key": f"{e}_{s['id']}_{main}".replace("-", "m"), "entry": e, "mesh": f"prop_{e}",
                        "glb": Path(mdl["glb"]).relative_to(store.root).as_posix(), "matrix": W.tolist(),
                        "slot": s["id"], "prop": True, "pieces": [main, *sub]})
    return out, drop, sorted(missing)


def ok_entries(store: ProjectStore, use_active: bool):
    """ok(entry) for `choose`: Approved in library review, or (use_active) any built entry."""
    if use_active:
        return lambda e: True
    from .look import approved
    good = approved(store)
    return lambda e: e in good


def settings(store: ProjectStore):
    from .schema import UnrealSettings
    return store.load().unreal or UnrealSettings()


def ground_all(store: ProjectStore, placements: list[dict], ground, rule: str, bounds: dict) -> None:
    """Every placement on the ground under its model's outline (in place): its z (or matrix) and
    its outline ("foot")."""
    from .assembly import glb_bounds
    for p in placements:
        if p["glb"] not in bounds:
            bounds[p["glb"]] = [v.tolist() for v in glb_bounds(store.root / p["glb"])]
        lo, hi = bounds[p["glb"]]
        W = grounded(world_matrix(p), lo, hi, ground, rule)
        if "matrix" in p:
            p["matrix"] = W.tolist()
        else:
            p["z"] = float(W[2, 3])
        p["foot"] = footprint_of(W, lo, hi)


def plan(store: ProjectStore, gb: dict, use_active: bool = False) -> dict:
    """{placed (with attached pieces), replaced, held, entries (materials), missing_children,
    bounds {glb: [lo, hi]}}. Everything placed stands on the ground (ground.Ground, the project's
    ground rule)."""
    from . import assembly
    from .ground import Ground
    from .look import _candidates
    cands, _, entries = _candidates(store, None, math.inf)
    typed = [s for s in gb["slots"] if s.get("typology")]
    for p in cands:
        if p.get("slot") is None:
            p["typed"] = True
            p["slot"] = slot_of_point(typed, p.get("typology"), p["x"], p["y"])
    buildings = planned_buildings(store, gb)
    reps = representative_placements(store, gb, cands, buildings)
    for p in reps:
        entries[p["entry"]] = (Path("3d/library") / p["entry"] / "procedural" / "job.json").as_posix()
    ok = ok_entries(store, use_active)
    placed, replaced, held = choose(cands + reps, {sid: len(v) for sid, v in buildings.items()}, ok)
    ground, rule, bounds = Ground.from_project(store), settings(store).ground_rule, {}
    ground_all(store, placed, ground, rule, bounds)          # parents first: their pieces follow them
    kids, kid_entries, missing = assembly.child_placements(store, placed)
    by_key = {p["key"]: p for p in placed}
    kids = [k | {"slot": by_key[k["parent"]]["slot"]} for k in kids]
    props, _, no_model = prop_placements(store, gb)
    ground_all(store, kids + props, ground, rule, bounds)
    drop: dict[str, set] = {}
    for p in props:                               # one model per prop entry: all its pieces, or none
        if not ok(p["entry"]) or replaced.get(p["slot"]) == "whole":
            held.append(p | {"why": "not approved" if not ok(p["entry"]) else "its slot is replaced whole"})
            continue
        placed.append(p)
        drop.setdefault(p["slot"], set()).update(p["pieces"])
        replaced.setdefault(p["slot"], "pieces")
    used = {p["entry"] for p in placed + kids}
    entries = {e: j for e, j in {**kid_entries, **entries}.items() if e in used}
    parents = [p for p in placed if not p.get("prop")]
    return {"placed": placed + kids, "parents": len(parents), "props": len(placed) - len(parents),
            "replaced": replaced, "held": held, "entries": entries, "missing_children": missing,
            "drop_pieces": drop, "no_model": no_model, "bounds": bounds, "ground_rule": rule}


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
        if p.get("typed"):                           # typed builds clear the pieces under their real outline
            feet.setdefault(p["slot"], []).append(p["foot"])
    rows = actor_rows(pl["placed"], slots)
    lib = out / LIB_DIR
    shutil.rmtree(lib, ignore_errors=True)
    lib.mkdir(parents=True)
    own = {}                                     # entries whose every material the importer rebuilds
    for e, j in sorted(pl["entries"].items()):
        spec = ue.library_slots(store.root / j)
        if spec and set(spec) >= set((read_json(store.root / j, default={}) or {}).get("materials", {})):
            own[e] = spec
    assets = {}
    for p in pl["placed"]:
        aid = asset_id(p.get("mesh") or p["key"])
        if aid in assets:
            continue
        dst = lib / f"{aid}.glb"
        src = store.root / p["glb"]
        raw = src.read_bytes()
        # the importer gives these the entry's materials, so their maps go (Unreal would import
        # every one); TRELLIS builds and painted kit pieces keep their GLB's own materials
        dst.write_bytes(strip_textures(raw) if p["entry"] in own else raw)
        assets[aid] = {"id": aid, "kind": "library", "entry": p["entry"], "glb": f"{LIB_DIR}/{aid}.glb",
                       "source": p["glb"], "bounds": pl["bounds"][p["glb"]],
                       "stripped_kb": round((len(raw) - dst.stat().st_size) / 1024)}
        if p["entry"] not in own:                    # its own baked maps go into its material instances
            assets[aid]["baked"] = baked_materials(raw)
    for s in m["slots"]:
        mode = pl["replaced"].get(s["id"])
        if mode is None:
            continue
        s["components"] = manifest_components(kept_pieces(slots[s["id"]], mode, feet.get(s["id"], []),
                                                          pl["drop_pieces"].get(s["id"], ())), index)
        s["library"] = rows.get(s["id"], [])
        s["replaced"] = mode
    m["assets"] = [a for a in m["assets"] if any(a["id"] == c["asset"] for s in m["slots"] for c in s["components"])]
    m["library_assets"] = sorted(assets.values(), key=lambda a: a["id"])
    m["library_entries"] = own
    over = read_json(store.root / MATERIAL_OVERRIDES, default={}) or {}
    m["material_overrides"] = {e: v for e, v in over.items() if not e.startswith("_") and e in {a["entry"] for a in assets.values()}}
    m["source"] = "library+greybox"
    m["terrain"] = terrain_section(gb)
    held_entries = sorted({h["entry"] for h in pl["held"]})
    m["library"] = {"use_active": use_active, "ground_rule": pl["ground_rule"], "placed": len(pl["placed"]), "parents": pl["parents"],
                    "props": pl["props"], "no_model": pl["no_model"],
                    "slots_replaced": len(pl["replaced"]), "held": len(pl["held"]), "held_entries": held_entries,
                    "missing_children": pl["missing_children"]}
    log(f"library: {pl['parents']} builds + {pl['props']} props + {len(pl['placed']) - pl['parents'] - pl['props']} attached pieces in "
        f"{len(pl['replaced'])} slots; {len(pl['held'])} held back as greybox")
    return m["library"]
