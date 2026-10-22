"""Build reviews of procedural library assets (Library › Asset review › detail view, and
`ap library review apply`).

Per asset id (a kit entry's module has its own id: stoa -> stoa-bay), in
site/library_review.json under "_reviews":
    {"status": approved | needs_changes | needs_feature | ready | needs_review,
     "note": "...", "round": n, "at": iso time, "history": [{round, status, note, at, images}]}
"_review_round" holds the current round (starts at 1). Editing an Approved asset's brief sets it
back to Needs review. `apply --done` marks the handled assets Ready for review and opens the
next round.

Renders per round: 3d/library/<asset>/review/round_<n>/{preview_sheet, preview_threeq, sheet,
threeq}.png (quick Workbench first, Cycles when done) plus procedural.glb and status.json (the
rebuild job's steps). The previous build is the newest older round that has renders.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import blender, building_gen, filestore, kit_proc
from .building_parts import merged
from .project import ProjectStore, read_json, write_json
from .stages import sw_library, sw_shots, sw_site

STATUSES = {"approved": "Approved", "needs_changes": "Needs changes", "needs_feature": "Needs new feature",
            "ready": "Ready for review", "needs_review": "Needs review"}
TODO = ("needs_changes", "needs_feature")
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/blender_render_asset.py"
# the sheet's own angle: most sheets draw the street front (-Y); halls and basilicas their end (-X)
SHEET_AZ = {"hall_vault": -94, "basilica": -94}
STEPS = ("brief saved", "Blender build", "quick preview", "Cycles: sheet angle", "Cycles: three-quarter")
# brief values that are shares (0..1] of a bay, a storey, a face or a span
FRACTIONS = {"niche": ("w", "h", "rise", "mezzanine", "door_w"), "loggia": ("w", "h", "sill"),
             "window": ("w", "h", "sill"), "slot": ("w", "h", "sill"), "blind": ("w", "h", "sill"),
             "top": ("span",), "accent": ("w",), "end_screen": ("mezzanine", "passage_w"), "curtain": ("w", "transom"),
             "street_garden": ("length",), "panel_frame": ("w", "margin"), "arcade": ("rise",),
             "gallery": ("w", "h", "sill", "rise")}
TOP_FRACTIONS = ("nave", "aisle_h", "gallery_h", "atrium")
ZERO_OK = ("sill", "transom")          # shares that may be 0 (a loggia from the floor, no transom)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file(store: ProjectStore) -> dict:
    return sw_library.decisions(store)


def current_round(store: ProjectStore) -> int:
    return int(_file(store).get("_review_round", 1))


def reviews(store: ProjectStore) -> dict:
    return _file(store).get("_reviews", {})


def get(store: ProjectStore, asset: str) -> dict:
    return reviews(store).get(asset, {"status": None, "note": "", "round": current_round(store), "history": []})


def _save(store: ProjectStore, asset: str, rev: dict) -> dict:
    d = _file(store)
    d.setdefault("_reviews", {})[asset] = rev
    write_json(sw_library.decisions_path(store), d)
    return rev


def set_status(store: ProjectStore, asset: str, status: str | None, note: str = "") -> dict:
    """The user's call on an asset's build this round, with a note; kept in its history."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of {list(STATUSES)}")
    rnd = current_round(store)
    rev = get(store, asset)
    imgs = images(store, asset)
    item = {"round": rnd, "status": status, "note": note.strip(), "at": _now(),
            "images": [p for p in (imgs.get("reference"), *(imgs.get("current") or {}).values()) if p]}
    hist = [h for h in rev.get("history", []) if h.get("round") != rnd or h.get("status") in ("ready", "needs_review")]
    hist.append(item)
    from . import versions
    versions.mark_approved(store, asset, status == "approved")        # the version current now is the Approved one
    return _save(store, asset, {"status": status, "note": note.strip(), "round": rnd, "at": item["at"], "history": hist})


