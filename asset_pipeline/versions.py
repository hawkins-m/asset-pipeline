"""Asset version bank: every build of a library asset is kept as a numbered version, so an
earlier look can be compared, restored or pinned.

On disk, per asset, under 3d/<plan>/<asset>/versions/ (for the library plan this is the same dir
as the procedural build, kit_proc.out_dir):
    versions.json     the index: [{n, kind, created, source, thumb, bytes, pinned, approved,
                                   files, note, ...}]
    v<N>/             a procedural version: brief.json (the brief incl. "guidance"), build.json
                      (settings and report), threeq.png / sheet.png (thumbnails), and
                      procedural.glb (+ .zones.json) only while pinned and current when pinned
A TRELLIS version keeps no copy: its GLB, stats json and input png stay where s5_3d put them
(so s5_3d.results is unchanged); `files` lists them, project-relative. Restoring one makes it the
newest result (the one cleanup picks); deleting one removes those files.

Retention: the last K unpinned versions (project.json `versions_keep`, default 3) stay; older
ones are only removed after a confirmation in the UI (prune): a build never prunes, so a failed
build costs no version. Pinned, Approved and the current (Active) version are never removed, and
the current one can't be deleted while other versions exist. Instances (library_instances) are not versions.
"""
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from . import filestore
from .project import ProjectStore, read_json, write_json


def result_time(glb: Path) -> float:
    from .stages.s5_3d import result_time as t
    return t(glb)

PLAN = "library"
KEEP_DEFAULT = 3
TRELLIS_STEM = re.compile(r"_s\d+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def asset_dir(store: ProjectStore, asset: str, plan: str = PLAN) -> Path:
    return store.root / "3d" / plan / asset


def root(store: ProjectStore, asset: str, plan: str = PLAN) -> Path:
    return asset_dir(store, asset, plan) / "versions"


def vdir(store: ProjectStore, asset: str, n: int, plan: str = PLAN) -> Path:
    return root(store, asset, plan) / f"v{n}"


def _index_path(store, asset, plan=PLAN) -> Path:
    return root(store, asset, plan) / "versions.json"


def has_bank(store, asset, plan=PLAN) -> bool:
    return _index_path(store, asset, plan).is_file()


def load(store: ProjectStore, asset: str, plan: str = PLAN) -> list[dict]:
    return read_json(_index_path(store, asset, plan), default=None) or []


def _save(store, asset, rows, plan=PLAN) -> None:
    write_json(_index_path(store, asset, plan), sorted(rows, key=lambda r: r["n"]))


def _rel(store, p: Path) -> str:
    return p.relative_to(store.root).as_posix()


def _size(paths) -> int:
    return sum(p.stat().st_size for p in paths if p.is_file())


def _dir_size(d: Path) -> int:
    return _size(p for p in d.rglob("*")) if d.is_dir() else 0


def _next_n(rows) -> int:
    return max((r["n"] for r in rows), default=0) + 1


# --- settings ------------------------------------------------------------------------------

def keep_limit(store: ProjectStore) -> int:
    k = (read_json(store.project_file, default={}) or {}).get("versions_keep")
    return int(k) if isinstance(k, int) and not isinstance(k, bool) and k >= 1 else KEEP_DEFAULT


def set_keep(store: ProjectStore, k) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("versions to keep must be a whole number, 1 or more")
    d = read_json(store.project_file, default={}) or {}
    d["versions_keep"] = k
    write_json(store.project_file, d)
    return k


def settings(store: ProjectStore) -> dict:
    return {"versions_keep": keep_limit(store), "default": KEEP_DEFAULT}


# --- recording -----------------------------------------------------------------------------

def _brief(store, asset) -> dict | None:
    from . import kit_proc
    return read_json(kit_proc.spec_path(store, asset), default=None)


def _source_of(brief: dict) -> dict:
    return {"sheet": brief.get("sheet"), "view": brief.get("view"), "position": brief.get("position"),
            "guidance": brief.get("guidance") or "",
            "form": brief.get("form_choice") or brief.get("form") or brief.get("kind"),   # the form picked in the app
            "seed": None, "input": None}


def _round_images(store, asset) -> dict:
    """The newest review round's renders (Cycles, else the quick preview): {threeq, sheet} paths."""
    from . import library_review as lr
    rounds = [r for r in lr._rounds(store, asset) if lr._imgs(store, lr.review_dir(store, asset, r))]
    if not rounds:
        return {}
    d = lr.review_dir(store, asset, rounds[-1])
    return {k: store.root / v for k, v in lr._imgs(store, d).items()}


def _fill_procedural(store, asset, n, brief, report, thumbs=True) -> dict:
    d = vdir(store, asset, n)
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "brief.json", brief)
    write_json(d / "build.json", report)
    row = {"files": ["brief.json", "build.json"]}
    if thumbs:
        row |= attach_thumbs_to(store, d, _round_images(store, asset))
    return row


