"""Library › Entries management: create, duplicate and rename library entries.

Entries made here are "custom": kept in site/library_custom.json (merged into
sw_library.load), so a re-plan never wipes them. Plan-derived entries (from the greybox) can
only change their display name and description: their id is the plan's.

An entry's files, by asset id (the entry itself, plus `<entry>-<piece>` for a kit decision):
    refs/library/<id>/            reference sheets + meta.json
    views/library/<id>/           cut views + meta.json
    3d/library/<id>/              procedural build, TRELLIS results, review rounds
    cleanup/library/<id>/         cleaned meshes
    library/kits/<id>.json        the procedural brief
    site/library/{refs,crops}/<id>.jpg   the entry's crop (source decision)
plus the stars and chosen view in review.json. Duplicate copies all of it; rename moves it.
Build review statuses (site/library_review.json "_reviews") are copied by neither: a duplicate
starts unreviewed, a rename carries the old id's review over.
"""
import copy
import re
import shutil
from pathlib import Path

from . import kit_proc, review
from .project import ProjectStore, read_json, write_json
from .stages import s1_plan, s2_refs, s3_views, s5_3d, s6_cleanup, sw_library

ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
KINDS = ("building", "prop")
DEFAULT_TIER = {"building": "B", "prop": "D"}
DEFAULT_SIZE = {"building": [20.0, 20.0, 15.0], "prop": [2.0, 2.0, 2.0]}
PLAN = sw_library.PLAN_NAME


class EntryExists(ValueError):
    """The target id (or files at it) exists; pass overwrite=True after the user agrees."""


def check_id(eid: str) -> str:
    if not isinstance(eid, str) or not ID_RE.fullmatch(eid) or eid.startswith("_") or eid.endswith("-"):
        raise ValueError("an id is lowercase letters, digits and hyphens (e.g. stone-counter)")
    return eid


def _all_entries(store: ProjectStore) -> dict[str, dict]:
    data = sw_library.load(store) or {}
    return {e["id"]: e for e in data.get("entries", []) + data.get("unseen", [])}


def _size(size) -> list[float] | None:
    if size in (None, [], ""):
        return None
    if isinstance(size, (int, float)):
        size = [size] * 3
    size = [float(x) for x in size]
    if len(size) == 1:
        size = size * 3
    if len(size) != 3 or any(x <= 0 for x in size):
        raise ValueError("size is one number, or width, depth, height, in metres (all above 0)")
    return size


def asset_ids(store: ProjectStore, eid: str) -> list[str]:
    """The asset ids an entry owns: itself, and its kit modules for a kit decision."""
    pieces = (sw_library.decisions(store).get(eid) or {}).get("pieces") or {}
    return [eid] + [f"{eid}-{p}" for p in pieces]


def paths(store: ProjectStore, asset: str) -> dict[str, Path]:
    """Where an asset's files live (the directories and files that may exist)."""
    lib = sw_library.sw_site.site_dir(store) / sw_library.LIBRARY_DIR
    unit = s2_refs.Unit(PLAN, asset, "object")
    return {"refs": unit.dir(store), "views": s3_views.view_dir(store, unit),
            "3d": s5_3d.out_dir(store, PLAN, asset), "cleanup": s6_cleanup.out_dir(store, PLAN, asset),
            "brief": kit_proc.spec_path(store, asset),
            "ref_crop": lib / "refs" / f"{asset}.jpg", "crop": lib / "crops" / f"{asset}.jpg"}


def _id_re(old: str):
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(old) + r"(?![A-Za-z0-9_-])")


def _retarget(target: Path, old: str, new: str) -> None:
    """Inside a copied/moved directory (or file): the old id in JSON text and in file names
    becomes the new one (meta.json's unit/assets, a brief's sheet and view paths...)."""
    rx = _id_re(old)
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    for f in files:
        if f.suffix == ".json":
            txt = f.read_text()
            new_txt = rx.sub(new, txt)
            if new_txt != txt:
                f.write_text(new_txt)
    for f in sorted((p for p in (target.rglob("*") if target.is_dir() else [])), key=lambda p: -len(p.parts)):
        if rx.search(f.name):
            f.rename(f.with_name(rx.sub(new, f.name)))