def brief_edited(store: ProjectStore, asset: str) -> dict:
    """An edit to an Approved asset's brief sets it back to Needs review."""
    rev = get(store, asset)
    if rev.get("status") != "approved":
        return rev
    hist = rev.get("history", []) + [{"round": current_round(store), "status": "needs_review",
                                      "note": "brief edited after approval", "at": _now(), "images": []}]
    from . import versions
    versions.mark_approved(store, asset, False)
    return _save(store, asset, rev | {"status": "needs_review", "at": _now(), "history": hist})


# --- renders -------------------------------------------------------------------------------

def review_dir(store: ProjectStore, asset: str, rnd: int) -> Path:
    return kit_proc.out_dir(store, asset) / "review" / f"round_{rnd}"


def _rounds(store: ProjectStore, asset: str) -> list[int]:
    base = kit_proc.out_dir(store, asset) / "review"
    return sorted(int(p.name.split("_")[1]) for p in base.glob("round_*") if p.name.split("_")[1].isdigit()) if base.is_dir() else []


def _imgs(store: ProjectStore, d: Path) -> dict:
    out = {}
    for v in ("sheet", "threeq"):
        for name in (v, f"preview_{v}"):          # Cycles when it's there, else the quick preview
            if (d / f"{name}.png").is_file():
                out[v] = (d / f"{name}.png").relative_to(store.root).as_posix()
                break
    return out


def images(store: ProjectStore, asset: str) -> dict:
    """Reference view, current build renders (the newest round) and the previous round's."""
    brief = read_json(kit_proc.spec_path(store, asset), default=None) or {}
    rounds = [r for r in _rounds(store, asset) if _imgs(store, review_dir(store, asset, r))]
    cur = rounds[-1] if rounds else None
    prev = rounds[-2] if len(rounds) > 1 else None
    glb = kit_proc.out_dir(store, asset) / "procedural.glb"
    st = read_json(review_dir(store, asset, cur) / "status.json", default=None) if cur else None
    from . import assembly
    asm = assembly.assembly_state(store, asset)            # with attached pieces: the assembly when up to date
    if asm and asm["fresh"] and asm["glb"]:
        glb = store.root / asm["glb"]
    return {"reference": brief.get("view"), "current_round": cur, "previous_round": prev, "assembly": asm,
            "current": _imgs(store, review_dir(store, asset, cur)) if cur else {},
            "previous": _imgs(store, review_dir(store, asset, prev)) if prev else {},
            "quick": bool(cur and not (review_dir(store, asset, cur) / "threeq.png").is_file()),
            "glb": glb.relative_to(store.root).as_posix() if glb.is_file() else None, "job": st}


def render(store: ProjectStore, asset: str, glb: Path, out: Path, mode: str, form: str | None, extra_views=None,
           figure=None) -> dict:
    """Renders of a GLB from the sheet's angle and three-quarter (quick or Cycles)."""
    views = [{"name": ("preview_" if mode == "quick" else "") + "sheet", "az": SHEET_AZ.get(form, -4), "el": 0.12},
             {"name": ("preview_" if mode == "quick" else "") + "threeq", "az": -35, "el": 0.35}] + (extra_views or [])
    out.mkdir(parents=True, exist_ok=True)
    zones = kit_proc.zones_path(glb)
    if not zones.is_file():                      # a copy of the GLB (a review round): the asset's own zones
        zones = kit_proc.zones_path(kit_proc.out_dir(store, asset) / "procedural.glb")
    job = {"glb": str(glb), "out_dir": str(out), "mode": mode, "views": views, "figure": figure,
           "size": [900, 640] if mode == "cycles" else [720, 512], "samples": 64,
           "zones": str(zones) if zones.is_file() else None}
    jp = out / f"render_{mode}.json"
    write_json(jp, job)
    log = out / f"render_{mode}.log"
    log.unlink(missing_ok=True)
    blender.run(SCRIPT, [jp], log, f"review renders {asset}")
    line = next((ln for ln in log.read_text().splitlines() if ln.startswith("REPORT ")), None)
    if not line:
        raise RuntimeError("render wrote no report:\n" + "\n".join(log.read_text().splitlines()[-15:]))
    return json.loads(line[7:])