def attach_thumbs_to(store, d: Path, imgs: dict) -> dict:
    """Copy render images into a version dir; returns the row fields {thumb, files}."""
    out, files = None, []
    for k in ("threeq", "sheet"):
        if imgs.get(k) and Path(imgs[k]).is_file():
            dst = d / f"{k}.png"
            shutil.copy2(imgs[k], dst)
            files.append(dst.name)
            out = out or _rel(store, dst)
    return {"thumb": out, "files": files}


def record_procedural(store: ProjectStore, asset: str, report: dict | None = None, surface: str | None = None,
                      thumbs: bool = False, created: str | None = None, plan: str = PLAN, adopted: bool = False,
                      make_current: bool = True) -> dict:
    """A new version from the build that just finished: the brief as it is now plus the build's
    settings and report. Thumbnails arrive with the review renders (attach_thumbs).
    make_current=False (a reroll): the version in use stays as it is."""
    brief = _brief(store, asset)
    if brief is None:
        raise FileNotFoundError(f"no brief for {asset}")
    rows = load(store, asset, plan)
    n = _next_n(rows)
    report = dict(report or {})
    row = {"n": n, "kind": "procedural", "created": created or _now(), "source": _source_of(brief),
           "thumb": None, "bytes": 0, "pinned": False, "approved": False, "files": [], "note": "",
           "brief_sha": report.get("brief_sha")}
    settings_ = {"surface": surface or brief.get("surface") or "auto", "report": report}
    row |= _fill_procedural(store, asset, n, brief, settings_, thumbs=thumbs)
    row["bytes"] = _dir_size(vdir(store, asset, n))
    rows.append(row)
    _save(store, asset, rows, plan)
    if not adopted and make_current and _becomes_current(store, asset, "procedural", plan):
        set_current(store, asset, n, plan)
    return row


def attach_thumbs(store: ProjectStore, asset: str, n: int | None, src_dir: Path | None = None) -> None:
    """The review renders of a rebuild become the version's thumbnails (called after the quick
    preview and again after Cycles)."""
    rows = load(store, asset)
    row = next((r for r in rows if r["n"] == n), None)
    if row is None or row["kind"] != "procedural":
        return
    from . import library_review as lr
    imgs = {k: store.root / v for k, v in lr._imgs(store, src_dir).items()} if src_dir else _round_images(store, asset)
    d = vdir(store, asset, n)
    for old in ("threeq.png", "sheet.png"):
        (d / old).unlink(missing_ok=True)
    got = attach_thumbs_to(store, d, imgs)
    row["thumb"] = got["thumb"]
    row["files"] = sorted(set(row["files"]) - {"threeq.png", "sheet.png"} | set(got["files"]))
    row["bytes"] = _dir_size(d)
    _save(store, asset, rows)


def _rel_path(store: ProjectStore, p: str | None) -> str | None:
    """A project-relative path (TRELLIS stats record absolute ones)."""
    if not p:
        return None
    q = Path(p)
    try:
        return q.resolve().relative_to(store.root.resolve()).as_posix() if q.is_absolute() else q.as_posix()
    except ValueError:
        return q.as_posix()


def _sheet_of_view(store: ProjectStore, view: str | None) -> str | None:
    """The reference sheet a cut view came from (the view dir's meta.json), else the view."""
    if not view:
        return None
    meta = read_json((store.root / view).parent / "meta.json", default=None) or {}
    hit = next((v.get("sheet") for v in meta.get("views", []) if v.get("file") == Path(view).name), None)
    return hit or view