def _remove(p: Path) -> None:
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()


def _existing(store: ProjectStore, pairs: list[tuple[str, str]]) -> list[str]:
    out = []
    for _, dst in pairs:
        out += [p.relative_to(store.root).as_posix() for k, p in paths(store, dst).items() if p.exists()]
    return out


def _review_json(store: ProjectStore, pairs: list[tuple[str, str]], move: bool) -> None:
    """Stars and chosen views move with the files."""
    data = review.load(store)
    for src, dst in pairs:
        for k in list(data["stars"]):
            for pre in (f"{s2_refs.REFS}/{PLAN}/{src}/", f"views/{PLAN}/{src}/"):
                if k.startswith(pre):
                    data["stars"][pre.replace(f"/{src}/", f"/{dst}/") + k[len(pre):]] = data["stars"][k]
                    if move:
                        data["stars"].pop(k)
        ch = data["chosen"].get(f"{PLAN}/{src}")
        if ch is not None:
            data["chosen"][f"{PLAN}/{dst}"] = ch.replace(f"/{src}/", f"/{dst}/")
            if move:
                data["chosen"].pop(f"{PLAN}/{src}")
    write_json(review._file(store), data)


def _transfer(store: ProjectStore, src: str, dst: str, move: bool, overwrite: bool) -> list[str]:
    """Copy (or move) every file of `src` and its kit modules to `dst`. Returns the
    project-relative paths written."""
    pairs = [(a, dst + a[len(src):]) for a in asset_ids(store, src)]
    clash = _existing(store, pairs)
    if clash and not overwrite:
        raise EntryExists(f"files already exist for {dst}: {', '.join(clash[:6])}{' …' if len(clash) > 6 else ''}")
    written = []
    for a, b in pairs:
        ps, pd = paths(store, a), paths(store, b)
        for k in ("refs", "views", "3d", "cleanup", "brief", "ref_crop", "crop"):
            if not ps[k].exists():
                continue
            _remove(pd[k])
            pd[k].parent.mkdir(parents=True, exist_ok=True)
            if move:
                shutil.move(ps[k], pd[k])
            elif ps[k].is_dir():
                shutil.copytree(ps[k], pd[k])
            else:
                shutil.copy2(ps[k], pd[k])
            if k not in ("ref_crop", "crop"):
                _retarget(pd[k], a, b)
            written.append(pd[k].relative_to(store.root).as_posix())
    _review_json(store, pairs, move)
    return written


def sync_plan(store: ProjectStore, extra: list[str] = ()) -> None:
    """Re-write plans/library.json so new/renamed entries become units (sheets, views, 3D).
    The assets already included stay included; `extra` entries are added. Best effort: an
    entry that can't be planned (a source decision without a crop) is skipped."""
    dec = sw_library.decisions(store)
    old = s1_plan.load(store, PLAN)
    included = {a.id for a in old.assets if a.include} if old else set()
    keep = set(extra)
    for eid, d in dec.items():
        if eid.startswith("_"):
            continue
        ids = {eid} | {f"{eid}-{p}" for p in (d.get("pieces") or {})}
        if ids & included:
            keep.add(eid)
    ents = _all_entries(store)
    keep = {k for k in keep if (dec.get(k) or {}).get("decision") not in (None, "exclude")
            and not ((dec.get(k) or {}).get("decision") == "source" and not (ents.get(k) or {}).get("best"))}
    if keep:
        sw_library.asset_plan(store, only=sorted(keep))


# --- API ---------------------------------------------------------------------------------

