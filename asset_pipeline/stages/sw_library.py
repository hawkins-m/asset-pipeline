"""World mode, phase 3 step 1: the asset library plan (CPU only, no generation).

Every locked shot frame is matched against its shot's object-id pass (sw_shots: one colour
per greybox slot), so each visible slot knows which frame shows it and where. Slots are
grouped into library entries by what they are (their greybox tags):

  A  landmarks and kit monuments: a catalog typology with a reference image (one entry per
     part of the ensemble: rotunda, its colonnades), and the kit types the civic cores
     place (temple, stoa, cloister...).
  B  singular civic and infrastructure typologies: small max_count, or a single-building
     form (hall, drum, cavea, tower, pavilion...); the enclosure's wall and gates.
  C  fill typologies (housing, mixed): one entry per typology x material, since each is
     built as a facade kit later (PLAN.md, Phase 3).
  D  props and vegetation inside visible slots (market stalls, trees by species), from
     the slots' pieces; their share is their slot's, so it is approximate.

Ground and water slots (streets, plazas, parks, terrain, pools) are surfaces: materials in
the engine, not assets. They're counted, not listed as entries.

Sources: the locked frame of every locked shot that has one, plus that shot's frames
marked as shot frames (role "source"), but only frames that still fit the current
greybox (`frame_fit`: an old-layout frame no longer lines up with the id pass, so its
crops show whatever the old layout had there). Mood shots (MOOD_TIERS) never seed
assets; their frames are kept per entry as `mood` references.

Checks (`check`, SAM 3.1 on ComfyUI): trees, sky, buildings and the entry's own noun are
segmented on each source frame (masks cached under site/library/sam/), and each entry's
id-pass area is scored: how much of it the frame actually fills with the asset, and how
much with trees or sky. The plan turns those into flags (a crop mostly trees, sky or
something else; a small one) and picks each entry's best source by share x asset cover.
The user decides per entry in the Library tab (site/library_review.json, kept across
re-plans): good source / use description / exclude.

Writes site/library.json, a crop per entry (site/library/crops/<id>.jpg: the best source
frame around the entry's slots, everything else dimmed) and site/library.html (the list
grouped by district and typology).
"""
import html
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance

from .. import crop_refine, review
from ..project import ProjectStore, read_json, write_json
from . import s0_frames, sw_shots, sw_site

LIBRARY_DIR = "library"
MIN_SHARE = 0.005            # an entry needs this share of some source frame to have a usable crop

SURFACE_TYPES = {"terrain", "sea", "street", "avenue", "radial", "plaza", "ring", "lane", "stair", "park",
                 "garden", "canal", "terrace", "pool", "lagoon"}
KIT_TYPES = {"temple", "stoa", "cloister", "colonnade", "rotunda", "portico", "forum_gate", "fountain", "villa"}
SINGULAR_FORMS = {"hall", "drum", "cavea", "cube", "tower", "podium_tower", "pavilion", "arcade_line", "open"}
SINGULAR_FAMILIES = {"civic", "infrastructure", "public_realm"}
ENCLOSURE_TYPES = {"wall", "gate", "quay"}
# props: greybox piece name -> library noun (pieces of one prop share a noun)
PROP_PIECES = {"market:hut": "market hut", "market:hut-roof": "market hut", "market:tent": "market tent",
               "market:canopy": "canopy table", "market:canopy-post": "canopy table", "market:table": "canopy table",
               "market:stall": "market stall"}
TREE_RE = re.compile(r"^tree:(\w+?)(-trunk|-crown)?$")
# SAM 3.1 nouns run on every source frame (check): what an entry's area may be filled with
CHECK_NOUNS = ("tree", "sky", "building", "colonnade", "arch", "market stall", "tent", "canopy")
ASSET_NOUNS = {"market hut": ("market stall",), "market tent": ("tent", "market stall"),
               "canopy table": ("canopy", "market stall"), "market stall": ("market stall",)}
BUILDING_NOUNS = ("building", "colonnade", "arch")
FLAG_TREES, FLAG_SKY, FLAG_ASSET, FLAG_SMALL = 0.35, 0.2, 0.4, 0.01
# source: the crop guides its sheets; reference: description plus a chosen frame (any frame,
# old layouts too) as a look reference, no crop; description: description only
# kit: a facade kit (bay / corner modules, each with its own description and size) drawn
# with a look image, tiled along the greybox footprints later
DECISIONS = ("source", "description", "reference", "kit", "exclude")
# Redux total for the look ref (scene frames leak content). A prop crop at 0.06 copied the
# crop's shape (the pilot hut came out as the canopy stands around it): props get 0.03.
REF_STRENGTH = {"source": 0.06, "reference": 0.04, "kit": 0.05}
PROP_SOURCE_STRENGTH = 0.03


def ref_strength(decision: str, tier: str) -> float:
    return PROP_SOURCE_STRENGTH if decision == "source" and tier == "D" else REF_STRENGTH.get(decision, 0.0)
# generic descriptions for kit monuments and props (catalog typologies carry their own `desc`)
KIT_DESC = {
    "temple": "A classical temple on a high stepped podium: a deep front portico of tall fluted columns under a "
              "triangular pediment, plain cella walls behind",
    "stoa": "A stoa: a long open colonnaded hall, one row of tall columns in front of a solid back wall, a deep "
            "shaded walk, a flat roof behind a plain entablature",
    "cloister": "One range of an arcaded courtyard: a two-storey block whose ground floor opens to the court in "
                "a long arcade of round arches on square piers, a roofed walk behind the arcade, a cornice on top",
    "colonnade": "A curved double colonnade: two rows of tall columns under a continuous entablature",
    "portico": "One range of an enclosed forum: two storeys of colonnades facing the square (a tall lower "
               "order, a lighter upper gallery, an attic), shops behind the colonnades",
    "forum_gate": "A monumental gate arch: one deep round-arched passage through a massive block, an attic on "
                  "top, set into a colonnaded range",
    "fountain": "A round fountain basin with a statue on a tall plinth in its middle",
    "villa": "A courtyard villa with a pool",
}
PROP_DESC = {
    "market hut": "A small closed market hut of timber boards with a pitched roof, a wide hinged sales hatch "
                  "and counter on the front, small enough for one vendor",
    "market tent": "A small private market tent: a steep A-frame canvas roof on a light frame, one open side",
    "canopy table": "An open market stand: a flat canopy on four slim posts over a long table",
    "market stall": "A small open market stall with a counter and an awning",
}
# props' sizes, m (w, d, h): as city.forum_market draws them
PROP_DIMS = {"market hut": (3.0, 2.4, 2.3), "market tent": (3.0, 3.0, 2.8), "canopy table": (4.4, 3.2, 2.7),
             "market stall": (5.0, 3.5, 3.0)}