def rebuild(store: ProjectStore, asset: str, log=print) -> dict:
    """The rebuild job: build the asset (brief already saved), copy the GLB into this round,
    quick preview renders within seconds, then the Cycles renders. Steps go to status.json."""
    rnd = current_round(store)
    d = review_dir(store, asset, rnd)
    d.mkdir(parents=True, exist_ok=True)
    for f in d.glob("*.png"):
        f.unlink()

    def step(k, line=""):
        write_json(d / "status.json", {"step": k, "of": len(STEPS), "steps": STEPS, "line": line or STEPS[min(k, len(STEPS) - 1)],
                                       "at": _now()})
    step(1, "Blender build")
    from . import versions
    res = kit_proc.build_any(store, asset, log=log)
    vn = res.get("version")
    glb = kit_proc.out_dir(store, asset) / "procedural.glb"
    filestore.copy(store, glb, d / "procedural.glb")
    form = (read_json(kit_proc.spec_path(store, asset), default={}) or {}).get("form")
    from . import assembly
    shown = assembly.render_glb(store, asset, log=log) or glb   # attached pieces render with it
    step(2, f"quick preview · {res.get('faces', 0):,} faces")
    render(store, asset, shown, d, "quick", form)
    versions.attach_thumbs(store, asset, vn, d)
    step(3)
    render(store, asset, shown, d, "cycles", form, extra_views=None)
    versions.attach_thumbs(store, asset, vn, d)
    step(5, "done")
    return {"round": rnd, "faces": res.get("faces"), "images": _imgs(store, d), "version": vn}


REROLL_FILES = ("procedural.glb", "procedural.zones.json", "procedural.json")


def reroll(store: ProjectStore, asset: str, guidance: str = "", view: str = "front", log=print,
           at=lambda step, note: None, to_cpu=lambda: None) -> dict:
    """Reroll (Views and 3D): a new procedural version from the asset's current sheet, steered by
    `guidance` (the VLM's brief edits, all accepted) or, without guidance, the sheet measured again
    with the artist's edits kept. The new version keeps its own GLB and does NOT become the
    version in use: the brief, the build on disk, the review status and the version in use are put
    back afterwards, also when a step fails (then no version is added). to_cpu: the job leaves
    GPU 0 for the Blender build (jobs.hop)."""
    from . import brief_guide, building_proc, library_entries, versions
    sp = kit_proc.spec_path(store, asset)
    brief = read_json(sp, default=None)
    if brief is None:
        raise ValueError(f"{asset} has no procedural brief yet: Brief & build it first (Library › Reference sheets)")
    sheet = brief.get("sheet")
    if not sheet or not (store.root / sheet).is_file():
        raise ValueError(f"{asset}: the brief's sheet is gone; Brief & build from a starred sheet first")
    guidance = (guidance or "").strip()
    if view == "front" and brief.get("position") is not None:     # measure the view the brief already uses
        view = str(brief["position"])
    out = kit_proc.out_dir(store, asset)
    versions.migrate_asset(store, asset)
    cur = versions.current(store, asset)
    approved = {r["n"] for r in versions.load(store, asset) if r["approved"]}
    rev = reviews(store).get(asset)
    bak = out / "_reroll_backup"
    shutil.rmtree(bak, ignore_errors=True)
    for f in REROLL_FILES:
        if (out / f).is_file():
            filestore.copy(store, out / f, bak / f)
    vn = None
    try:
        if guidance:
            at(1, "VLM: brief edits from the guidance…")
            prop = brief_guide.propose(store, asset, sheet, guidance, view)
            brief_guide.apply(store, asset, prop["edits"], guidance)
        else:
            at(1, "VLM: measuring the sheet again…")
            library_entries.brief_from_sheet(store, asset, sheet, view)
        building_proc.read_band_materials(store, asset, log=lambda *a: None)
        at(2, "Waiting for the CPU…")
        to_cpu()
        at(2, "Blender build…")
        res = kit_proc.build_any(store, asset, log=log, make_current=False)
        vn = res["version"]
        d = versions.vdir(store, asset, vn)                # the version's own GLB: the build on disk goes back
        filestore.copy(store, out / "procedural.glb", d / "procedural.glb")
        keep = {"procedural.glb"}
        if kit_proc.zones_path(out / "procedural.glb").is_file():
            shutil.copy2(kit_proc.zones_path(out / "procedural.glb"), d / "procedural.zones.json")
            keep.add("procedural.zones.json")
        at(3, "renders for the thumbnail…")
        rd = d / "renders"
        form = (read_json(sp, default={}) or {}).get("form")
        render(store, asset, d / "procedural.glb", rd, "quick", form)
        versions.attach_thumbs(store, asset, vn, rd)
        shutil.rmtree(rd, ignore_errors=True)
        rows = versions.load(store, asset)
        for r in rows:
            if r["n"] == vn:
                r["files"] = sorted(set(r["files"]) | keep)
                r["bytes"] = versions._dir_size(d)
        versions._save(store, asset, rows)
    finally:                                               # put back what the version in use is made of
        write_json(sp, brief)
        for f in REROLL_FILES:
            if (bak / f).is_file():
                filestore.copy(store, bak / f, out / f)
            elif (out / f).is_file():
                filestore.release(store, out / f)
        shutil.rmtree(bak, ignore_errors=True)
        if rev is not None and reviews(store).get(asset) != rev:
            _save(store, asset, rev)
        rows = versions.load(store, asset)
        if any(r["approved"] != (r["n"] in approved) for r in rows):
            for r in rows:
                r["approved"] = r["n"] in approved
            versions._save(store, asset, rows)
        if cur is not None:
            versions.set_current(store, asset, cur)
    return {"version": vn, "current": cur, "guidance": guidance}


