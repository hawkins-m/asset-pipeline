"""Areas: the unit of the daily asset loop. An area is a set of shots (what they show) and/or
districts (every entry built there); batch commands take --area NAME or --district ID, skip
approved assets, and pick up where they stopped.

    ap area define PROJECT NAME --shots S1 S2 ... [--districts D ...]
    ap area status PROJECT (NAME | --district D)       what each asset needs next
    ap area checklist PROJECT NAME                     the checklist in the project (areas/NAME.md)

Membership: a library entry is in a shot-defined area when one of the shots' id passes shows a
slot of it (at least MIN_SHARE of the frame; tile slots through the entries that list them), and
in a district-defined one when the entry has instances in that district.
"""
import hashlib
import re
from pathlib import Path

from .project import ProjectStore, read_json, write_json

MIN_SHARE = 0.0005


def path(store: ProjectStore) -> Path:
    from .stages import sw_site
    return sw_site.site_dir(store) / "areas.json"


def load(store: ProjectStore) -> dict:
    return read_json(path(store), default={}) or {}


def define(store: ProjectStore, name: str, shots: list[str] | None = None, districts: list[str] | None = None) -> dict:
    from .stages import sw_site
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError(f"area name {name!r}: lower-case letters, digits and dashes")
    known = {s["id"] for s in sw_site.load_greybox(store)["shots"]}
    bad = [s for s in shots or [] if s not in known]
    if bad:
        raise ValueError(f"no shot(s) {', '.join(bad)} (ap shots list {store.root.name})")
    if not shots and not districts:
        raise ValueError("give --shots and/or --districts")
    a = load(store)
    a[name] = {"shots": shots or [], "districts": districts or []}
    write_json(path(store), a)
    return a[name]


def brief_sha(store: ProjectStore, asset: str) -> str | None:
    from . import kit_proc
    sp = kit_proc.spec_path(store, asset)
    return hashlib.sha1(sp.read_bytes()).hexdigest()[:12] if sp.is_file() else None


def _entries_and_decisions(store):
    from .stages import sw_library
    data = sw_library.load(store)
    if data is None:
        raise FileNotFoundError(f"no library plan yet: run ap library plan {store.root.name}")
    ents = {e["id"]: e for e in data["entries"]} | {u["id"]: u for u in data.get("unseen", [])}
    return ents, sw_library.decisions(store)


def members(store: ProjectStore, area: str | None = None, district: str | None = None) -> dict[str, float]:
    """{entry: its largest frame share in the area's shots (0 for district-only members)}."""
    from .stages import sw_shots, sw_site
    if not area and not district:
        raise ValueError("give an area (--area NAME) or a district (--district ID)")
    spec = {"shots": [], "districts": [district]} if district else load(store).get(area)
    if spec is None:
        raise KeyError(f"no area {area!r}: define it with ap area define {store.root.name} {area} --shots ...")
    ents, _ = _entries_and_decisions(store)
    by_slot = {}
    for e in ents.values():
        for sid in e.get("slot_ids") or []:
            by_slot.setdefault(sid, []).append(e["id"])
    slots = {s["id"]: s for s in sw_site.load_greybox(store)["slots"]} if spec["shots"] else {}   # districts only: no greybox needed
    out = {}
    for shot in spec["shots"]:
        ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json", default=None)
        if ids is None:
            raise FileNotFoundError(f"{shot}: no id pass; run ap shots render {store.root.name} {shot}")
        for sid, v in ids["slots"].items():
            if v["frac"] < MIN_SHARE:
                continue
            got = by_slot.get(sid, [])
            s = slots.get(sid) or {}
            if not got and s.get("typology") not in (None, "None"):     # entries newer than the plan
                t = s["typology"].replace("_", "-")
                got = [k for k in (f"{t}-{s.get('material')}", t) if k in ents][:1]
            for e in got:
                out[e] = max(out.get(e, 0.0), v["frac"])
    for e in ents.values():                          # props and kit pieces inside a slot (stalls in a market):
        for src in e.get("sources") or []:           # the shots the library plan found them in
            if src.get("shot") in spec["shots"]:
                out[e["id"]] = max(out.get(e["id"], 0.0), float(src.get("share") or 0.0))
    for d in spec.get("districts") or []:
        for e in ents.values():
            if d in (e.get("districts") or {}) or e.get("district") == d:
                out.setdefault(e["id"], 0.0)
    from . import variants                           # a variant set's members go where its base goes
    for base, vs in variants.sets(store).items():
        if base in out and (not vs.get("areas") or not area or area in vs["areas"]):
            for m in vs.get("members") or {}:
                if m in ents:
                    out.setdefault(m, out[base])
    return out