# Build split (PLAN.md "Build split"): architecture is built procedurally in Blender from a brief
# measured on its sheet; irregular assets (rotunda-type landmarks, props, ornament) go to TRELLIS.
ROUTES = ("procedural", "trellis")
TRELLIS_FORMS = {"drum"}             # rotunda-type landmarks; every other catalog form is procedural
TRELLIS_PARTS = {"rotunda", "fountain", "statue", "nymphaeum", "sculpture", "obelisk"}


def route_auto(e: dict, typ=None) -> str | None:
    """An entry's default build route; None for vegetation (Unreal foliage, not built).
    Architecture is procedural: kit pieces (even of an irregular ensemble, like a rotunda's
    colonnade), catalog forms other than drums, and the greybox's own structures (walls,
    gates, quays, blocks). TRELLIS: props, ornament and drum-type landmarks."""
    if e["kind"] == "vegetation":
        return None
    if e["tier"] == "D" or e["part"] in TRELLIS_PARTS:
        return "trellis"
    if e["part"] in KIT_DESC:
        return "procedural"
    if e["typology"] in TRELLIS_PARTS:
        return "trellis"
    if typ is None:
        return "procedural"
    return "trellis" if typ.form in TRELLIS_FORMS else "procedural"


def route(entry: dict, decision: dict | None) -> str | None:
    """The effective route: the user's choice, else the default."""
    return (decision or {}).get("route") or entry.get("route_auto")


TIERS = {"A": "landmarks and kit monuments", "B": "singular civic and infrastructure", "C": "fill typologies",
         "D": "props and vegetation"}


def library_path(store: ProjectStore) -> Path:
    return sw_site.site_dir(store) / "library.json"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


FIT_RATIO = 0.9             # a frame's edge match now / when generated: below, the greybox changed in its view
FIT_MIN = 0.25              # and a frame that never followed its greybox (scale-model raised views) is out too


def frame_fit(store: ProjectStore, key: str) -> dict:
    """Does a frame still match its shot's current greybox? Its edge match against the
    current canny pass, against the one recorded when it was generated. A rebuild that
    changed what the shot sees drops it; one that changed only things out of view (or
    small ones: tree crowns) doesn't, so such a frame still lines up with the id pass."""
    shot = key.split("/")[1]
    meta = read_json(store.root / Path(key).parent / "meta.json", default={}) or {}
    rec = next((f.get("edge_match") for f in meta.get("frames", []) if f["file"] == Path(key).name), None)
    now = s0_frames.edge_match(store.root / key, sw_shots.shot_dir(store, shot) / "canny.png")
    if rec is None:
        ok, why = False, "no recorded layout match (not a pipeline frame)"
    elif now < FIT_RATIO * rec:
        ok, why = False, f"old layout: the greybox changed in this view (match {rec:.2f} then, {now:.2f} now)"
    elif now < FIT_MIN:
        ok, why = False, f"doesn't follow its greybox (match {now:.2f})"
    else:
        ok, why = True, ""
    return {"frame": key, "shot": shot, "recorded": rec, "now": round(now, 4), "ok": ok, "reason": why}


def candidates(store: ProjectStore) -> list[dict]:
    """Every frame that could seed assets, with its fit: the locked frame of each locked
    shot (not greybox-locked, not mood) and that shot's frames marked as shot frames."""
    mood = s0_frames.mood_shots(store)
    roles = review.roles(store)
    out = []
    for shot, key in s0_frames.locked(store).items():
        if key == s0_frames.LOCK_GREYBOX or shot in mood:
            continue
        keys = [key] + [f["key"] for b in s0_frames.batches(store, shot) for f in b["frames"]
                        if f["key"] != key and roles.get(f["key"]) == "source"]
        for k in keys:
            out.append(frame_fit(store, k) | {"locked": k == key})
    return out


def sources(store: ProjectStore, cands: list[dict] | None = None) -> dict[str, list[str]]:
    """shot -> its frames that seed assets: candidates that still fit the current greybox
    (the locked frame first)."""
    out: dict[str, list[str]] = {}
    for c in cands if cands is not None else candidates(store):
        if c["ok"]:
            out.setdefault(c["shot"], []).append(c["frame"])
    return out


def mood_frames(store: ProjectStore) -> dict[str, str]:
    """Mood shot -> its locked frame (look only), when it still fits its greybox."""
    mood = s0_frames.mood_shots(store)
    return {s: k for s, k in s0_frames.locked(store).items()
            if s in mood and k != s0_frames.LOCK_GREYBOX and frame_fit(store, k)["ok"]}


def _material_of(slot: dict, layout) -> str | None:
    if slot.get("material"):
        return slot["material"]
    for m in layout.materials:
        if slot["type"] in m.types and (not m.districts or slot.get("district") in m.districts):
            return m.id
    return None


def classify(slot: dict, layout, catalog: dict) -> tuple[str, str, str | None] | None:
    """(tier, typology-or-type, material | None) of a building-like slot; None for surfaces
    and prop holders (their pieces are classified separately)."""
    typ, ty = slot["type"], slot.get("typology")
    if typ in SURFACE_TYPES or typ == "market":
        return None
    t = catalog.get(ty) if ty else catalog.get(typ)
    if ty and t is not None and t.ref:
        return "A", ty, _material_of(slot, layout)
    if typ in KIT_TYPES:
        return "A", typ, _material_of(slot, layout)
    if typ in ENCLOSURE_TYPES:
        return "B", typ, _material_of(slot, layout)
    if t is not None and (t.max_count is not None and t.max_count <= 14 or t.form in SINGULAR_FORMS
                          or t.family in SINGULAR_FAMILIES):
        return "B", t.id, _material_of(slot, layout)
    return "C", ty or typ, _material_of(slot, layout)


