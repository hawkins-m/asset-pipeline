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

Sources: the locked frame of every locked shot that has one, plus that shot's other
frames marked as shot frames (role "source") from the same batch geometry. Mood shots
(MOOD_TIERS) never seed assets; their frames are kept per entry as `mood` references.
Locked cameras never move, so a locked frame made before a later rebuild still lines up
with its shot's current id pass (only geometry changed elsewhere since: checked per shot).

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

from .. import review
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
TIERS = {"A": "landmarks and kit monuments", "B": "singular civic and infrastructure", "C": "fill typologies",
         "D": "props and vegetation"}


def library_path(store: ProjectStore) -> Path:
    return sw_site.site_dir(store) / "library.json"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def sources(store: ProjectStore) -> dict[str, list[str]]:
    """shot -> its frames that may seed assets (locked frame first). Mood shots excluded."""
    mood = s0_frames.mood_shots(store)
    roles = review.roles(store)
    out = {}
    for shot, key in s0_frames.locked(store).items():
        if key == s0_frames.LOCK_GREYBOX or shot in mood:
            continue
        geom = (read_json(store.root / Path(key).parent / "meta.json", default={}) or {}).get("geometry_sha256")
        keys = [key]
        for b in s0_frames.batches(store, shot):
            for f in b["frames"]:
                if f["key"] != key and roles.get(f["key"]) == "source":
                    g = (read_json(store.root / Path(f["key"]).parent / "meta.json", default={}) or {}).get(
                        "geometry_sha256")
                    if g == geom:
                        keys.append(f["key"])
        out[shot] = keys
    return out


def mood_frames(store: ProjectStore) -> dict[str, str]:
    """Mood shot -> its locked frame (look only)."""
    mood = s0_frames.mood_shots(store)
    return {s: k for s, k in s0_frames.locked(store).items() if s in mood and k != s0_frames.LOCK_GREYBOX}


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
    src = sources(store)
    moods = mood_frames(store)
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
            for k in keys:
                entries[eid]["sources"].append({"frame": k, "shot": shot, "tier": tiers.get(shot),
                                                "share": round(pe["share"], 5),
                                                "bbox": [round(x, 4) for x in pe["bbox"]],
                                                "slots": sorted(pe["slots"]), "locked": k == keys[0],
                                                "districts": dict(pe["districts"])})
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
        e["sources"].sort(key=lambda r: (-r["share"], not r["locked"]))
        if e["sources"] and e["sources"][0]["share"] >= min_share:
            best = e["sources"][0]
            e["best"], e["share"] = best["frame"], best["share"]
            vis = Counter()
            for r in e["sources"]:
                vis.update(r["districts"])
            e["district"] = vis.most_common(1)[0][0]
        else:
            e["district"] = e["districts"].most_common(1)[0][0] if e["districts"] else "-"
        e["districts"] = dict(e["districts"].most_common())
        if "materials" in e:
            e["materials"] = dict(e["materials"].most_common())
        e["description_auto"] = _description(e, catalog, mats)
        e["description"] = None
        e.pop("slots")
        (out_entries if e["best"] else unseen).append(e)
    order = {"A": 0, "B": 1, "C": 2, "D": 3}
    out_entries.sort(key=lambda e: (e["district"], order[e["tier"]], e["typology"], -e["share"]))
    unseen.sort(key=lambda e: (order[e["tier"]], e["typology"], -e["instances"]))
    data = {"min_share": min_share, "sources": src, "mood": moods,
            "entries": out_entries,
            "unseen": [{k: e[k] for k in ("id", "tier", "typology", "part", "material", "instances", "districts",
                                          "mood")}
                       | {"share": e["sources"][0]["share"] if e["sources"] else 0.0} for e in unseen],
            "surfaces": {k: v for k, v in surfaces.most_common()}}
    lib = sw_site.site_dir(store) / LIBRARY_DIR
    _crops(store, data["entries"], lib / "crops")
    write_json(library_path(store), data)
    (sw_site.site_dir(store) / "library.html").write_text(report_html(store, data))
    return data