def record_trellis(store: ProjectStore, plan: str, asset: str, record: dict, created: str | None = None,
                   adopted: bool = False) -> dict:
    """A new version from a TRELLIS result (s5_3d.run's record). The files stay in place.
    adopted: an older result taken into the bank (migration): it doesn't become current."""
    rows = load(store, asset, plan)
    glb = record["glb"]
    files = [glb, str(Path(glb).with_suffix(".json").as_posix()), record["input"]]
    files = [f for f in files if (store.root / f).is_file()]
    view = _rel_path(store, record.get("view"))
    src = {"sheet": _sheet_of_view(store, view), "view": view, "position": None, "guidance": "", "form": None,
           "seed": record.get("seed"), "input": record.get("input"), "mode": record.get("mode")}
    row = next((r for r in rows if r["kind"] == "trellis" and r["files"][:1] == [glb]), None)
    if row is None:       # a rerun with the same view, mode and seed overwrites its files: one version
        row = {"n": _next_n(rows), "kind": "trellis", "pinned": False, "approved": False, "note": ""}
        rows.append(row)
    row |= {"created": created or _now(), "source": src, "thumb": record.get("input"), "files": files,
            "bytes": _size(store.root / f for f in files)}
    _save(store, asset, rows, plan)
    if not adopted and _becomes_current(store, asset, "trellis", plan):
        set_current(store, asset, row["n"], plan)
    return row


# --- queries -------------------------------------------------------------------------------

def _trellis_current(store, rows, asset, plan=PLAN):
    """The TRELLIS version that is the newest result (what cleanup picks)."""
    best, t = None, -1.0
    for r in rows:
        if r["kind"] != "trellis" or not r["files"]:
            continue
        p = store.root / r["files"][0]
        if p.is_file() and result_time(p) > t:
            best, t = r, result_time(p)
    return best


def _pointer_path(store, asset, plan=PLAN) -> Path:
    return root(store, asset, plan) / "current.json"


def set_current(store: ProjectStore, asset: str, n: int, plan: str = PLAN) -> None:
    """The version the pipeline uses (cleanup, export, the Views and 3D cards): set by "Use this
    version" and by a new build that matches the entry's route."""
    write_json(_pointer_path(store, asset, plan), {"n": n, "at": _now()})


def _usable(store, r, asset, plan) -> bool:
    if r["kind"] == "trellis":
        return bool(r["files"]) and (store.root / r["files"][0]).is_file()
    return True


def route_of(store: ProjectStore, asset: str) -> str | None:
    """The build route of a library asset's entry (a kit module follows its parent), or None
    outside the library."""
    from . import library_entries
    from .stages import sw_library
    try:
        e, own = library_entries._entry_of(store, asset)
    except FileNotFoundError:
        return None
    if e is None:
        return None
    return sw_library.route(e, sw_library.decisions(store).get(asset if own else e["id"]))


def _becomes_current(store, asset, kind, plan) -> bool:
    r = route_of(store, asset) if plan == PLAN else None
    return r is None or r == kind


def current(store: ProjectStore, asset: str, plan: str = PLAN) -> int | None:
    """The version that is the asset's build now: the one "Use this version" (or the last build
    matching the entry's route) pointed at; without a pointer, the procedural one whose brief is
    the brief on disk (else the newest), or, with no brief, the newest TRELLIS result."""
    rows = load(store, asset, plan)
    if not rows:
        return None
    ptr = (read_json(_pointer_path(store, asset, plan), default=None) or {}).get("n")
    hit = next((r for r in rows if r["n"] == ptr), None)
    if hit is not None and _usable(store, hit, asset, plan):
        return hit["n"]
    proc = [r for r in rows if r["kind"] == "procedural"]
    if proc and _brief(store, asset) is not None:
        from .areas import brief_sha
        sha = brief_sha(store, asset)
        same = [r for r in proc if r.get("brief_sha") == sha] if sha else []
        return (same or proc)[-1]["n"]
    t = _trellis_current(store, rows, asset, plan)
    return t["n"] if t else rows[-1]["n"]


def _view(store, r, cur) -> dict:
    o = dict(r)
    o["current"] = r["n"] == cur
    o["has_glb"] = bool(r["kind"] == "trellis" or "procedural.glb" in r["files"])
    return o


def listing(store: ProjectStore, asset: str, plan: str = PLAN) -> dict:
    """The strip: versions newest first, sizes, what retention would remove."""
    if not has_bank(store, asset, plan):
        migrate_asset(store, asset, plan)
    rows = load(store, asset, plan)
    cur = current(store, asset, plan)
    pr = prune(store, asset, dry_run=True, plan=plan)
    return {"asset": asset, "keep": keep_limit(store), "current": cur,
            "versions": [_view(store, r, cur) for r in sorted(rows, key=lambda r: -r["n"])],
            "bytes": sum(r["bytes"] for r in rows), "prunable": pr["remove"], "prunable_bytes": pr["bytes"]}