def _entry_id(tier: str, group: str, part: str, material: str | None) -> str:
    if tier == "C":
        return _slug(f"{group}-{material or 'any'}")
    if tier == "D":
        return _slug(part)
    if part == group:
        return _slug(group)
    last = group.split("_")[-1]            # market_forum + forum_gate -> market-forum-gate
    return _slug(f"{group}-{part[len(last) + 1:] if part.startswith(last + '_') else part}")


def _props_of(slot: dict) -> Counter:
    """Library nouns of the props in a slot (one per prop instance, not per piece)."""
    out = Counter()
    for p in slot["pieces"]:
        name = p["piece"]
        if name in PROP_PIECES:
            noun = PROP_PIECES[name]
            # count each prop once: by its main piece
            if name in ("market:hut", "market:tent", "market:canopy", "market:stall"):
                out[noun] += 1
            continue
        m = TREE_RE.match(name)
        if m and m.group(2) in (None, "-crown"):
            out[f"{m.group(1)} tree"] += 1
    return out


def plan(store: ProjectStore, min_share: float = MIN_SHARE) -> dict:
    """Build the library plan from the locked shot frames; writes library.json, the crops
    and library.html. Returns the plan."""
    layout = sw_site.load_layout(store)
    gb = sw_site.load_greybox(store)
    slots = {s["id"]: s for s in gb["slots"]}
    catalog = {t.id: t for t in (layout.city.typologies if layout.city else [])}
    mats = {m.id: m for m in layout.materials}
    cands = candidates(store)
    src = sources(store, cands)
    moods = mood_frames(store)
    idcache: dict[str, tuple] = {}
    tiers = {s["id"]: s["tier"] for s in gb["shots"]}

    entries: dict[str, dict] = {}
    slot_entry: dict[str, list[str]] = defaultdict(list)    # slot -> entries it belongs to

    def entry(tier, group, part, material, kind):
        eid = _entry_id(tier, group, part, material)
        if eid not in entries:
            entries[eid] = {"id": eid, "tier": tier, "kind": kind, "typology": group, "part": part,
                            "material": material, "instances": 0, "districts": Counter(), "slots": [],
                            "sources": [], "mood": [], "best": None, "share": 0.0}
        return entries[eid]

    # every instance in the greybox, so counts cover what no frame shows too
    for s in gb["slots"]:
        c = classify(s, layout, catalog)
        if c:
            tier, group, mat = c
            part = s["type"] if tier == "A" and s.get("typology") and s["type"] != s.get("typology") else group
            e = entry(tier, group, part, mat if tier in ("A", "C") else None, "building" if s["category"] ==
                      "building" else "structure")
            e["instances"] += 1
            e["districts"][s.get("district") or "-"] += 1
            e["slots"].append(s["id"])
            if tier == "B" and mat:
                e.setdefault("materials", Counter())[mat] += 1
            slot_entry[s["id"]].append(e["id"])
        for noun, n in _props_of(s).items():
            kind = "vegetation" if noun.endswith(" tree") else "prop"
            e = entry("D", noun, noun, None, kind)
            e["instances"] += n
            e["districts"][s.get("district") or "-"] += n
            slot_entry[s["id"]].append(e["id"])

    surfaces = Counter()
    locks = s0_frames.locked(store)
    for shot, keys in src.items():
        ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json", default=None)
        if not ids:
            raise FileNotFoundError(f"shot {shot} has no id pass: ap shots render first")
        per_entry: dict[str, dict] = {}
        for sid, v in ids["slots"].items():
            s = slots.get(sid)
            if s is None:
                continue
            if s["type"] in SURFACE_TYPES:
                surfaces[s["type"]] += v["pixels"]
            for eid in slot_entry.get(sid, []):
                pe = per_entry.setdefault(eid, {"share": 0.0, "top": 0.0, "bbox": None, "slots": [],
                                                "districts": Counter()})
                pe["share"] += v["frac"]
                if v["frac"] > pe["top"]:          # the crop frames the largest instance on screen
                    pe["top"], pe["bbox"] = v["frac"], v["bbox"]
                pe["slots"].append(sid)
                pe["districts"][s.get("district") or "-"] += v["pixels"]
        for eid, pe in per_entry.items():
            area = _entry_mask(store, shot, pe["slots"], idcache) if pe["share"] >= min_share else None
            for k in keys:
                rec = {"frame": k, "shot": shot, "tier": tiers.get(shot), "share": round(pe["share"], 5),
                       "bbox": [round(x, 4) for x in pe["bbox"]], "slots": sorted(pe["slots"]),
                       # the shot's own lock, not the first usable frame: a lock that predates
                       # the layout is dropped from keys, and its first source frame isn't locked
                       "locked": k == locks.get(shot), "districts": dict(pe["districts"]),
                       "scores": _scores(store, k, area, entries[eid]) if area is not None else None}
                ref = read_json(_refined_path(store, eid, k).with_suffix(".json"), default=None)
                if ref and entries[eid]["tier"] != "D":
                    rec["kept"], rec["refined"] = ref["kept"], ref["instance"]
                entries[eid]["sources"].append(rec)
    for shot, k in moods.items():
        ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json", default=None) or {"slots": {}}
        seen = Counter()
        for sid, v in ids["slots"].items():
            for eid in slot_entry.get(sid, []):
                seen[eid] += v["frac"]
        for eid, share in seen.items():
            if share >= min_share:
                entries[eid]["mood"].append({"frame": k, "shot": shot, "share": round(share, 5)})

    out_entries, unseen = [], []
    for e in entries.values():
        # best source: among those big enough, the most screen share discounted by how little
        # of it is the asset
        e["sources"].sort(key=lambda r: (r["share"] < min_share, -r["share"] * _cover(r), -r["share"],
                                         not r["locked"]))
        if e["sources"] and e["sources"][0]["share"] >= min_share:
            best = e["sources"][0]
            e["best"], e["share"] = best["frame"], best["share"]
            e["scores"], e["kept"] = best["scores"], best.get("kept")
            vis = Counter()
            for r in e["sources"]:
                vis.update(r["districts"])
            e["district"] = vis.most_common(1)[0][0]
        else:
            e["district"] = e["districts"].most_common(1)[0][0] if e["districts"] else "-"
        e["districts"] = dict(e["districts"].most_common())
        if "materials" in e:
            e["materials"] = dict(e["materials"].most_common())
        e["size_m"] = _size(e, slots, catalog.get(e["typology"]), layout.city.storey_m if layout.city else 3.4)
        e["flags"] = flags(e, min_share)
        e["description_auto"] = _description(e, catalog, mats)
        e["route_auto"] = route_auto(e, catalog.get(e["typology"]))
        e["description"] = None
        e["slot_ids"] = sorted(e.pop("slots"))
        (out_entries if e["best"] else unseen).append(e)
    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    out_entries.sort(key=lambda e: (e["district"], order[e["tier"]], e["typology"], -e["share"]))
    unseen.sort(key=lambda e: (order[e["tier"]], e["typology"], -e["instances"]))
    data = {"min_share": min_share, "sources": src, "mood": moods, "candidates": cands,
            "checked": all(_sam_mask(store, k, "tree") is not None for ks in src.values() for k in ks),
            "entries": out_entries,
            "unseen": [{k: e[k] for k in ("id", "tier", "kind", "typology", "part", "material", "instances",
                                          "districts", "district", "mood", "flags", "description_auto", "size_m",
                                          "route_auto", "slot_ids")}
                       | {"share": e["sources"][0]["share"] if e["sources"] else 0.0} for e in unseen],
            "surfaces": {k: v for k, v in surfaces.most_common()}}
    _auto_decide(store, data["entries"])
    lib = sw_site.site_dir(store) / LIBRARY_DIR
    _crops(store, data["entries"], lib / "crops")
    write_json(library_path(store), data)
    (sw_site.site_dir(store) / "library.html").write_text(report_html(store, data))
    return data