# --- usage, checks ---------------------------------------------------------------------------

def entry_of(store: ProjectStore, asset: str) -> dict | None:
    data = sw_library.load(store) or {}
    es = {e["id"]: e for e in data.get("entries", []) + data.get("unseen", [])}
    if asset in es:
        return es[asset]
    head = asset.rsplit("-", 1)[0]                     # a kit module: <entry>-<piece>
    return es.get(head)


def usage(store: ProjectStore, asset: str) -> dict:
    """Where the asset is used: its instances in the greybox and the shots that show them, with
    each shot's lock (frame: a locked shot frame; greybox: greybox-locked, final in Unreal)."""
    e = entry_of(store, asset)
    if not e:
        return {"instances": 0, "slots": [], "shots": []}
    slots = set(e.get("slot_ids") or [])
    layout = sw_site.load_layout(store)
    shots = []
    for sh in layout.shots:
        ids = read_json(sw_shots.shot_dir(store, sh.id) / "ids.json", default=None)
        if not ids:
            continue
        hit = {s: v["frac"] for s, v in ids["slots"].items() if s in slots and v["frac"] > 0}
        if hit:
            lock = None if not sh.locked else ("greybox" if sh.locked == "greybox" else "frame")
            shots.append({"shot": sh.id, "tier": sh.tier, "frac": round(sum(hit.values()), 5), "lock": lock,
                          "frame": sh.locked if lock == "frame" else None, "slots": sorted(hit)})
    shots.sort(key=lambda r: (r["lock"] != "frame", -r["frac"]))
    return {"instances": e.get("instances", len(slots)), "slots": sorted(slots), "shots": shots,
            "size_m": e.get("size_m"), "typology": e.get("typology"), "entry": e["id"]}


def locked_impact(store: ProjectStore, asset: str, old: dict | None, new: dict, tol: float = 0.05) -> list[dict]:
    """Locked shot frames whose greybox would need to change to match a footprint or height
    change in the brief: the shots showing this asset's instances, when the dims moved by more
    than `tol`."""
    if not old or not old.get("dims"):
        return []
    moved = [k for k, a, b in zip("wdh", old["dims"], new["dims"]) if abs(b - a) > tol * max(abs(a), 1e-6)]
    if not moved:
        return []
    return [r | {"changed": moved} for r in usage(store, asset)["shots"] if r["lock"] == "frame"]