def get(store, asset, n, plan=PLAN) -> dict:
    for r in load(store, asset, plan):
        if r["n"] == n:
            return r
    raise KeyError(f"{asset} has no version {n}")


# --- retention, delete ---------------------------------------------------------------------

def _protected(store, asset, rows, plan) -> set[int]:
    """Never removed by retention: pinned, Approved, the version in use, and the one "Use this
    version" points at (even while current() falls back to another, e.g. a file went missing)."""
    cur = current(store, asset, plan)
    ptr = (read_json(_pointer_path(store, asset, plan), default=None) or {}).get("n")
    return {r["n"] for r in rows if r["pinned"] or r["approved"]} | {x for x in (cur, ptr) if x is not None}


def prune(store: ProjectStore, asset: str, dry_run: bool = True, plan: str = PLAN, keep: int | None = None) -> dict:
    """Versions beyond the last K unpinned ones (K: project setting). Pinned, Approved and the
    current version stay. dry_run lists them (n and size) and removes nothing."""
    rows = load(store, asset, plan)
    k = keep if keep is not None else keep_limit(store)
    unpinned = sorted((r for r in rows if not r["pinned"]), key=lambda r: -r["n"])
    stay = _protected(store, asset, rows, plan)
    old = sorted((r for r in unpinned[k:] if r["n"] not in stay), key=lambda r: r["n"])
    out = {"remove": [r["n"] for r in old], "bytes": sum(r["bytes"] for r in old), "keep": k}
    if not dry_run:
        for r in old:
            _remove(store, asset, r, plan)
        _save(store, asset, [r for r in rows if r["n"] not in out["remove"]], plan)
    return out


def removable(store: ProjectStore, asset: str, plan: str = PLAN) -> dict:
    """Every unpinned version except the Approved and the current one (Storage › Delete unpinned)."""
    rows = load(store, asset, plan)
    stay = _protected(store, asset, rows, plan)
    old = [r for r in rows if r["n"] not in stay]
    return {"remove": [r["n"] for r in old], "bytes": sum(r["bytes"] for r in old)}


def delete_unpinned(store: ProjectStore, asset: str, plan: str = PLAN) -> dict:
    out = removable(store, asset, plan)
    rows = load(store, asset, plan)
    for r in rows:
        if r["n"] in out["remove"]:
            _remove(store, asset, r, plan)
    _save(store, asset, [r for r in rows if r["n"] not in out["remove"]], plan)
    return out


def _remove(store, asset, r, plan) -> None:
    """Drop a version's files: its references go, a shared file stays for whoever else links it."""
    if r["kind"] == "trellis":
        for f in r["files"]:
            filestore.release(store, store.root / f)
    vd = vdir(store, asset, r["n"], plan)
    for f in filestore.candidates(store, vd) if vd.is_dir() else []:
        filestore.release(store, f)
    shutil.rmtree(vd, ignore_errors=True)


def delete(store: ProjectStore, asset: str, n: int, confirm_pinned: bool = False, plan: str = PLAN) -> dict:
    rows = load(store, asset, plan)
    r = get(store, asset, n, plan)
    if r["approved"]:
        raise ValueError(f"v{n} is the Approved version: approve another build first")
    if len(rows) > 1 and current(store, asset, plan) == n:      # the last version may go: nothing else to use
        raise ValueError(f"v{n} is the Active version: use another version first")
    if r["pinned"] and not confirm_pinned:
        raise PermissionError(f"v{n} is pinned: confirm to delete it anyway")
    _remove(store, asset, r, plan)
    _save(store, asset, [x for x in rows if x["n"] != n], plan)
    return {"deleted": n, "bytes": r["bytes"]}


# --- pin, approve, restore -----------------------------------------------------------------