# a fill entry's library asset is one representative building: a row form is drawn as a run
# of this many houses, a perimeter block as a square courtyard block
ROW_UNITS = 4


def _size(e: dict, slots: dict, typ=None, storey_m: float = 3.4) -> list[float]:
    """(width, depth, height) m: props from their drawn size; fill typologies (tier C) from
    the catalog's middle sizes (their slots hold a whole block of buildings); others from the
    instance whose world bounding box is the smallest footprint (the least rotated: an AABB
    grows with the rotation), width the longer side."""
    if e["tier"] == "D":
        return list(PROP_DIMS.get(e["part"], (1.0, 1.0, 1.0)))
    if e["tier"] == "C" and typ is not None and typ.width and typ.depth and typ.storeys:
        w, d = sum(typ.width) / 2, sum(typ.depth) / 2
        h = round(sum(typ.storeys) / 2) * storey_m
        if typ.form == "row":
            w = ROW_UNITS * w
        elif typ.form == "perimeter":
            d = w
        return [round(max(w, d), 2), round(min(w, d), 2), round(h, 2)]
    best = None
    for sid in e["slots"]:
        (x0, y0, z0), (x1, y1, z1) = slots[sid]["bbox"]
        dims = (max(x1 - x0, y1 - y0), min(x1 - x0, y1 - y0), z1 - z0)
        if best is None or dims[0] * dims[1] < best[0] * best[1]:
            best = dims
    return [round(min(max(v, 0.1), 1000.0), 2) for v in best] if best else [1.0, 1.0, 1.0]


def _sam_path(store: ProjectStore, frame: str, noun: str) -> Path:
    return sw_site.site_dir(store) / LIBRARY_DIR / "sam" / f"{_slug(frame)}__{_slug(noun)}.png"


def _sam_mask(store: ProjectStore, frame: str, noun: str) -> np.ndarray | None:
    p = _sam_path(store, frame, noun)
    return np.asarray(Image.open(p).convert("L")) > 127 if p.is_file() else None


def _entry_mask(store: ProjectStore, shot: str, slot_ids: list[str], cache: dict) -> np.ndarray:
    """The pixels of these slots in the shot's id pass."""
    if shot not in cache:
        ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json")
        rgb = np.asarray(Image.open(sw_shots.shot_dir(store, shot) / "ids.png").convert("RGB"))
        cache[shot] = sw_shots.decode_ids(rgb, {k: v["color"] for k, v in ids["slots"].items()})[:2]
    idx, names = cache[shot]
    return np.isin(idx, [names.index(s) for s in slot_ids if s in names])


def _scores(store: ProjectStore, frame: str, area: np.ndarray, e: dict) -> dict | None:
    """What fills the entry's area in a frame (SAM masks from `check`): the asset itself,
    trees, sky, as shares of the area. None until checked."""
    n = int(area.sum())
    if not n:
        return None
    masks = {}
    for noun in CHECK_NOUNS:
        m = _sam_mask(store, frame, noun)
        if m is None:
            return None
        if m.shape != area.shape:
            m = np.asarray(Image.fromarray(m.astype(np.uint8) * 255).resize(area.shape[::-1])) > 127
        masks[noun] = m
    veg = e["kind"] == "vegetation"
    own = ("tree",) if veg else ASSET_NOUNS.get(e["part"], BUILDING_NOUNS)
    asset = np.logical_or.reduce([masks[k] for k in own])
    trees = masks["tree"] & ~asset if not veg else np.zeros_like(asset)
    sky = masks["sky"] & ~asset
    return {"asset": round(float((asset & area).sum()) / n, 3), "trees": round(float((trees & area).sum()) / n, 3),
            "sky": round(float((sky & area).sum()) / n, 3)}


def _cover(r: dict) -> float:
    if r.get("kept") is not None:            # refined (check): the share of the instance drawn as it
        return r["kept"]
    return r["scores"]["asset"] if r.get("scores") else 1.0