def slots_in(store: ProjectStore, area: str) -> set[str]:
    """The greybox slots an area covers: those its shots show (MIN_SHARE of a frame or more) and
    those in its districts."""
    from .stages import sw_shots, sw_site
    spec = load(store).get(area)
    if spec is None:
        raise KeyError(f"no area {area!r}")
    out = set()
    for shot in spec["shots"]:
        ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json", default=None) or {"slots": {}}
        out |= {sid for sid, v in ids["slots"].items() if v["frac"] >= MIN_SHARE}
    ds = set(spec.get("districts") or [])
    if ds:
        out |= {s["id"] for s in sw_site.load_greybox(store)["slots"] if s.get("district") in ds}
    return out


def assets_of(store: ProjectStore, entry: str, decision: dict | None) -> list[str]:
    """The assets an entry is made of: its own, or its kit modules (decision kit)."""
    if (decision or {}).get("decision") == "kit":
        return [f"{entry}-{k}" for k in (decision.get("pieces") or {})]
    return [entry]


def status(store: ProjectStore, area: str | None = None, district: str | None = None) -> list[dict]:
    """One row per asset of the area: what it has (sheets, brief, build, review) and its next step."""
    from . import kit_proc, library_review, variants
    from .stages import s2_refs, s5_3d, sw_library
    ents, dec = _entries_and_decisions(store)
    in_set = {m: base for base, vs in variants.sets(store).items() for m in (vs.get("members") or {}) if m != base}
    revs = library_review.reviews(store)
    units = {u.key: u for u in s2_refs.units(store, sw_library.PLAN_NAME)}
    rows = []
    for eid, share in sorted(members(store, area, district).items(), key=lambda kv: -kv[1]):
        e, d = ents[eid], dec.get(eid) or {}
        if e.get("kind") == "vegetation":
            continue                                   # trees: Unreal PCG (vegetation zones, street-tree slots)
        route = sw_library.route(e, d) or "trellis"
        for a in assets_of(store, eid, d):
            u = units.get(a)
            nsheets = len(s2_refs.sheets(store, u)) if u else 0
            brief = kit_proc.spec_path(store, a).is_file()
            rep = read_json(kit_proc.out_dir(store, a) / "procedural.json", default=None)
            built = (kit_proc.out_dir(store, a) / "procedural.glb").is_file()
            fresh = built and rep is not None and rep.get("brief_sha") == brief_sha(store, a)
            inst = read_json(kit_proc.out_dir(store, a) / "instances" / "index.json", default=None) or []
            trellis = s5_3d.results(store, sw_library.PLAN_NAME, a) if route == "trellis" else []
            st = (revs.get(a) or {}).get("status")
            missing = []
            if route == "procedural" and not brief and e.get("kind") != "kit":
                from . import library_entries
                fi = library_entries.form_info(store, a)
                missing = [k for k, v in (("form", fi["form"]), ("size", fi["size_m"])) if not v]
            row = {"asset": a, "entry": eid, "tier": e.get("tier"), "kind": e.get("kind"), "share": round(share, 4), "missing": missing,
                   "decision": d.get("decision"), "route": route, "sheets": nsheets, "brief": brief,
                   "built": built, "fresh": fresh, "instances": len(inst), "trellis": len(trellis),
                   "status": st, "note": (revs.get(a) or {}).get("note", ""), "variant_of": in_set.get(eid)}
            row["next"] = next_step(store, row, area, district)
            rows.append(row)
    return rows


def next_step(store: ProjectStore, r: dict, area: str | None, district: str | None) -> str:
    sel = f"--area {area}" if area else f"--district {district}"
    p = store.root.name
    if r["status"] == "approved":
        return "done (approved)"
    if not r["decision"] and not r["brief"]:
        return f"decide its source in Library › Entries (or ap library decide {p} {r['entry']} ...)"
    if not r["sheets"] and not (r["route"] == "procedural" and r["brief"]):
        return f"make sheets: References tab, or ap library sheets {p} {sel}"
    if r["route"] == "procedural":
        if r.get("missing"):
            return (f"set its {' and '.join(r['missing'])}: Manage entry… › Form / Size in Library › Asset review "
                    f"(the Brief & build dialog asks too)")
        if not r["brief"]:
            return f"brief from the best sheet view: ap library brief {p} {r['asset']} <sheet> front"
        if not r["built"] or not r["fresh"]:
            return f"build: Library › Asset review › Save & Rebuild, or ap library build {p} {sel}"
        if r["status"] in ("needs_changes", "needs_feature"):
            return "match the sheet with brief fields, then Save & Rebuild (RUNBOOK: matching a sheet)"
        return "review: Library › Asset review (A approve, C needs changes)"
    if not r["trellis"]:
        return f"pick a view in Review, then 3D there or ap library trellis {p} {sel}"
    return "review the TRELLIS result in Review, then Approve it there"