def pin(store: ProjectStore, asset: str, n: int, plan: str = PLAN) -> dict:
    """Keep a version for good. A procedural one also keeps its GLB when it is the current build
    (the GLB on disk is that build's); an older one is protected without a GLB."""
    from . import kit_proc
    rows = load(store, asset, plan)
    r = next((x for x in rows if x["n"] == n), None)
    if r is None:
        raise KeyError(f"{asset} has no version {n}")
    r["pinned"] = True
    if r["kind"] == "procedural" and current(store, asset, plan) == n:
        glb = kit_proc.out_dir(store, asset) / "procedural.glb"
        if glb.is_file():
            d = vdir(store, asset, n, plan)
            filestore.copy(store, glb, d / "procedural.glb")
            z = kit_proc.zones_path(glb)
            if z.is_file():
                shutil.copy2(z, d / "procedural.zones.json")
            r["files"] = sorted(set(r["files"]) | {"procedural.glb"} | ({"procedural.zones.json"} if z.is_file() else set()))
            r["bytes"] = _dir_size(d)
    _save(store, asset, rows, plan)
    return r


def unpin(store: ProjectStore, asset: str, n: int, plan: str = PLAN) -> dict:
    rows = load(store, asset, plan)
    r = next((x for x in rows if x["n"] == n), None)
    if r is None:
        raise KeyError(f"{asset} has no version {n}")
    r["pinned"] = False
    if r["kind"] == "procedural":
        d = vdir(store, asset, n, plan)
        for f in ("procedural.glb", "procedural.zones.json"):
            filestore.release(store, d / f)
        r["files"] = [f for f in r["files"] if not f.startswith("procedural.")]
        r["bytes"] = _dir_size(d)
    _save(store, asset, rows, plan)
    return r


def mark_approved(store: ProjectStore, asset: str, approved: bool = True, plan: str = PLAN) -> int | None:
    """The version current now is the Approved one (library_review.set_status); any other
    status clears the mark."""
    if not has_bank(store, asset, plan):
        if not approved:
            return None
        migrate_asset(store, asset, plan)
    rows = load(store, asset, plan)
    n = current(store, asset, plan) if approved else None
    for r in rows:
        r["approved"] = r["n"] == n
    _save(store, asset, rows, plan)
    return n


def _switch_route(store, asset, kind, plan) -> dict | None:
    """Make the entry's route match a version being used (TRELLIS / procedural). A kit module's
    route is its parent's: left alone. Returns {"from", "to"} when it changed."""
    from . import library_entries
    from .stages import sw_library
    if plan != PLAN:
        return None
    e, own = library_entries._entry_of(store, asset)
    if e is None or not own:
        return None
    old = sw_library.route(e, sw_library.decisions(store).get(asset))
    if old == kind:
        return None
    sw_library.set_route(store, asset, kind)
    return {"from": old, "to": kind}


def restore(store: ProjectStore, asset: str, n: int, log=print, plan: str = PLAN) -> dict:
    """Use a version: it becomes the version the pipeline uses (cleanup, export), and the entry's
    route switches to its kind. Procedural: its brief goes back as the asset's brief; with a pinned
    GLB that is copied back as the build, else the asset is rebuilt (the new version is used).
    TRELLIS: the version becomes the newest result (and its view the chosen one if it still exists).
    Raises if the version didn't end up current."""
    r = get(store, asset, n, plan)
    if not _usable(store, r, asset, plan):
        raise FileNotFoundError(f"v{n}: its 3D model is gone")
    route = _switch_route(store, asset, r["kind"], plan)
    res = _restore(store, asset, n, r, log, plan)
    want = res["version"]
    if want is not None:
        set_current(store, asset, want, plan)
    got = current(store, asset, plan)
    if want is None or got != want:
        raise RuntimeError(f"v{n} of {asset} could not be made the version in use (the pipeline still uses "
                           f"{'v' + str(got) if got else 'nothing'})")
    return res | {"current": got, "route": route, "glb": _rel_or_none(store, current_glb(store, asset, plan))}


def _rel_or_none(store, p):
    return _rel(store, p) if p else None


def version_glb(store: ProjectStore, asset: str, r: dict, plan: str = PLAN) -> Path | None:
    """A version's 3D model, if it has one: a TRELLIS version's GLB; a procedural one's pinned copy,
    or the asset's build on disk when that build is this version's (same brief, procedural.json)."""
    from . import kit_proc
    if r["kind"] == "trellis":
        p = store.root / r["files"][0] if r["files"] else None
        return p if p and p.is_file() else None
    own = vdir(store, asset, r["n"], plan) / "procedural.glb"
    out = kit_proc.out_dir(store, asset) if plan == PLAN else asset_dir(store, asset, plan)
    live = out / "procedural.glb"
    built = (read_json(out / "procedural.json", default={}) or {}).get("brief_sha")
    if live.is_file() and r.get("brief_sha") and r.get("brief_sha") == built:
        return live
    return own if own.is_file() else None