def flags(e: dict, min_share: float = MIN_SHARE) -> list[str]:
    """Automatic warnings on an entry's crop, for the user's review."""
    out = []
    if e.get("kind") == "vegetation":
        out.append("Unreal foliage: not generated")
    if not e.get("best"):
        out.append("not visible in any current shot frame")
        return out
    kept = e.get("kept")
    if kept is not None and kept < crop_refine.USABLE:
        out.append(f"no usable crop: only {100 * kept:.0f}% of its greybox area is drawn as the building")
    sc = e.get("scores")
    if sc:
        if sc["trees"] >= FLAG_TREES:
            out.append(f"trees cover {100 * sc['trees']:.0f}% of the asset area")
        if sc["sky"] >= FLAG_SKY:
            out.append(f"sky covers {100 * sc['sky']:.0f}% of the asset area")
        rest = 1 - sc["asset"] - sc["trees"] - sc["sky"]
        if sc["asset"] < FLAG_ASSET:
            what = "something else" if rest >= max(sc["trees"], sc["sky"]) else "trees or sky"
            out.append(f"only {100 * sc['asset']:.0f}% of the area is drawn as the asset ({what} fills the rest)")
    else:
        out.append("not checked yet (ap library check)")
    if e["share"] < FLAG_SMALL:
        out.append(f"small in its frame ({100 * e['share']:.1f}%)")
    if e["tier"] == "D":
        out.append("approximate crop: the id pass shows the whole slot holding it")
    return out


def _refined_path(store: ProjectStore, eid: str, frame: str) -> Path:
    return sw_site.site_dir(store) / LIBRARY_DIR / "refined" / f"{eid}__{_slug(frame)}.png"


def _auto_decide(store: ProjectStore, entries: list[dict]) -> None:
    """Cards with no usable crop default to Use description (marked auto, so a later check
    that finds a usable crop takes it back); the user's own decisions are never touched."""
    d = decisions(store)
    changed = False
    for e in entries:
        bad = e.get("kept") is not None and e["kept"] < crop_refine.USABLE
        cur = d.get(e["id"], {})
        if bad and not cur.get("decision"):
            d[e["id"]] = cur | {"decision": "description", "auto": True, "note": "no usable crop"}
            changed = True
        elif not bad and cur.get("auto"):
            d[e["id"]] = {k: v for k, v in cur.items() if k not in ("decision", "auto", "note")}
            if not d[e["id"]]:
                d.pop(e["id"])
            changed = True
    if changed:
        write_json(decisions_path(store), d)


def refine_sources(store: ProjectStore, client, data: dict, log=None) -> int:
    """Refined instance masks (crop_refine) for every building-like entry's sources big
    enough to crop, cached under site/library/refined/. Returns how many were made."""
    seg = sw_site.site_dir(store) / LIBRARY_DIR / "seg"
    frames = sorted({r["frame"] for e in data["entries"] if e["tier"] != "D" for r in e["sources"]
                     if r["share"] >= data["min_share"]})
    crop_refine.dump(store.root, frames, seg, log=log)
    cache, n = {}, 0
    for e in data["entries"]:
        if e["tier"] == "D":
            continue
        for r in e["sources"]:
            if r["share"] < data["min_share"]:
                continue
            out = _refined_path(store, e["id"], r["frame"])
            if out.with_suffix(".json").is_file():
                continue
            sd = sw_shots.shot_dir(store, r["shot"])
            ids = read_json(sd / "ids.json")
            top = max(r["slots"], key=lambda sid: ids["slots"][sid]["frac"])      # the largest instance
            frame = store.root / r["frame"]
            size = Image.open(frame).size
            R = _entry_mask(store, r["shot"], [top], cache)
            if R.shape[::-1] != size:
                R = np.asarray(Image.fromarray(R.astype(np.uint8) * 255).resize(size)) > 127
            m = crop_refine.refine(frame, seg, r["frame"], R, crop_refine.greybox_depth(sd, size), client)
            out.parent.mkdir(parents=True, exist_ok=True)
            Image.fromarray(m["S"].astype(np.uint8) * 255).save(out)
            kept = round(float((m["S"] & R).sum()) / max(int(R.sum()), 1), 3)   # SAM may reach past R
            write_json(out.with_suffix(".json"), {"frame": r["frame"], "instance": top, "kept": kept,
                                                 "labels": round(float(m["L"].sum()) / max(int(R.sum()), 1), 3),
                                                 "depth": round(float(m["D"].sum()) / max(int(R.sum()), 1), 3)})
            n += 1
    return n


def check(store: ProjectStore, client=None, min_share: float = MIN_SHARE, progress=None) -> dict:
    """Segment trees, sky, buildings and the props' nouns with SAM 3.1 on every source frame
    (ComfyUI, GPU 1; cached per frame and noun), then re-plan with the flags."""
    from ..comfy.client import connect
    from ..segment import detect
    client = client or connect()
    frames = [k for ks in sources(store).values() for k in ks]
    todo = [(k, n) for k in frames for n in CHECK_NOUNS if not _sam_path(store, k, n).is_file()]
    for i, (k, noun) in enumerate(todo):
        size = Image.open(store.root / k).size
        dets = detect(client, store.root / k, noun, max_dets=32 if noun != "sky" else 2, threshold=0.4)
        m = np.zeros(size[::-1], bool)
        for d in dets:
            m |= d.mask
        p = _sam_path(store, k, noun)
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(m.astype(np.uint8) * 255).save(p)
        if progress:
            progress(i + 1, len(todo))
    data = plan(store, min_share)
    if crop_refine.available():          # labels + depth + SAM box: the refined crops
        refine_sources(store, client, data)
        data = plan(store, min_share)
    return data


def decisions_path(store: ProjectStore) -> Path:
    return sw_site.site_dir(store) / "library_review.json"


def decisions(store: ProjectStore) -> dict[str, dict]:
    """Per entry id: the user's decision, description, route. Keys starting with "_" are not
    entries: "_reviews" holds the build reviews per asset id (library_review.py)."""
    return read_json(decisions_path(store), default={}) or {}