def _walk_fractions(feats: dict, path: str = "") -> list[str]:
    errs = []
    for k, v in (feats or {}).items():
        p = f"{path}{k}"
        if isinstance(v, dict):
            for fk in FRACTIONS.get(k, ()):
                x = v.get(fk)
                lo_ok = fk in ZERO_OK
                if isinstance(x, (int, float)) and not ((0 <= x if lo_ok else 0 < x) and x <= 1):
                    errs.append(f"{p}.{fk} = {x}: a share, must be in {'[0' if lo_ok else '(0'}, 1]")
            errs += _walk_fractions(v, p + ".")
        elif k in TOP_FRACTIONS and isinstance(v, (int, float)) and not 0 < v <= 1:
            errs.append(f"{p} = {v}: a share, must be in (0, 1]")
    return errs


def validate(store: ProjectStore, asset: str, brief: dict) -> dict:
    """Errors (block the rebuild) and warnings for a brief. Errors: positive sizes, shares in
    (0, 1], choices from building_gen.ENUMS, parts that fit each other. Warnings: footprint and
    storeys outside the catalog's ranges (a sheet may show another size; the catalog itself is
    edited in the City tab, and changing it reshuffles the city plan)."""
    errs, warns = [], []
    dims = brief.get("dims") or []
    if len(dims) != 3 or any(not isinstance(x, (int, float)) or x <= 0 for x in dims):
        errs.append("footprint and height must be three positive numbers")
        return {"errors": errs, "warnings": warns}
    if brief.get("form") not in building_gen.FORMS:
        errs.append(f"form must be one of {building_gen.FORMS}")
        return {"errors": errs, "warnings": warns}
    f = building_gen.features(brief["form"], brief.get("features"))
    errs += _walk_fractions(f)
    errs += _walk_enums(f)
    from .material_lib import LIBRARY
    for band, t in (brief.get("material_types") or {}).items():
        if band not in ("base", "wall", "top") or t not in LIBRARY:
            errs.append(f"material type {band} = {t!r}: bands base / wall / top, types {', '.join(sorted(LIBRARY))}")
    for band, x in (brief.get("material_scale") or {}).items():
        if band not in ("base", "wall", "top") or isinstance(x, bool) or not isinstance(x, (int, float)) \
                or not 0.05 <= x <= 20:
            errs.append(f"texture scale {band} = {x!r}: bands base / wall / top, a multiplier of the tile size in 0.05–20")
    pf = ((f.get("front") or {}).get("panel_frame") or {}) if brief["form"] == "basilica" else {}
    for k in ("proj", "gap"):
        if isinstance(pf.get(k), (int, float)) and not pf[k] > 0 - (k == "gap") * 1e-9:
            errs.append(f"front.panel_frame.{k} = {pf[k]}: must be {'positive' if k == 'proj' else 'zero or more'} (metres)")
    a_errs, a_warns = _arcade_checks(brief["form"], f, dims)
    errs += a_errs
    warns += a_warns
    if brief.get("surface") not in (None, "", "auto", "library", "patch"):
        errs.append(f"surface {brief['surface']!r}: library or patch")
    e = entry_of(store, asset)
    layout = sw_site.load_layout(store)
    typ = {t.id: t for t in (layout.city.typologies if layout.city else [])}.get((e or {}).get("typology"))
    w, d, h = dims
    p = brief.get("params") or {}
    if typ is not None and typ.width and typ.depth:      # landmark types carry no size ranges
        lo_w, hi_w = typ.width
        lo_d, hi_d = typ.depth
        form = brief["form"]
        if form == "row":
            hw = w / f["houses"]
            if not lo_w <= hw <= hi_w:
                warns.append(f"house width {hw:.1f} m outside the catalog's {lo_w:g}–{hi_w:g} m")
            if not lo_d <= d <= hi_d + 0.01:
                warns.append(f"depth {d:g} m outside the catalog's {lo_d:g}–{hi_d:g} m")
        elif form == "podium_tower":
            tw = f.get("tower_w") or min(22.0, 0.78 * d)
            if not lo_w <= tw <= hi_w:
                warns.append(f"tower width {tw:.1f} m outside the catalog's {lo_w:g}–{hi_w:g} m")
        else:
            a, b = max(w, d), min(w, d)
            if not (lo_w <= a <= max(hi_w, hi_d) + 0.01 and min(lo_w, lo_d) - 0.01 <= b <= max(hi_w, hi_d) + 0.01):
                warns.append(f"footprint {w:g} × {d:g} m outside the catalog's {lo_w:g}–{hi_w:g} × {lo_d:g}–{hi_d:g} m")
        st = p.get("storey_h") or f.get("storey_h") or typ.storey_m or (layout.city.storey_m if layout.city else 3.4)
        gh = f.get("ground_h") or st
        n = round((h - gh) / st) + 1 if form == "perimeter" else round(h / st)
        lo_s, hi_s = typ.storeys or (n, n)
        if not lo_s <= n <= hi_s:
            warns.append(f"{n} storeys at {st:g} m ({h:g} m) outside the catalog's {lo_s}–{hi_s}")
    elif typ is not None:
        warns.append("its catalog type has no size ranges (a landmark): sizes not checked")
    else:
        warns.append("no catalog typology for this asset: sizes not checked against ranges")
    if brief["form"] == "cube":
        ni = f.get("niche") or {}
        if ni and f["wall_t"] >= ni.get("depth", 99):
            errs.append(f"wall {f['wall_t']:g} m is as deep as the niche ({ni.get('depth'):g} m)")
    if e and e.get("size_m"):
        gw, gd, gh_ = e["size_m"]
        if abs(h - gh_) > 0.1 * gh_:
            warns.append(f"height {h:g} m differs from the greybox instance's {gh_:g} m")
    return {"errors": errs, "warnings": warns}