def current_glb(store: ProjectStore, asset: str, plan: str = PLAN) -> Path | None:
    """The GLB of the version in use: what cleanup and export take. A TRELLIS version's model; a
    procedural one's build (the asset's procedural.glb when it is the current build, else the
    version's pinned copy)."""
    from . import kit_proc
    n = current(store, asset, plan)
    if n is None:
        return None
    r = get(store, asset, n, plan)
    if r["kind"] == "trellis":
        p = store.root / r["files"][0]
        return p if p.is_file() else None
    own = vdir(store, asset, n, plan) / "procedural.glb"
    live = kit_proc.out_dir(store, asset) / "procedural.glb" if plan == PLAN else asset_dir(store, asset, plan) / "procedural.glb"
    if live.is_file():
        from .areas import brief_sha
        if r.get("brief_sha") is None or r.get("brief_sha") == brief_sha(store, asset) or not own.is_file():
            return live
    return own if own.is_file() else None


def cards(store: ProjectStore, asset: str, plan: str = PLAN) -> list[dict]:
    """Views and 3D's model cards: one per version that has a 3D model, the one in use first, then
    newest first. Nothing else (builds that aren't versions get none)."""
    if not has_bank(store, asset, plan):
        migrate_asset(store, asset, plan)
    adopt_orphans(store, asset, plan)
    rows = load(store, asset, plan)
    cur = current(store, asset, plan)
    out = []
    for r in sorted(rows, key=lambda r: (r["n"] != cur, -r["n"])):
        if r["kind"] == "trellis":
            glb = version_glb(store, asset, r, plan)
            stats = read_json(glb.with_suffix(".json"), default={}) if glb else {}
            inp = (r.get("source") or {}).get("input")
        else:
            glb = current_glb(store, asset, plan) if r["n"] == cur else version_glb(store, asset, r, plan)
            stats = ((read_json(vdir(store, asset, r["n"], plan) / "build.json", default={}) or {}).get("report") or {})
            inp = None
        if not glb or not glb.is_file():
            continue
        stats = stats or {}
        out.append({"n": r["n"], "kind": r["kind"], "current": r["n"] == cur, "glb": _rel(store, glb),
                    "input": inp if inp and (store.root / inp).is_file() else None,
                    "faces": stats.get("glb_faces") or stats.get("faces"), "file_bytes": glb.stat().st_size,
                    "peak_vram_gb": stats.get("peak_vram_gb"), "seed": (r.get("source") or {}).get("seed"),
                    "thumb": r.get("thumb"), "created": r.get("created")})
    return out


def adopt_orphans(store: ProjectStore, asset: str, plan: str = PLAN) -> int:
    """TRELLIS results on disk that no version lists (from before the bank, or left by an older
    run) become versions, oldest first. They don't become the version in use."""
    rows = load(store, asset, plan)
    listed = {f for r in rows for f in r["files"]}
    n = 0
    for g in trellis_results(store, asset, plan):
        if _rel(store, g) in listed:
            continue
        stem = g.stem
        m = re.search(r"_(?P<mode>1024_cascade|512|1024|1536_cascade)_s(?P<seed>\d+)$", stem)
        stats = read_json(g.with_suffix(".json"), default={}) or {}
        record_trellis(store, plan, asset, {"glb": _rel(store, g), "input": _rel(store, g.parent / f"{stem}_input.png"),
                                            "view": stats.get("image") or stats.get("view"),
                                            "seed": int(m["seed"]) if m else None, "mode": m["mode"] if m else None},
                       created=_iso(result_time(g)), adopted=True)
        n += 1
    return n


def loose_files(store: ProjectStore, plan: str = PLAN) -> list[dict]:
    """GLBs at an asset's top level that are neither its current build (procedural.glb) nor any
    version's, and can't become versions (no TRELLIS name): removable under Tools › Storage…."""
    out = []
    for a in assets_of(store, plan):
        listed = {f for r in load(store, a, plan) for f in r["files"]}
        for g in sorted(asset_dir(store, a, plan).glob("*.glb")):
            if g.name == "procedural.glb" or _rel(store, g) in listed or TRELLIS_STEM.search(g.stem):
                continue
            out.append({"asset": a, "file": _rel(store, g), "bytes": g.stat().st_size})
    return out