def describe(store: ProjectStore, entry: str, text: str | None) -> dict:
    """The user's description for an entry (None restores the auto one); kept whatever the
    decision."""
    _check_entry(store, entry)
    d = decisions(store)
    if text:
        d.setdefault(entry, {})["description"] = text.strip()
    elif entry in d:
        d[entry].pop("description", None)
        if not d[entry]:
            d.pop(entry)
    write_json(decisions_path(store), d)
    return d


def set_route(store: ProjectStore, entry: str, value: str | None) -> dict:
    """The user's build route for an entry (procedural / trellis); None restores the default."""
    if value is not None and value not in ROUTES:
        raise ValueError(f"route must be one of {ROUTES}")
    _check_entry(store, entry)
    d = decisions(store)
    if value:
        d.setdefault(entry, {})["route"] = value
    elif entry in d:
        d[entry].pop("route", None)
        if not d[entry]:
            d.pop(entry)
    write_json(decisions_path(store), d)
    return d


def district_frame(store: ProjectStore, district: str | None) -> str | None:
    """The approved look frame of a district: its eye-level medium shot's locked frame
    (med-<district>), else None (greybox-locked or no such shot)."""
    layout = sw_site.load_layout(store)
    for sh in layout.shots:
        if sh.id == f"med-{district}" and sh.locked and sh.locked != s0_frames.LOCK_GREYBOX:
            return sh.locked
    return None


def _check_entry(store: ProjectStore, entry: str) -> None:
    data = load(store)
    ids = {e["id"] for e in (data or {}).get("entries", [])} | {u["id"] for u in (data or {}).get("unseen", [])}
    if entry not in ids:
        raise KeyError(f"no library entry {entry}")