def view(store: ProjectStore, eid: str) -> dict:
    e = _all_entries(store).get(eid)
    if e is None:
        raise KeyError(f"no library entry {eid}")
    d = sw_library.decisions(store).get(eid) or {}
    return {"id": eid, "custom": eid in sw_library.custom(store), "name": d.get("name") or e.get("name") or eid.replace("-", " "),
            "description": d.get("description"), "description_auto": e.get("description_auto"),
            "decision": d.get("decision"), "route": sw_library.route(e, d), "route_auto": e.get("route_auto"),
            "kind": e.get("kind"), "tier": e.get("tier"), "size_m": e.get("size_m")}


def create(store: ProjectStore, eid: str, name: str | None = None, description: str | None = None,
           kind: str = "prop", route: str | None = None, size_m=None, tier: str | None = None,
           decision: str | None = "description", overwrite: bool = False) -> dict:
    check_id(eid)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    route = route or ("procedural" if kind == "building" else "trellis")
    if route not in sw_library.ROUTES:
        raise ValueError(f"route must be one of {sw_library.ROUTES}")
    tier = tier or DEFAULT_TIER[kind]
    if tier not in sw_library.TIERS:
        raise ValueError(f"tier must be one of {tuple(sw_library.TIERS)}")
    if decision not in (None, "description", "exclude"):
        raise ValueError("a new entry has no crop: decide it 'description' (or leave it undecided)")
    if eid in _all_entries(store):
        raise EntryExists(f"there is already an entry {eid}")
    stale = _existing(store, [(eid, eid)])
    if stale and not overwrite:
        raise EntryExists(f"files already exist for {eid}: {', '.join(stale[:6])}")
    for p in paths(store, eid).values():
        _remove(p)
    name = (name or eid.replace("-", " ")).strip()
    desc = (description or "").strip() or name
    noun = re.sub(r"\s+", " ", name.lower()) if name else eid.replace("-", " ")
    entry = {"id": eid, "tier": tier, "kind": kind, "typology": noun, "part": noun, "material": None, "instances": 1,
             "districts": {}, "district": "custom", "mood": [], "flags": [], "best": None, "share": 0.0,
             "description_auto": desc, "description": None, "size_m": _size(size_m) or DEFAULT_SIZE[kind],
             "route_auto": route, "slot_ids": [], "custom": True, "name": name}
    cust = sw_library.custom(store)
    cust[eid] = entry
    sw_library.save_custom(store, cust)
    if decision:
        sw_library.decide(store, eid, decision)
        sync_plan(store, [eid])
    return view(store, eid)


def duplicate(store: ProjectStore, src: str, dst: str, name: str | None = None, overwrite: bool = False) -> dict:
    """A new custom entry `dst` with src's description, route, decision and every file: sheets,
    views, brief, build, TRELLIS results, review renders. Not its review status."""
    check_id(dst)
    ents = _all_entries(store)
    if src not in ents:
        raise KeyError(f"no library entry {src}")
    if dst in ents:
        raise EntryExists(f"there is already an entry {dst}")
    if src == dst:
        raise ValueError("pick a different id")
    _transfer(store, src, dst, move=False, overwrite=overwrite)
    e = copy.deepcopy(ents[src])
    for k in ("sources", "scores", "kept"):
        e.pop(k, None)
    e.update(id=dst, custom=True, slot_ids=[], instances=1, district=e.get("district") or "custom",
             name=(name or "").strip() or f"{view(store, src)['name']} copy", duplicate_of=src)
    cust = sw_library.custom(store)
    cust[dst] = e
    sw_library.save_custom(store, cust)
    d = sw_library.decisions(store)
    sd = {k: copy.deepcopy(v) for k, v in (d.get(src) or {}).items() if k != "name"}
    sd["name"] = e["name"]
    d[dst] = sd
    write_json(sw_library.decisions_path(store), d)
    if sd.get("decision"):
        sync_plan(store, [dst])
    return view(store, dst)