def module_size(store: ProjectStore, e: dict) -> list[float] | None:
    """A structure built from repeated modules, as one module: a city wall run as one wall bay
    (city.WALL_BAY_M along it, the enclosure's wall depth and height). None for anything else."""
    if (e or {}).get("typology") != "wall":
        return None
    from . import city
    from .stages import sw_site
    try:
        enc = sw_site.load_layout(store).city.enclosure
    except (FileNotFoundError, AttributeError):
        return None
    if enc is None or enc.kind != "wall":
        return None
    return [city.WALL_BAY_M, float(enc.wall_d), float(enc.wall_h)]


def _size_text(s) -> str:
    return " × ".join(f"{x:g}" for x in s) + " m"


def form_gaps(store: ProjectStore, rows: list[dict]) -> list[dict]:
    """The area's procedural assets that can't be briefed yet (no form, or no size), each with its
    suggested fixes, the first one recommended: [{asset, entry, missing, size_m, fixes: [{kind:
    form | route | manage, label, why, form?, size_m?, route?}]}]. A form fix sets the entry's form
    and size (Manage entry… › Form, Size); a route fix moves it to TRELLIS."""
    from . import library_entries
    from .building_gen import FORM_CHOICE
    out = []
    for r in rows:
        if r["route"] != "procedural" or r["brief"] or not r.get("missing") or r["status"] == "approved":
            continue
        a = r["asset"]
        ent, own = library_entries._entry_of(store, a)
        e = (ent or {}) if own else {}
        info = library_entries.form_info(store, a)
        choice, matched = library_entries.suggestion(e or ent or {"id": a}, None if own else a)
        size, size_why = info["size_m"], {"yours": "your size", "plan": "the plan's size",
                                         "kit piece": "the kit piece's size"}.get(info["size_source"])
        mod = module_size(store, e)
        if mod:
            size, size_why = mod, "one wall bay (the plan's size is the whole wall run)"
            matched_why = "a city wall is a run of arched bays: one bay, arches on piers with nothing over them"
        else:
            matched_why = None
        fixes = []
        if "form" in r["missing"]:
            form_fix = {"kind": "form", "form": choice, "size_m": size,
                        "label": f"Form: {FORM_CHOICE[choice]['label']}" + (f", {_size_text(size)}" if size else ""),
                        "why": (matched_why or f"its words point to it ({FORM_CHOICE[choice]['desc']})" if matched else
                                "no form matches its description; the cube is the catch-all")
                        + (f"; size: {size_why}" if size else "")}
            if matched or (e or ent or {}).get("kind") != "building":
                fixes.append(form_fix)
            else:                                       # an irregular building: TRELLIS from a sheet view
                fixes += [{"kind": "route", "route": "trellis", "label": "Build with TRELLIS",
                           "why": "no building form matches its description: TRELLIS makes it from a sheet view"},
                          form_fix]
        if not size or ("size" in r["missing"] and not fixes):
            fixes.append({"kind": "manage", "label": "Set size…", "why": "no size in the plan: give width, depth, height"})
        out.append({"asset": a, "entry": r["entry"], "missing": r["missing"], "size_m": info["size_m"], "fixes": fixes})
    return out


CHECK_BEGIN, CHECK_END = "<!-- ap area status: begin -->", "<!-- ap area status: end -->"


def checklist(store: ProjectStore, name: str) -> Path:
    """areas/NAME.md in the project: the status table (regenerated between the markers) and
    whatever the artist wrote around it (gap groups, notes), kept as it is."""
    rows = status(store, area=name)
    yes = lambda b: "yes" if b else "–"                 # noqa: E731
    table = ["| Asset | Tier | Route | Share | Sheets | Brief | Build | Review | Next |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        build = ("fresh" if r["fresh"] else "stale") if r["built"] else ("TRELLIS" if r["trellis"] else "–")
        table.append(f"| {r['asset']} | {r['tier'] or ''} | {r['route']} | {100 * r['share']:.1f}% | {r['sheets']} | {yes(r['brief'])} "
                     f"| {build} | {r['status'] or 'not reviewed'} | {r['next']} |")
    block = "\n".join([CHECK_BEGIN, "", *table, "", CHECK_END])
    out = store.root / "areas" / f"{name}.md"
    out.parent.mkdir(exist_ok=True)
    if out.is_file() and CHECK_BEGIN in out.read_text():
        txt = out.read_text()
        txt = txt[:txt.index(CHECK_BEGIN)] + block + txt[txt.index(CHECK_END) + len(CHECK_END):]
    else:
        spec = load(store)[name]
        txt = (f"# Area {name}\n\nShots: {', '.join(spec['shots']) or '–'}. Districts: {', '.join(spec['districts']) or '–'}.\n"
               f"Regenerate the table with `ap area checklist {store.root.name} {name}`; text outside it is kept.\n\n"
               f"## Status\n\n{block}\n\n## Gaps against the sheets\n\n(a) existing brief fields, (b) a new brief field, "
               f"(c) a new builder part.\n")
    out.write_text(txt)
    return out