def remove_loose(store: ProjectStore, plan: str = PLAN) -> dict:
    rows = loose_files(store, plan)
    freed = sum(filestore.release(store, store.root / r["file"]) for r in rows)
    return {"removed": len(rows), "bytes": freed}


def _restore(store, asset, n, r, log, plan) -> dict:
    from . import kit_proc
    if r["kind"] == "trellis":
        glb = store.root / r["files"][0]
        if not glb.is_file():
            raise FileNotFoundError(f"v{n}: its 3D model is gone")
        j = glb.with_suffix(".json")              # the entry's own file: the GLB may be shared
        os.utime(j if j.is_file() else glb)
        view = (r.get("source") or {}).get("view")
        if view and (store.root / view).is_file():
            from . import review
            try:
                review.choose(store, plan, asset, view)
            except (ValueError, FileNotFoundError):
                pass
        return {"version": n, "rebuilt": False}
    d = vdir(store, asset, n, plan)
    brief = read_json(d / "brief.json", default=None)
    if brief is None:
        raise FileNotFoundError(f"v{n}: its brief is gone")
    old = _brief(store, asset)
    if old != brief:
        write_json(kit_proc.spec_path(store, asset), brief)
        from . import library_review as lr
        lr.brief_edited(store, asset)
    live = version_glb(store, asset, r, plan)
    if live is not None and live == kit_proc.out_dir(store, asset) / "procedural.glb":
        return {"version": n, "rebuilt": False}       # the build on disk is this version's
    if (d / "procedural.glb").is_file():
        out = kit_proc.out_dir(store, asset)
        filestore.copy(store, d / "procedural.glb", out / "procedural.glb")
        if (d / "procedural.zones.json").is_file():
            shutil.copy2(d / "procedural.zones.json", kit_proc.zones_path(out / "procedural.glb"))
        rep = (read_json(d / "build.json", default={}) or {}).get("report") or {}
        from .areas import brief_sha
        write_json(out / "procedural.json", rep | {"brief_sha": brief_sha(store, asset)})
        rows = load(store, asset, plan)
        for x in rows:
            if x["n"] == n:
                x["brief_sha"] = brief_sha(store, asset)
        _save(store, asset, rows, plan)
        return {"version": n, "rebuilt": False}
    from . import library_review as lr
    res = lr.rebuild(store, asset, log=log)
    return {"version": res.get("version"), "from": n, "rebuilt": True}


# --- compare -------------------------------------------------------------------------------

def _flat(o, path="") -> dict:
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            out |= _flat(v, f"{path}.{k}" if path else str(k))
        return out
    return {path: o}


def compare(store: ProjectStore, asset: str, a: int, b: int, plan: str = PLAN) -> dict:
    ra, rb = get(store, asset, a, plan), get(store, asset, b, plan)
    src = [{"field": k, "a": (ra["source"] or {}).get(k), "b": (rb["source"] or {}).get(k)}
           for k in sorted(set(ra["source"] or {}) | set(rb["source"] or {}))
           if (ra["source"] or {}).get(k) != (rb["source"] or {}).get(k)]
    brief = []
    if ra["kind"] == "procedural" and rb["kind"] == "procedural":
        fa = _flat(read_json(vdir(store, asset, a, plan) / "brief.json", default={}) or {})
        fb = _flat(read_json(vdir(store, asset, b, plan) / "brief.json", default={}) or {})
        brief = [{"path": k, "a": fa.get(k), "b": fb.get(k)} for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)]
    return {"a": ra, "b": rb, "source_diff": src, "brief_diff": brief}


# --- sizes, storage ------------------------------------------------------------------------

def assets_of(store: ProjectStore, plan: str = PLAN) -> list[str]:
    base = store.root / "3d" / plan
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))) \
        if base.is_dir() else []                   # "_keep-…" folders are hand-kept copies, not assets


def current_bytes(store: ProjectStore, asset: str, plan: str = PLAN) -> int:
    """The asset's build files (everything under its out dir) that aren't a version's."""
    d = asset_dir(store, asset, plan)
    if not d.is_dir():
        return 0
    refd = {store.root / f for r in load(store, asset, plan) if r["kind"] == "trellis" for f in r["files"]}
    vroot = root(store, asset, plan)
    return sum(p.stat().st_size for p in d.rglob("*")
               if p.is_file() and vroot not in p.parents and p not in refd)