def rename(store: ProjectStore, src: str, dst: str | None = None, name: str | None = None,
           overwrite: bool = False) -> dict:
    """Custom entries: a new id (files move) and/or display name. Plan-derived entries: the
    display name only (their id is the plan's)."""
    ents = _all_entries(store)
    if src not in ents:
        raise KeyError(f"no library entry {src}")
    cust = sw_library.custom(store)
    if dst and dst != src:
        if src not in cust:
            raise ValueError(f"{src} comes from the library plan: its id can't change, only its name "
                             "(duplicate it to get an entry with an id of your own)")
        check_id(dst)
        if dst in ents:
            raise EntryExists(f"there is already an entry {dst}")
        _transfer(store, src, dst, move=True, overwrite=overwrite)
        e = cust.pop(src)
        e["id"] = dst
        cust[dst] = e
        d = sw_library.decisions(store)
        if src in d:
            d[dst] = d.pop(src)
        rv = d.get("_reviews") or {}
        for a in [a for a in rv if a == src or a.startswith(src + "-")]:
            rv[dst + a[len(src):]] = rv.pop(a)
        write_json(sw_library.decisions_path(store), d)
        sw_library.save_custom(store, cust)
        src = dst
        sync_plan(store, [dst] if (d.get(dst) or {}).get("decision") else [])
    if name is not None and name.strip():
        set_name(store, src, name)
    return view(store, src)


def set_name(store: ProjectStore, eid: str, name: str) -> dict:
    if eid not in _all_entries(store):
        raise KeyError(f"no library entry {eid}")
    d = sw_library.decisions(store)
    d.setdefault(eid, {})["name"] = " ".join(name.split())
    write_json(sw_library.decisions_path(store), d)
    cust = sw_library.custom(store)
    if eid in cust:
        cust[eid]["name"] = d[eid]["name"]
        sw_library.save_custom(store, cust)
    return view(store, eid)


# --- next step after a sheet is starred ---------------------------------------------------------

def brief_from_sheet(store: ProjectStore, entry: str, sheet: str, view="front", form: str | None = None,
                     dims=None, params=None) -> dict:
    """The procedural brief of an entry from one sheet (`ap library brief`): the form comes from
    the entry's catalog typology (or the entry's own `form`, else a cube), the size from the
    plan. Raises ValueError with the CLI's messages."""
    import json as _json
    from .building_proc import FORM_OF, brief, front_view
    from .stages import sw_site
    e = _all_entries(store).get(entry)
    dims = dims or (e or {}).get("size_m")
    if not form and e:
        form = e.get("form")
        if not form and not e.get("custom"):
            layout = sw_site.load_layout(store)
            typ = {t.id: t for t in (layout.city.typologies if layout.city else [])}.get(e["typology"])
            form = FORM_OF.get(typ.form) if typ else None
            form = "basilica" if e["typology"] == "basilica" else form
        elif not form:
            form = "cube"
    if not form or not dims:
        raise ValueError("give --form and --dims (no catalog form with a generator, or no size)")
    if view == "front":
        fv = front_view(store, [sheet], entry)
        if not fv["best"]:
            raise ValueError(f"no near-front view on {sheet}: " + ", ".join(f"view {p} {s:.2f}" for _, p, s in fv["scores"])
                             + " (pick one: 0 / 1 / 2 / all)")
        view = fv["best"][1]
    elif view != "all":
        view = int(view)
    return brief(store, entry, sheet, view, form, dims, _json.loads(params) if isinstance(params, str) else params)


def next_step(store: ProjectStore, plan: str, key: str) -> dict | None:
    """What a starred sheet of a library asset leads to: Brief & build (procedural) or Cut
    views (TRELLIS). None for other plans / units."""
    if plan != PLAN:
        return None
    e = _all_entries(store).get(key)
    if e is None:
        return None
    r = sw_library.route(e, sw_library.decisions(store).get(key))
    if r is None:
        return None
    return {"route": r, "has_brief": kit_proc.spec_path(store, key).is_file(),
            "has_build": (kit_proc.out_dir(store, key) / "procedural.glb").is_file(),
            "views": len(s3_views.views(store, plan, key))}