def _description(e: dict, catalog: dict, mats: dict) -> str:
    """The auto description that will drive the entry's reference sheets (step 2 refines it)."""
    t = catalog.get(e["typology"])
    base = (t.desc or t.prompt) if t else ""
    if not base:
        base = {"A": f"a {e['part'].replace('_', ' ')}", "B": f"a {e['typology'].replace('_', ' ')}",
                "C": f"a {e['typology'].replace('_', ' ')} building", "D": f"a {e['part']}"}[e["tier"]]
    if e["tier"] == "A" and e["part"] != e["typology"]:
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
    gb = sw_site.load_greybox(store)
    cache: dict[str, tuple] = {}
    for e in entries:
        best = next(r for r in e["sources"] if r["frame"] == e["best"])
        shot = best["shot"]
        if shot not in cache:
            ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json")
            rgb = np.asarray(Image.open(sw_shots.shot_dir(store, shot) / "ids.png").convert("RGB"))
            idx, names, _ = sw_shots.decode_ids(rgb, {k: v["color"] for k, v in ids["slots"].items()})
            cache[shot] = (idx, names)
        idx, names = cache[shot]
        want = [names.index(s) for s in best["slots"] if s in names]
        mask = np.isin(idx, want)
        frame = Image.open(store.root / best["frame"]).convert("RGB")
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
                    f"{f' (+{others} more)' if others > 0 else ''}{mood}</figcaption></figure>")
            rows.append("</div>")
    def moods(u):
        return "; mood frame: " + ", ".join(f"{m['shot']} {100 * m['share']:.0f}%" for m in u["mood"]) if u["mood"] else ""

    unseen = "".join(f"<li>{esc(u['id'])} ({u['tier']}, {u['instances']}x, best share {100 * u['share']:.2f}%"
                     f"{esc(moods(u))})</li>" for u in data["unseen"])
    src = "".join(f"<li>{esc(s)}: {', '.join(f'<code>{esc(k)}</code>' for k in ks)}</li>"
                  for s, ks in data["sources"].items())
    counts = Counter(e["tier"] for e in data["entries"])
    head = " · ".join(f"{t} {TIERS[t]}: {counts.get(t, 0)}" for t in "ABCD")
    return f"""<!doctype html><meta charset=utf-8><title>Asset library plan</title>
<style>body{{font:14px system-ui,sans-serif;margin:16px;background:#fafaf8;color:#222}}
h2{{border-bottom:1px solid #ccc;margin-top:28px}} h3{{margin:14px 0 6px;color:#555}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
figure{{margin:0;background:#fff;border:1px solid #ddd;padding:6px}} img{{width:100%;display:block}}
figcaption{{font-size:12px;line-height:1.45;margin-top:4px}} code{{font-size:11px;word-break:break-all}}
span[class^=t]{{padding:0 5px;border-radius:3px;color:#fff;font-size:11px}}
.tA{{background:#7a3}}.tB{{background:#37a}}.tC{{background:#a73}}.tD{{background:#777}}
@media (prefers-color-scheme:dark){{body{{background:#1b1b1a;color:#ddd}}figure{{background:#262625;border-color:#444}}h3{{color:#aaa}}}}</style>
<h1>Asset library plan</h1><p>{len(data['entries'])} entries from the locked shot frames ({head}).
Crops: the best source frame, the entry's slots bright (object-id pass), everything else dimmed.</p>
{''.join(rows)}
<h2>Not visible enough in any shot frame <small>{len(data['unseen'])}</small></h2>
<p>Under {100 * data['min_share']:.1f}% of every source frame: built from their description alone, or left as massing.</p><ul>{unseen}</ul>
<h2>Sources</h2><ul>{src}</ul>
<h2>Surfaces (materials, not assets)</h2><p>{esc(', '.join(data['surfaces']))}</p>
"""


def load(store: ProjectStore) -> dict | None:
    return read_json(library_path(store), default=None)


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
    lines.append(f"not visible enough ({len(data['unseen'])}): " + ", ".join(
        u["id"] + (f" (mood: {', '.join(m['shot'] for m in u['mood'])})" if u.get("mood") else "") for u in data["unseen"]))
    return "\n".join(lines)