def _arcade_checks(form: str, f: dict, dims) -> tuple[list[str], list[str]]:
    """An arcade's geometry (errors) and how its storeys fit the brief's height (warnings)."""
    errs, warns = [], []
    if not f.get("arcade"):
        return errs, warns
    if form == "arcade_hall" and not f.get("range"):
        warns.append("arcade: only a colonnade range (range) uses it")
    A = building_gen.arcade_spec(f["arcade"])
    num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)          # noqa: E731
    for k in ("bay_m", "pier_w", "arch_h", "h", "depth"):
        if not num(A.get(k)) or A[k] <= 0:
            errs.append(f"arcade.{k} = {A.get(k)!r}: a size in metres, above 0")
    if errs:
        return errs, warns
    gap = A["bay_m"] - A["pier_w"]
    if gap < 0.5:
        errs.append(f"arcade.bay_m {A['bay_m']:g} m leaves {gap:.2f} m between piers {A['pier_w']:g} m wide: at least 0.5 m")
    if A["arch_h"] > A["h"] - 0.3:
        errs.append(f"arcade.arch_h {A['arch_h']:g} m: the crown must stay 0.3 m under the arcade storey (h {A['h']:g} m)")
    elif A.get("arch", "round") == "round" and gap >= 0.5 and A["arch_h"] < gap / 2 + 0.2:
        errs.append(f"arcade.arch_h {A['arch_h']:g} m is too low for a round arch {gap:.2f} m wide: at least "
                    f"{gap / 2 + 0.2:.2f} m (or arch seg)")
    if A["depth"] < A["pier_w"] + 0.4:
        errs.append(f"arcade.depth {A['depth']:g} m: the walk needs the piers' {A['pier_w']:g} m plus 0.4 m")
    im = A.get("impost")
    if im and not (num(im.get("h")) and im["h"] > 0 and num(im.get("proj")) and im["proj"] >= 0):
        errs.append("arcade.impost: h above 0 m and proj 0 m or more")
    G = A.get("gallery")
    if G:
        if not num(G.get("storey_h")) or G["storey_h"] < 2.4:
            errs.append(f"arcade.gallery.storey_h = {G.get('storey_h')!r}: at least 2.4 m")
        if not isinstance(G.get("storeys"), int) or isinstance(G.get("storeys"), bool) or G["storeys"] < 0:
            errs.append(f"arcade.gallery.storeys = {G.get('storeys')!r}: a whole number, 0 for as many as the height takes")
        if G.get("kind", "loggia") == "loggia" and num(G.get("depth")) and G["depth"] < A["pier_w"] + 0.3:
            warns.append(f"arcade.gallery.depth {G['depth']:g} m is no deeper than the piers: the loggia has no walk")
    if form == "arcade":
        faces = f.get("faces") or []
        bad = [x for x in faces if x not in building_gen.SIDES]
        if bad or not faces:
            errs.append(f"faces {faces}: one or more of {', '.join(building_gen.SIDES)}")
    if not errs:
        sub = f if form == "arcade" else {"arcade": A, "attic": None}
        hh = building_gen.arcade_height(sub, dims[2]) + ((f.get("attic") or {}).get("storey_h", 0.0) if form == "arcade" else 0.0)
        if abs(hh - dims[2]) > 0.15 * dims[2]:
            warns.append(f"arcade and gallery storeys come to {hh:.1f} m; the brief's height is {dims[2]:g} m")
    return errs, warns