def decide(store: ProjectStore, entry: str, decision: str | None, note: str | None = None,
           ref_frame: str | None = None, pieces: dict | None = None) -> dict:
    """The user's call on an entry: source (its crop is a good visual source), description
    (build it from its description only), reference (description plus `ref_frame`, any
    project frame, as a look reference), exclude (no asset); None clears it."""
    from datetime import datetime, timezone
    if decision is not None and decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if decision == "kit":
        if not pieces:
            raise ValueError("a kit decision needs pieces: {name: {description, size: [w, d, h]}}")
        for name, pc in pieces.items():
            if not re.fullmatch(r"[a-z0-9-]+", name) or not pc.get("description") or len(pc.get("size", [])) != 3:
                raise ValueError(f"kit piece {name!r}: a lowercase name, a description and size [w, d, h]")
    if decision in ("reference", "kit") and ref_frame or decision == "reference":
        if not ref_frame:
            raise ValueError("a reference decision needs a frame")
        try:
            ref_frame = review.rel(store, ref_frame)
        except FileNotFoundError:
            raise ValueError(f"no such image in the project: {ref_frame}") from None
        if not (store.root / ref_frame).is_file() or Path(ref_frame).suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError(f"no such image in the project: {ref_frame}")
    _check_entry(store, entry)
    d = decisions(store)
    keep = {k: v for k, v in d.get(entry, {}).items() if k in ("description", "route", "name")}   # the user's text and route stay
    if decision is None:
        d.pop(entry, None)
        if keep:
            d[entry] = keep
    else:
        d[entry] = {"decision": decision, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")} | keep
        if ref_frame and decision in ("reference", "kit"):
            d[entry]["frame"] = ref_frame
        if pieces and decision == "kit":
            d[entry]["pieces"] = pieces
        if note:
            d[entry]["note"] = note
    write_json(decisions_path(store), d)
    return d


PLAN_NAME = "library"
CATEGORY = {"building": "building", "structure": "structure", "prop": "prop", "vegetation": "vegetation"}


def asset_plan(store: ProjectStore, only: list[str] | None = None):
    """The decided entries as an AssetPlan named `library` (plans/library.json), so stages
    2-6 run on them unchanged: source -> the clean crop as look reference, reference -> the
    chosen frame, description -> none. Undecided entries stay greybox massing; excluded and
    vegetation (Unreal foliage) aren't in it. `only`: just these entries are included (a
    pilot); the others are listed but not generated."""
    from ..schema import AssetPlan, Dimensions, PlanAsset
    from . import s1_plan
    data = load(store)
    if data is None:
        raise FileNotFoundError("no library plan yet: ap library plan")
    dec = decisions(store)
    entries = {e["id"]: e for e in data["entries"]} | {u["id"]: u for u in data["unseen"]}
    missing = [i for i in only or [] if i not in entries]
    if missing:
        raise KeyError(f"no library entries {missing}")
    undecided = [i for i in only or [] if dec.get(i, {}).get("decision") in (None, "exclude")]
    if undecided:
        raise ValueError(f"not decided (or excluded): {undecided}")
    assets = []
    for eid, d in dec.items():
        e = entries.get(eid)
        if eid.startswith("_") or e is None or d.get("decision") in (None, "exclude") or e.get("kind") == "vegetation":
            continue
        inc = only is None or eid in only
        if d["decision"] == "kit":
            # one asset per module, each sheeted alone (trim matches through the shared look
            # ref). Flux draws a small module (a two-column stoa bay) as asked, but a multi-storey
            # bay, alone or on a kit sheet, comes out as a whole building.
            for name, pc in d["pieces"].items():
                w, dp, h = pc["size"]
                assets.append(PlanAsset(
                    id=f"{eid}-{name}", name=f"{entry_name(e, d)} {name}", noun=pc.get("noun") or "building",
                    category="structure", description=pc["description"], count=1,
                    dimensions=Dimensions(width=w, depth=dp, height=h), placement=f"kit module of {eid}",
                    include=inc, refs=[d["frame"]] if d.get("frame") else [],
                    ref_strength=ref_strength("kit", e["tier"]), route=d.get("route") or "procedural"))
            continue
        refs = {"source": [f"site/{LIBRARY_DIR}/refs/{eid}.jpg"], "reference": [d.get("frame")],
                "description": []}[d["decision"]]
        if d["decision"] == "source" and not e.get("best"):
            raise ValueError(f"{eid}: marked good source but no current frame shows it")
        w, dp, h = e.get("size_m") or [1.0, 1.0, 1.0]
        assets.append(PlanAsset(
            id=eid, name=entry_name(e, d), noun=e["part"].replace("_", " "),
            category=CATEGORY.get(e["kind"], "structure"),
            description=d.get("description") or e.get("description") or e["description_auto"],
            count=max(1, e["instances"]), dimensions=Dimensions(width=w, depth=dp, height=h),
            placement=f"{e['tier']} · {e.get('district') or '-'}", include=inc,
            refs=[r for r in refs if r], ref_strength=ref_strength(d["decision"], e["tier"]),
            route=route(e, d) or "trellis"))
    plan = AssetPlan(scene=f"site/{library_path(store).name}", summary="world-mode asset library (sw_library)",
                     assets=assets, llm="")
    return s1_plan.save(store, PLAN_NAME, plan)


def entry_name(e: dict, d: dict | None) -> str:
    """An entry's current display name: the user's (decisions), else the entry's, else its id."""
    return (d or {}).get("name") or e.get("name") or e["id"].replace("-", " ")


def asset_names(store: ProjectStore) -> dict[str, str]:
    """{asset id: display name} from each entry's current name: the entry itself, and its kit
    modules as "<entry name> <piece>". Every UI label of a library asset comes from here."""
    data = load(store) or {}
    dec = decisions(store)
    out = {}
    for e in data.get("entries", []) + data.get("unseen", []):
        d = dec.get(e["id"]) or {}
        out[e["id"]] = n = entry_name(e, d)
        for piece in d.get("pieces") or {}:
            out[f"{e['id']}-{piece}"] = f"{n} {piece}"
    return out


def refresh_plan_names(store: ProjectStore) -> int:
    """Write the entries' current names into plans/library.json (Reference sheets, Views and 3D
    read them there) without re-planning. Returns how many changed."""
    from . import s1_plan
    plan = s1_plan.load(store, PLAN_NAME)
    if plan is None:
        return 0
    names, n = asset_names(store), 0
    for a in plan.assets:
        if names.get(a.id) and a.name != names[a.id]:
            a.name, n = names[a.id], n + 1
    if n:
        s1_plan.save(store, PLAN_NAME, plan)
    return n


def _description(e: dict, catalog: dict, mats: dict) -> str:
    """The auto description that will drive the entry's reference sheets (step 2 refines it)."""
    t = catalog.get(e["typology"])
    base = (t.desc or t.prompt) if t else ""
    if e["tier"] == "A" and e["part"] in KIT_DESC and (e["part"] != e["typology"] or not base):
        base = KIT_DESC[e["part"]] + (f". Part of: {base}" if base else "")
    elif e["tier"] == "D" and e["part"] in PROP_DESC:
        base = PROP_DESC[e["part"]]
    if not base:
        base = {"A": f"a {e['part'].replace('_', ' ')}", "B": f"a {e['typology'].replace('_', ' ')}",
                "C": f"a {e['typology'].replace('_', ' ')} building", "D": f"a {e['part']}"}[e["tier"]]
    if e["tier"] == "A" and e["part"] != e["typology"] and e["part"] not in KIT_DESC:
        base = f"{e['part'].replace('_', ' ')} of: {base}"
    m = mats.get(e.get("material") or "")
    return f"{base} Material: {m.words}." if m else base


def _crops(store: ProjectStore, entries: list[dict], out: Path) -> None:
    """For each entry: its best source frame cropped around its largest instance on screen,
    the entry's slots bright (id-pass pixels) and everything else dimmed. Props and trees
    are pieces inside a slot, which the id pass doesn't separate: their crop shows the
    whole slot (approximate)."""
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.jpg"):
        old.unlink()
    cache: dict[str, tuple] = {}
    for e in entries:
        best = next(r for r in e["sources"] if r["frame"] == e["best"])
        frame = Image.open(store.root / best["frame"]).convert("RGB")
        rp = _refined_path(store, e["id"], best["frame"])
        if best.get("refined") and rp.is_file():          # the refined instance mask (check)
            mask = np.asarray(Image.open(rp).convert("L")) > 127
            ys, xs = np.nonzero(mask)
            if len(xs):
                W0, H0 = mask.shape[1], mask.shape[0]
                best = best | {"bbox": [xs.min() / W0, ys.min() / H0, xs.max() / W0, ys.max() / H0]}
        else:
            mask = _entry_mask(store, best["shot"], best["slots"], cache)
        if frame.size != (mask.shape[1], mask.shape[0]):
            mask = np.asarray(Image.fromarray(mask.astype(np.uint8) * 255).resize(frame.size)) > 127
        dim = ImageEnhance.Brightness(frame).enhance(0.35)
        im = Image.composite(frame, dim, Image.fromarray(mask.astype(np.uint8) * 255))
        W, H = frame.size
        x0, y0, x1, y1 = best["bbox"]
        pad = 0.08 * max(x1 - x0, y1 - y0) + 0.01
        box = (int(max(0, x0 - pad) * W), int(max(0, y0 - pad) * H), int(min(1, x1 + pad) * W),
               int(min(1, y1 + pad) * H))
        crop = im.crop(box)
        crop.thumbnail((480, 360))
        crop.save(out / f"{e['id']}.jpg", quality=88)
        clean = frame.crop(box)          # undimmed: the look reference a "source" entry's sheets get
        clean.thumbnail((768, 768))
        (out.parent / "refs").mkdir(exist_ok=True)
        clean.save(out.parent / "refs" / f"{e['id']}.jpg", quality=92)


def report_html(store: ProjectStore, data: dict) -> str:
    """A local page (site/library.html): entries grouped by district, then typology, each
    with its crop, tier, instance count, best source frame and share."""
    esc = html.escape
    by_d: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for e in data["entries"]:
        by_d[e["district"]][e["typology"]].append(e)
    rows = []
    for d in sorted(by_d):
        n = sum(len(v) for v in by_d[d].values())
        rows.append(f"<h2>{esc(d)} <small>{n} entr{'y' if n == 1 else 'ies'}</small></h2>")
        for ty in sorted(by_d[d], key=lambda t: min("ABCD".index(e["tier"]) for e in by_d[d][t])):
            rows.append(f"<h3>{esc(ty)}</h3><div class=grid>")
            for e in by_d[d][ty]:
                others = len({r['frame'] for r in e["sources"] if r["share"] >= data["min_share"]}) - 1
                mats = (f" · {esc(e['material'])}" if e.get("material") else "") + (
                    " · " + esc(", ".join(e["materials"])) if e.get("materials") else "")
                mood = f"<br>mood: {esc(', '.join(m['shot'] for m in e['mood']))}" if e["mood"] else ""
                rows.append(
                    f"<figure><img src='library/crops/{esc(e['id'])}.jpg' loading=lazy>"
                    f"<figcaption><b>{esc(e['id'])}</b> <span class=t{e['tier']}>{e['tier']}</span>"
                    f"<br>{esc(e['part'] if e['part'] != ty else e['kind'])}{mats}"
                    f"<br>{e['instances']} instance{'s' if e['instances'] != 1 else ''} · "
                    f"{esc(', '.join(f'{k} {v}' for k, v in list(e['districts'].items())[:3]))}"
                    f"<br>source: <code>{esc(e['best'])}</code> · {100 * e['share']:.1f}%{' of the slots holding it (approximate)' if e['tier'] == 'D' else ''}"
                    f"{f' (+{others} more)' if others > 0 else ''}{mood}"
                    + "".join(f"<br><span class=flag>⚠ {esc(f)}</span>" for f in e.get("flags", []))
                    + "</figcaption></figure>")
            rows.append("</div>")
    def moods(u):
        return "; mood frame: " + ", ".join(f"{m['shot']} {100 * m['share']:.0f}%" for m in u["mood"]) if u["mood"] else ""

    unseen = "".join(f"<li>{esc(u['id'])} ({u['tier']}, {u['instances']}x, best share {100 * u['share']:.2f}%"
                     f"{esc(moods(u))})</li>" for u in data["unseen"])
    src = "".join(f"<li>{esc(s)}: {', '.join(f'<code>{esc(k)}</code>' for k in ks)}</li>"
                  for s, ks in data["sources"].items())
    src += "".join(f"<li class=flag>excluded <code>{esc(c['frame'])}</code>: {esc(c['reason'])}</li>"
                   for c in data.get("candidates", []) if not c["ok"])
    counts = Counter(e["tier"] for e in data["entries"])
    head = " · ".join(f"{t} {TIERS[t]}: {counts.get(t, 0)}" for t in "ABCD")
    return f"""<!doctype html><meta charset=utf-8><title>Asset library plan</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;background:#fafaf8;color:#222}}
h2{{border-bottom:1px solid #ccc;margin-top:28px}} h3{{margin:14px 0 6px;color:#555}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
figure{{margin:0;background:#fff;border:1px solid #ddd;padding:6px}} img{{width:100%;display:block}}
figcaption{{font-size:12px;line-height:1.45;margin-top:4px}} code{{font-size:11px;word-break:break-all}}
span[class^=t]{{padding:0 5px;border-radius:3px;color:#fff;font-size:11px}}
.flag{{color:#a40}}.tA{{background:#7a3}}.tB{{background:#37a}}.tC{{background:#a73}}.tD{{background:#777}}
@media (prefers-color-scheme:dark){{body{{background:#1b1b1a;color:#ddd}}figure{{background:#262625;border-color:#444}}h3{{color:#aaa}}}}</style>
<h1>Asset library plan</h1><p>{len(data['entries'])} entries from the locked shot frames ({head}).
Crops: the best source frame, the entry's slots bright (object-id pass), everything else dimmed.</p>
{''.join(rows)}
<h2>Not visible enough in any shot frame <small>{len(data['unseen'])}</small></h2>
<p>Under {100 * data['min_share']:.1f}% of every source frame: built from their description alone, or left as massing.</p><ul>{unseen}</ul>
<h2>Sources</h2><ul>{src}</ul>
<h2>Surfaces (materials, not assets)</h2><p>{esc(', '.join(data['surfaces']))}</p>
"""


def custom_path(store: ProjectStore) -> Path:
    return sw_site.site_dir(store) / "library_custom.json"


def custom(store: ProjectStore) -> dict[str, dict]:
    """The user's own entries (created or duplicated in Library › Entries), by id. A separate
    file, so a re-plan (library.json) never wipes them."""
    return (read_json(custom_path(store), default={}) or {}).get("entries", {})


def save_custom(store: ProjectStore, entries: dict[str, dict]) -> None:
    write_json(custom_path(store), {"entries": entries})


def load(store: ProjectStore) -> dict | None:
    """The library plan with the user's custom entries merged in (an entry seen in a frame
    goes with the entries, the others with the unseen)."""
    data = read_json(library_path(store), default=None)
    cust = custom(store)
    if not cust:
        return data
    if data is None:
        data = {"min_share": MIN_SHARE, "sources": {}, "mood": {}, "candidates": [], "checked": True,
                "entries": [], "unseen": [], "surfaces": {}}
    ids = {e["id"] for e in data["entries"]} | {u["id"] for u in data["unseen"]}
    data = {**data, "entries": list(data["entries"]), "unseen": list(data["unseen"])}
    for eid, e in cust.items():
        if eid in ids:
            continue
        (data["entries"] if e.get("best") else data["unseen"]).append(e)
    return data


def summary(data: dict) -> str:
    """Text listing for the CLI: district -> typology -> entries."""
    lines = []
    by_d: dict[str, list] = defaultdict(list)
    for e in data["entries"]:
        by_d[e["district"]].append(e)
    for d in sorted(by_d):
        lines.append(f"{d}:")
        for e in by_d[d]:
            lines.append(f"  [{e['tier']}] {e['id']:<34} {e['instances']:>5}x  {100 * e['share']:5.1f}%  {e['best']}")
            for f in e.get("flags", []):
                lines.append(f"        ! {f}")
    for c in data.get("candidates", []):
        if not c["ok"]:
            lines.append(f"excluded {c['frame']}: {c['reason']}")
    lines.append(f"not visible enough ({len(data['unseen'])}): " + ", ".join(
        u["id"] + (f" (mood: {', '.join(m['shot'] for m in u['mood'])})" if u.get("mood") else "") for u in data["unseen"]))
    return "\n".join(lines)