def storage(store: ProjectStore, plan: str = PLAN) -> dict:
    out = []
    for a in assets_of(store, plan):
        rows = load(store, a, plan)
        rm = removable(store, a, plan) if rows else {"remove": [], "bytes": 0}
        vb = sum(r["bytes"] for r in rows)
        cb = current_bytes(store, a, plan)
        out.append({"asset": a, "versions": len(rows), "pinned": sum(r["pinned"] for r in rows),
                    "versions_bytes": vb, "current_bytes": cb, "total": vb + cb,
                    "removable": len(rm["remove"]), "removable_bytes": rm["bytes"]})
    return {"assets": out, "keep": keep_limit(store),
            "versions_bytes": sum(r["versions_bytes"] for r in out), "current_bytes": sum(r["current_bytes"] for r in out),
            "total": sum(r["total"] for r in out), "removable": sum(r["removable"] for r in out),
            "removable_bytes": sum(r["removable_bytes"] for r in out)}


def storage_prune(store: ProjectStore, asset: str | None = None, plan: str = PLAN) -> dict:
    """Delete unpinned versions of one asset, or of the whole project."""
    n = b = 0
    for a in ([asset] if asset else [a for a in assets_of(store, plan) if load(store, a, plan)]):
        r = delete_unpinned(store, a, plan)
        n += len(r["remove"])
        b += r["bytes"]
    return {"removed": n, "bytes": b}


# --- migration -----------------------------------------------------------------------------

def trellis_results(store, asset, plan=PLAN) -> list[Path]:
    d = asset_dir(store, asset, plan)
    return sorted((g for g in d.glob("*.glb") if TRELLIS_STEM.search(g.stem)), key=result_time) \
        if d.is_dir() else []


def migrate_asset(store: ProjectStore, asset: str, plan: str = PLAN) -> int:
    """Every existing build of the asset becomes a version, TRELLIS results oldest first, the
    procedural build (brief + latest review render as the thumbnail, no GLB) after them as the
    newest when it was built later. No-op when the bank exists. Returns the versions made."""
    if has_bank(store, asset, plan):
        return 0
    from . import kit_proc
    out = asset_dir(store, asset, plan)
    items = []          # (mtime, kind, payload)
    for g in trellis_results(store, asset, plan):
        items.append((result_time(g), "trellis", g))
    pglb = out / "procedural.glb"
    if plan == PLAN and _brief(store, asset) is not None and (pglb.is_file() or (out / "procedural.json").is_file()):
        items.append(((pglb if pglb.is_file() else out / "procedural.json").stat().st_mtime, "procedural", None))
    items.sort(key=lambda x: x[0])
    _save(store, asset, [], plan)
    for ts, kind, g in items:
        if kind == "procedural":
            rep = read_json(out / "procedural.json", default={}) or {}
            record_procedural(store, asset, rep, thumbs=True, created=_iso(ts), plan=plan, adopted=True)
        else:
            stem = g.stem
            stats = g.with_suffix(".json")
            m = re.search(r"_(?P<mode>1024_cascade|512|1024)_s(?P<seed>\d+)$", stem)
            rec = {"glb": _rel(store, g), "input": _rel(store, g.parent / f"{stem}_input.png"),
                   "view": None, "seed": int(m["seed"]) if m else None, "mode": m["mode"] if m else None}
            if stats.is_file():
                s = read_json(stats, default={}) or {}
                rec["view"] = s.get("image") or s.get("view")
            record_trellis(store, plan, asset, rec, created=_iso(ts), adopted=True)
    if plan == PLAN and items:
        from . import library_review as lr
        if lr.get(store, asset).get("status") == "approved":
            mark_approved(store, asset)
    return len(items)


def migrate(store: ProjectStore, plan: str = PLAN) -> dict:
    """Version 1 for every build the project already has (idempotent)."""
    from . import kit_proc
    names = set(assets_of(store, plan))
    names |= {p.stem for p in (store.root / "library/kits").glob("*.json")} if plan == PLAN else set()
    made = {}
    for a in sorted(names):
        n = migrate_asset(store, a, plan)
        if n:
            made[a] = n
    return {"assets": len(made), "versions": sum(made.values()), "detail": made}