def _walk_enums(f, path: str = "", parent: str = "") -> list[str]:
    """Values outside the choices a brief form offers (building_gen.ENUMS, bay kinds in patterns)."""
    errs = []
    if isinstance(f, dict):
        for k, v in f.items():
            p = f"{path}{k}"
            opts = building_gen.ENUMS.get(f"{parent}.{k}") or building_gen.ENUMS.get(k)
            if opts and isinstance(v, str) and v not in opts:
                errs.append(f"{p} = {v!r}: must be one of {', '.join(opts)}")
            elif k == "pattern" and isinstance(v, list):
                bad = [x for x in v if x not in building_gen.BAY_KINDS]
                if bad:
                    errs.append(f"{p}: {', '.join(map(str, bad))} not a bay kind ({', '.join(building_gen.BAY_KINDS)})")
            else:
                errs += _walk_enums(v, p + ".", k)
    elif isinstance(f, list):
        for i, v in enumerate(f):
            errs += _walk_enums(v, f"{path}{i}.", parent)
    return errs


def diff_features(form: str, feats: dict) -> dict:
    """The brief's features as the difference from the form's defaults as the brief form shows
    them (building_gen.form_features: what the user set)."""
    base = building_gen.form_features(form)

    def d(a, b):
        out = {}
        for k, v in (b or {}).items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                sub = d(a[k], v)
                if sub:
                    out[k] = sub
            elif a.get(k) != v:
                out[k] = v
        return out
    return d(base, feats)


def save_brief(store: ProjectStore, asset: str, new: dict) -> dict:
    """Validate and save an edited brief (features stored as the difference from the type's
    defaults); an Approved asset goes back to Needs review."""
    sp = kit_proc.spec_path(store, asset)
    old = read_json(sp, default=None) or {}
    b = {**old, **{k: v for k, v in new.items() if k in ("dims", "params", "features", "colors", "patches", "form",
                                                         "material_types", "material_scale", "surface",
                                                         "guidance")}}
    if not (b.get("guidance") or "").strip():
        b.pop("guidance", None)
    if not b.get("material_types"):
        b.pop("material_types", None)
    b["material_scale"] = {k: v for k, v in (b.get("material_scale") or {}).items() if v != 1}   # only the differences
    if not b["material_scale"]:
        b.pop("material_scale")
    if b.get("surface") in (None, "", "auto"):
        b.pop("surface", None)
    if new.get("sheet") and (new.get("sheet"), new.get("position")) != (old.get("sheet"), old.get("position")):
        from . import building_proc                       # another view: cut it and measure it again
        pos = new.get("position")
        pos = pos if pos == "all" else int(pos)
        v = building_proc.cut_view(store, new["sheet"], pos, asset)
        st = (b.get("params") or {}).get("storey_h", 3.4)
        b |= {"sheet": new["sheet"], "position": pos, "view": v,
              "measured": building_proc.measure(store.root / v, b["dims"], st)}
        b.pop("patches", None) if not new.get("patches") else None
    v = validate(store, asset, b)
    if v["errors"]:
        raise ValueError("; ".join(v["errors"]))
    b["features"] = diff_features(b["form"], building_gen.deep_merged(building_gen.form_features(b["form"]), b.get("features")))
    if b != old:
        write_json(sp, b)
        brief_edited(store, asset)
    return {"brief": b, "warnings": v["warnings"]}


def save_kit(store: ProjectStore, asset: str, params: dict) -> dict:
    """Save a kit piece's edited params (an arch gate, a colonnade bay: no catalog form): numbers
    stay numbers, sizes positive; an Approved asset goes back to Needs review."""
    sp = kit_proc.spec_path(store, asset)
    old = read_json(sp, default=None)
    if old is None:
        raise ValueError(f"no brief for {asset}: make one first (ap library brief)")
    errs = []
    for k, v in (params or {}).items():
        was = (old.get("params") or {}).get(k)
        if isinstance(was, (int, float)) and not isinstance(was, bool) and not isinstance(v, (int, float)):
            errs.append(f"{k} must be a number")
        elif isinstance(v, (int, float)) and k in ("w", "d", "h", "span") and v <= 0:
            errs.append(f"{k} must be positive")
    if errs:
        raise ValueError("; ".join(errs))
    new = old | {"params": (old.get("params") or {}) | (params or {})}
    if new != old:
        write_json(sp, new)
        brief_edited(store, asset)
    return {"brief": new, "warnings": []}


# --- the task list for a round -----------------------------------------------------------------

def tasks(store: ProjectStore) -> list[dict]:
    out = []
    for asset, rev in sorted(reviews(store).items()):
        if rev.get("status") in TODO:
            imgs = images(store, asset)
            out.append({"asset": asset, "status": rev["status"], "note": rev.get("note", ""), "round": rev.get("round"),
                        "brief": kit_proc.spec_path(store, asset).relative_to(store.root).as_posix(),
                        "images": [p for p in (imgs.get("reference"), *(imgs.get("current") or {}).values()) if p]})
    return out


def task_list(store: ProjectStore) -> str:
    ts = tasks(store)
    rnd = current_round(store)
    lines = [f"# Library review, round {rnd}: {len(ts)} asset(s) to handle (project {store.root.name})", ""]
    for i, t in enumerate(ts, 1):
        lines.append(f"{i}. {t['asset']} [{STATUSES[t['status']].lower()}]: brief {t['brief']}")
        if t["note"]:
            for k, ln in enumerate(t["note"].splitlines()):
                lines.append(("   note: " if k == 0 else "         ") + ln)
        for p in t["images"]:
            lines.append(f"   image: {store.root / p}")
        lines.append("")
    if ts:
        lines.append(f"# after handling: ap library review apply {store.root.name} --done " + " ".join(t["asset"] for t in ts))
    return "\n".join(lines)


def done(store: ProjectStore, assets: list[str]) -> int:
    """Mark handled assets Ready for review and open the next round. Returns the new round."""
    rv = reviews(store)
    missing = [a for a in assets if a not in rv]
    if missing:
        raise KeyError(f"no review for {missing}")
    rnd = current_round(store)
    for a in assets:
        rev = rv[a]
        hist = rev.get("history", []) + [{"round": rnd, "status": "ready", "note": "handled", "at": _now(), "images": []}]
        _save(store, a, rev | {"status": "ready", "round": rnd + 1, "at": _now(), "history": hist})
    d = _file(store)
    d["_review_round"] = rnd + 1
    write_json(sw_library.decisions_path(store), d)
    return rnd + 1
