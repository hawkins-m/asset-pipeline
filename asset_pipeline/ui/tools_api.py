"""UI endpoints for the runbook / USAGE commands that had no button yet: areas and their batches,
preflight, the remaining library, frames, shots, site, Unreal, terrain, demo, VLM and launcher
commands. Each endpoint calls the same Python function the CLI command calls (never `ap`), and
anything slow is a job (kinds and lanes below).

    GET/POST  /api/projects/{slug}/areas, /areas/status, /areas/{name}/checklist, /areas/batch
    POST      /api/projects/{slug}/library/asset/{asset}/brief-build   job library.brief (gpu0)
    POST      /api/preflight                                          quick request
    POST      /api/projects/{slug}/library/{pending,silhouette,review/done}, GET review/tasks
    POST      /api/projects/{slug}/frames/lock, /shots/add, /site/vegetation, /site/overlap
    GET/POST  /api/projects/{slug}/ue/...                             jobs ue.* (cpu)
    POST      /api/projects/{slug}/terrain/{action}                   job terrain.* (cpu)
    POST      /api/demo                                               job demo (cpu, script log -> note)
    POST      /api/vlm/up, /api/vlm/down                              jobs vlm.up / vlm.down (gpu0)
    GET/POST  /api/launcher[/install|/remove]
"""
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .. import areas, config, kit_proc, launcher, preflight, review, ue, vlm
from ..jobs import check_canceled, current_job
from ..project import ProjectStore, read_json
from ..stages import s0_frames, s2_refs, s5_3d, sw_library, sw_shots, sw_site

SLUG = re.compile(r"[a-z0-9][a-z0-9_-]*")
DOCS = "RUNBOOK.md"

# Preflight check name (prefix) -> how to fix it: the install command and the doc to read.
INSTALL_HINTS = [
    ("ComfyUI models", "scripts/fetch_models.sh", "README.md (models and licenses)"),
    ("Vision LLM", "scripts/install_llamacpp_vlm.sh 8b    # or: scripts/install_qwen_vl.sh", "CLAUDE.md (Vision LLM)"),
    ("Segmentation/depth", "scripts/install_segdepth.sh", "RUNBOOK.md"),
    ("TRELLIS.2", "scripts/install_trellis2.sh venv deps trellis nvdiffrast cumesh flexgemm ovoxel nvdiffrec verify",
     "USAGE.md (TRELLIS)"),
    ("Blender", "snap install blender --classic    # or set [tools] blender / AP_BLENDER", "USAGE.md"),
    ("Unreal Engine", "set [unreal] editor_cmd in config/backends.toml (UE 5.8, full editor)", "RUNBOOK.md (Unreal)"),
]


class AreaReq(BaseModel):
    name: str
    shots: list[str] = []
    districts: list[str] = []


class BatchReq(BaseModel):
    action: Literal["sheets", "build", "trellis", "instances"]
    area: str | None = None
    district: str | None = None
    assets: list[str] | None = None      # only these assets of the area (a row's next-step button)
    force: bool = False                  # build/instances: even if the brief is unchanged
    more: bool = False                   # trellis: another result for assets that have one
    mode: str = "1024_cascade"
    dry_run: bool = False                # count what would run; start nothing


class BriefBuildReq(BaseModel):
    sheet: str | None = None             # a sheet of the unit (default: its first)
    view: str = "front"                  # front | all | 0 | 1 | 2
    form: str | None = None
    dims: list[float] | None = None
    force_brief: bool = False            # redo the brief even if one exists


class PreflightReq(BaseModel):
    ue: bool = False


class SilhouetteReq(BaseModel):
    shot: str
    entries: list[str]


class PendingReq(BaseModel):
    shot: str
    no_draft: bool = False


class DoneReq(BaseModel):
    assets: list[str]


class LockReq(BaseModel):
    shot: str
    frame: str | None = None             # a frame key of the shot
    greybox: bool = False
    unlock: bool = False


class AddShotReq(BaseModel):
    id: str
    pos: tuple[float, float, float]
    look_at: tuple[float, float, float]
    lens: float = 35.0
    tier: Literal["wide", "medium", "tight"] = "medium"
    res: str = "1344x768"
    district: str | None = None
    notes: str = ""


class VegetationReq(BaseModel):
    preview: list[str] = []


class OverlapReq(BaseModel):
    shot: str


class UeImportReq(BaseModel):
    reset_layout: bool = False
    only_stale: bool = False
    prune: bool = False
    expect: int | None = None
    vegetation: bool = False
    no_backup_ok: bool = False           # reset / prune with no snapshot yet: the user said so


class UeRenderReq(BaseModel):
    shots: list[str] = []


class UeLookReq(BaseModel):
    name: str


class TerrainReq(BaseModel):
    confirm: bool = False                # apply and ue change the layout / the UE project


class DemoReq(BaseModel):
    mode: Literal["starter", "area"]
    slug: str = "my-project"             # starter: the new project; area: the existing one
    shot: str | None = None
    entries: list[str] = []


class VlmUpReq(BaseModel):
    model: str | None = None
    gpu: int = 0


class VlmDownReq(BaseModel):
    force: bool = False


def _run_demo_script(args: list[str], note, log: Path, cancel_hook) -> int:
    """scripts/demo.sh as a subprocess; its output lines go to the job's note and a log file."""
    script = config.REPO_ROOT / "scripts" / "demo.sh"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["bash", str(script), *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            cwd=str(config.REPO_ROOT), start_new_session=True)
    cancel_hook(lambda: os.killpg(proc.pid, signal.SIGTERM))
    with log.open("w") as f:
        for line in proc.stdout:
            f.write(line)
            f.flush()
            note(line.strip())
    return proc.wait()


def register(app: FastAPI, jobs, store_of, comfy_up, on_comfy) -> None:
    """store_of(slug) -> ProjectStore (404 for unknown), comfy_up() -> bool, on_comfy(store, stage) -> bool."""

    def sel(store, area, district):
        try:
            return areas.status(store, area, district)
        except KeyError as e:
            raise HTTPException(404, str(e).strip('"'))
        except (FileNotFoundError, ValueError) as e:
            raise HTTPException(400, str(e).strip('"'))

    # --- areas --------------------------------------------------------------------------

    @app.get("/api/projects/{slug}/areas")
    def areas_get(slug: str):
        store = store_of(slug)
        shots, districts = [], []
        if (sw_site.site_dir(store) / "greybox.json").is_file():
            shots = [s["id"] for s in sw_site.load_greybox(store)["shots"]]
        try:
            districts = [d.id for d in sw_site.load_layout(store).districts]
        except (FileNotFoundError, ValueError, AttributeError):
            pass
        return {"areas": areas.load(store), "shots": shots, "districts": districts,
                "has_plan": sw_library.load(store) is not None}

    @app.post("/api/projects/{slug}/areas")
    def areas_define(slug: str, req: AreaReq):
        store = store_of(slug)
        try:
            return areas.define(store, req.name, req.shots, req.districts)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/projects/{slug}/areas/status")
    def areas_status(slug: str, area: str | None = None, district: str | None = None):
        store = store_of(slug)
        if not area and not district:
            raise HTTPException(400, "give an area or a district")
        return {"rows": sel(store, area, district)}

    @app.post("/api/projects/{slug}/areas/{name}/checklist")
    def areas_checklist(slug: str, name: str):
        store = store_of(slug)
        sel(store, name, None)                       # 404 / 400 with the CLI's wording
        out = areas.checklist(store, name)
        return {"path": str(out), "rel": out.relative_to(store.root).as_posix(), "text": out.read_text()}

    def _batch_plan(store, req: BatchReq):
        """(items to run, notes) for an area batch, as the matching `ap library` command picks them."""
        rows = sel(store, req.area, req.district)
        if req.assets is not None:
            rows = [r for r in rows if r["asset"] in req.assets]
        notes = []
        if req.action == "sheets":
            dec = sw_library.decisions(store)
            only = sorted({r["entry"] for r in rows if r["status"] != "approved"
                           and (dec.get(r["entry"]) or {}).get("decision") not in (None, "exclude")})
            undecided = sorted({r["entry"] for r in rows if not r["decision"]})
            if undecided:
                notes.append("not decided yet (Library › Entries), skipped: " + ", ".join(undecided))
            return only, notes
        if req.action == "build":
            todo = [r["asset"] for r in rows if r["route"] == "procedural" and r["brief"] and r["status"] != "approved"
                    and (req.force or not r["fresh"])]
            nobrief = [r["asset"] for r in rows if r["route"] == "procedural" and not r["brief"] and r["status"] != "approved"]
            if nobrief:
                notes.append("no brief yet, skipped: " + ", ".join(nobrief))
            return todo, notes
        if req.action == "trellis":
            rows = [r for r in rows if r["route"] == "trellis" and r["status"] != "approved" and (req.more or not r["trellis"])]
            nochoice = [r["asset"] for r in rows if not review.chosen(store, sw_library.PLAN_NAME, r["asset"])]
            if nochoice:
                notes.append("no view chosen (Views and 3D), skipped: " + ", ".join(nochoice))
            return [r["asset"] for r in rows if r["asset"] not in nochoice], notes
        entries = sorted({r["asset"] for r in rows if r["route"] == "procedural" and r["brief"] and r["kind"] == "building"})
        have = {p.stem for p in (store.root / "library/kits").glob("*.json")}
        return [e for e in entries if e in have], notes

    @app.post("/api/projects/{slug}/areas/batch")
    def areas_batch(slug: str, req: BatchReq):
        store = store_of(slug)
        if bool(req.area) == bool(req.district):
            raise HTTPException(400, "give an area or a district")
        todo, notes = _batch_plan(store, req)
        count = len(todo)
        info = {"action": req.action, "count": count, "items": todo, "notes": notes}
        if req.dry_run:
            return info
        if not count:
            raise HTTPException(400, "nothing to do" + (": " + "; ".join(notes) if notes else ""))
        tag = {"area": req.area or req.district, "n": len(todo)}

        def failures(done_failed, what):
            done, failed = done_failed
            if failed:
                raise RuntimeError(f"{what}: {done} done, {len(failed)} failed ({'; '.join(failed)}). "
                                   "Run it again: finished items are skipped.")
            return {"done": done, "failed": []}

        def each(items, fn):
            done, failed = 0, []
            for x in items:
                check_canceled()
                try:
                    fn(x)
                    done += 1
                except Exception as e:                   # noqa: BLE001 - reported; the rest go on
                    failed.append(f"{x}: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")
            return done, failed

        if req.action == "sheets":
            def run():
                plan = sw_library.asset_plan(store, only=todo)
                inc = {x.id for x in plan.assets if x.include}
                units = [u for u in s2_refs.units(store, sw_library.PLAN_NAME) if u.key in inc and not s2_refs.sheets(store, u)]
                return failures(each(units, lambda u: s2_refs.generate(store, u, n=2)), "sheets")
            return jobs.submit("library.sheets", slug, run, lane="comfy", tag=tag,
                               gated=on_comfy(store, "references")).public()
        if req.action == "build":
            def run():
                return failures(each(todo, lambda a: kit_proc.build_any(store, a, log=lambda s: None, surface=None)), "build")
            return jobs.submit("library.build", slug, run, lane="cpu", tag=tag).public()
        if req.action == "trellis":
            def run():
                return failures(each(todo, lambda a: s5_3d.run(store, sw_library.PLAN_NAME, a, mode=req.mode)), "TRELLIS")
            return jobs.submit("library.trellis", slug, run, lane="gpu0", tag=tag).public()

        def run():
            from .. import library_instances
            return failures(each(todo, lambda e: library_instances.build_entries(store, [e], None, 600.0,
                                                                                 skip_fresh=not req.force)), "instances")
        return jobs.submit("library.instances", slug, run, lane="cpu", tag=tag).public()

    @app.post("/api/projects/{slug}/library/asset/{asset}/brief-build")
    def library_brief_build(slug: str, asset: str, req: BriefBuildReq):
        """The brief (from a sheet, as `ap library brief`) when there is none, then the build."""
        store = store_of(slug)
        have = kit_proc.spec_path(store, asset).is_file()
        if not have or req.force_brief:
            data = sw_library.load(store)
            if data is None:
                raise HTTPException(400, "no library plan yet")
            if not req.sheet:
                units = [u for u in s2_refs.units(store, sw_library.PLAN_NAME) if u.key == asset]
                sheets = s2_refs.sheets(store, units[0]) if units else []
                if not sheets:
                    raise HTTPException(400, f"{asset} has no sheets: make sheets first")
                # the newest starred sheet, as Library › Reference sheets' Brief & build uses; else the first
                stars = review.load(store)["stars"]
                starred = [k for k in sheets if stars.get(k)]
                req = req.model_copy(update={"sheet": (starred or sheets[:1])[-1]})

        def run():
            from .. import library_review as lr
            if not have or req.force_brief:
                make_brief(store, asset, req)
                lr.brief_edited(store, asset)                # a new brief: an Approved asset needs review again
            check_canceled()
            return lr.rebuild(store, asset, log=lambda s: None)
        return jobs.submit("library.brief", slug, run, lane="gpu0", tag={"asset": asset}).public()

    def make_brief(store, asset, req):
        """The body of `ap library brief` (library_entries.brief_from_sheet: one form logic for
        every button), with the form and size the user chose, else theirs or the catalog's."""
        from .. import library_entries
        library_entries.brief_from_sheet(store, asset, req.sheet, req.view, form=req.form, dims=req.dims)

    # --- preflight -------------------------------------------------------------------------

    @app.post("/api/preflight")
    def preflight_run(req: PreflightReq):
        out = []
        for c in preflight.run(ue=req.ue):
            fix = None
            if c.name.startswith("ComfyUI (") and not c.ok:
                fix = {"kind": "start_comfy", "label": "Start ComfyUI"}
            elif not c.ok:
                h = next((h for h in INSTALL_HINTS if c.name.startswith(h[0])), None)
                if h:
                    fix = {"kind": "install", "command": h[1], "doc": h[2]}
            out.append({"name": c.name, "ok": c.ok, "detail": c.detail, "required": c.required, "fix": fix})
        failed = [c for c in out if not c["ok"] and c["required"]]
        return {"checks": out, "ok": not failed, "ue": req.ue, "backend": config.backends()["comfyui"].get("backend")}

    # --- library: pending slots, silhouette, review round -------------------------------------

    @app.post("/api/projects/{slug}/library/pending")
    def library_pending(slug: str, req: PendingReq):
        from .. import look
        store = store_of(slug)
        return jobs.submit("library.pending", slug, lambda: look.pending_slots(store, req.shot, draft=not req.no_draft),
                           lane="cpu", tag={"shot": req.shot}).public()

    @app.post("/api/projects/{slug}/library/silhouette")
    def library_silhouette(slug: str, req: SilhouetteReq):
        from .. import look
        store = store_of(slug)
        if not req.entries:
            raise HTTPException(400, "name the entries to check")

        def run():
            out = {}
            for e in req.entries:
                check_canceled()
                out[e] = look.silhouette_of(store, e, req.shot)
            return out
        return jobs.submit("library.silhouette", slug, run, lane="cpu", tag={"shot": req.shot}).public()

    @app.get("/api/projects/{slug}/library/review/tasks")
    def library_review_tasks(slug: str):
        from .. import library_review as lr
        store = store_of(slug)
        return {"round": lr.current_round(store), "text": lr.task_list(store)}

    @app.post("/api/projects/{slug}/library/review/done")
    def library_review_done(slug: str, req: DoneReq):
        from .. import library_review as lr
        if not req.assets:
            raise HTTPException(400, "name the assets")
        try:
            return {"round": lr.done(store_of(slug), req.assets)}
        except (KeyError, ValueError) as e:
            raise HTTPException(400, str(e).strip('"'))

    # --- frames, shots, site --------------------------------------------------------------------

    @app.post("/api/projects/{slug}/frames/lock")
    def frames_lock(slug: str, req: LockReq):
        store = store_of(slug)
        if not (req.unlock or req.frame or req.greybox):
            raise HTTPException(400, "give a frame, greybox or unlock")
        try:
            key = s0_frames.lock(store, req.shot, None if req.unlock else s0_frames.LOCK_GREYBOX if req.greybox else req.frame)
        except (ValueError, KeyError, FileNotFoundError) as e:
            raise HTTPException(400, str(e).strip('"'))
        return {"shot": req.shot, "locked": key}

    @app.post("/api/projects/{slug}/shots/add")
    def shots_add(slug: str, req: AddShotReq):
        from ..schema import ShotSpec
        store = store_of(slug)
        w, _, h = req.res.lower().partition("x")
        try:
            shot = ShotSpec(id=req.id, tier=req.tier, pos=req.pos, look_at=req.look_at, lens_mm=req.lens,
                            resolution=(int(w), int(h or w)), district=req.district, notes=req.notes)
        except (ValueError, TypeError) as e:
            raise HTTPException(400, str(e))
        return jobs.submit("shots.add", slug, lambda: (sw_site.add_shot(store, shot), req.id)[1], lane="cpu",
                           tag={"shot": req.id}).public()

    @app.get("/api/projects/{slug}/shots")
    def shots_list(slug: str):
        store = store_of(slug)
        if not (sw_site.site_dir(store) / "greybox.json").is_file():
            return {"shots": []}
        return {"shots": sw_shots.status(store)}

    @app.post("/api/projects/{slug}/site/vegetation")
    def site_vegetation(slug: str, req: VegetationReq):
        from .. import vegetation
        store = store_of(slug)

        def run():
            rec = vegetation.street_tree_slots(store)
            prev = [str(p) for p in vegetation.preview(store, req.preview)] if req.preview else []
            return {"counts": rec["counts"], "path": str(vegetation.path(store)), "previews": prev}
        return jobs.submit("site.vegetation", slug, run, lane="cpu").public()

    @app.post("/api/projects/{slug}/site/overlap")
    def site_overlap(slug: str, req: OverlapReq):
        store = store_of(slug)
        return jobs.submit("site.overlap", slug, lambda: sw_site.plan_overlap(store, req.shot), lane="cpu",
                           tag={"shot": req.shot}).public()

    # --- Unreal (these run the editor when a user clicks; tests monkeypatch the ue functions) ----

    @app.get("/api/projects/{slug}/ue/stale")
    def ue_stale(slug: str):
        from .. import layout_history
        store = store_of(slug)
        if not ue.uproject(store).is_file():
            raise HTTPException(400, "no UE project yet")
        cls = layout_history.check(store)
        return {"current": len(cls["current"]), "stale": sorted(cls["stale"]), "hand": sorted(cls["hand"])}

    @app.get("/api/projects/{slug}/ue/looks")
    def ue_looks(slug: str):
        from .. import look
        d = look.preset_dir(store_of(slug))
        return {"looks": sorted(p.stem for p in d.glob("*.json") if p.stem != "defaults")} if d.is_dir() else {"looks": []}

    @app.post("/api/projects/{slug}/ue/init")
    def ue_init(slug: str):
        store = store_of(slug)
        return jobs.submit("ue.init", slug, lambda: str(ue.init(store)), lane="cpu").public()

    @app.post("/api/projects/{slug}/ue/import")
    def ue_import(slug: str, req: UeImportReq):
        store = store_of(slug)
        if req.only_stale and not req.reset_layout:
            raise HTTPException(400, "only stale goes with reset layout")
        if (req.reset_layout or req.prune) and not ue.backups(store) and not req.no_backup_ok:
            raise HTTPException(409, "back up the UE project first: this import discards work done in Unreal "
                                     "(Pipeline › Back up Unreal project)")
        up = ue.uproject(store)
        if up.is_file() and ue.editor_running(up):
            raise HTTPException(409, f"an Unreal Editor has {up.name} open: save and close it first")

        def run():
            only = None
            if req.only_stale:
                from .. import layout_history
                only = sorted(layout_history.check(store)["stale"])
            rep = ue.import_(store, reset_layout=req.reset_layout, prune=req.prune, expect=req.expect,
                             confirm=None, reset_only=only, vegetation=req.vegetation)
            return {"ok": rep["ok"], "imported": len(rep["assets"]["imported"]), "unchanged": len(rep["assets"]["skipped"]),
                    "created": len(rep["actors"]["created"]), "kept": len(rep["actors"]["kept_transform"]),
                    "reset": len(rep["actors"]["reset"]), "pruned": len(rep.get("pruned", [])),
                    "orphans": len(rep.get("orphans", [])), "prune_refused": rep.get("prune_refused"),
                    "warnings": (rep["sample_errors"] + rep["warnings"] + rep.get("log_errors", []))[:20],
                    "seconds": rep["seconds"]}
        return jobs.submit("ue.import", slug, run, lane="cpu",
                           tag={"reset": req.reset_layout, "prune": req.prune}).public()

    @app.post("/api/projects/{slug}/ue/render")
    def ue_render(slug: str, req: UeRenderReq):
        store = store_of(slug)
        return jobs.submit("ue.render", slug, lambda: ue.render_shots(store, req.shots or None), lane="cpu",
                           tag={"shots": req.shots}).public()

    @app.post("/api/projects/{slug}/ue/pull-layout")
    def ue_pull(slug: str):
        store = store_of(slug)
        return jobs.submit("ue.pull", slug, lambda: {"actors": len(ue.pull_layout(store)["actors"])}, lane="cpu").public()

    @app.post("/api/projects/{slug}/ue/look")
    def ue_look(slug: str, req: UeLookReq):
        from .. import look
        store = store_of(slug)
        return jobs.submit("ue.look", slug, lambda: look.run(store, req.name)["record"], lane="cpu",
                           tag={"look": req.name}).public()

    # --- terrain -------------------------------------------------------------------------------

    @app.post("/api/projects/{slug}/terrain/{action}")
    def terrain(slug: str, action: Literal["analyse", "build", "outer", "apply", "landscape", "ue"], req: TerrainReq):
        from .. import terrain_build as tb
        from .. import terrain_plan as tp
        store = store_of(slug)
        if action in ("apply", "ue") and not req.confirm:
            raise HTTPException(409, "this changes the layout" if action == "apply" else "this changes the Unreal project")
        fns = {"analyse": lambda: tp.analyse(store), "build": lambda: tb.build(store),
               "outer": lambda: [str(tb.rebuild_outer(store)), str(tb.outer_glb(store))],
               "apply": lambda: str(tb.apply(store)),
               "landscape": lambda: [str(tb.landscape_tiles(store)), str(tb.outer_glb(store))],
               "ue": lambda: str(tb.ue_assets(store))}

        def run():
            r = fns[action]()
            if action == "analyse":
                return {"shots": [{"shot": s["shot"], "lock": s["lock"], "bearing_span": s.get("bearing_span"),
                                   "elevation_span": s.get("elevation_span")} for s in r["shots"]]}
            if action == "build":
                return {"ranges": [{k: x.get(k) for k in ("name", "height_factor", "width_factor", "foothill_factor")}
                                   for x in r["ranges"]],
                        "cells_over_sky_after_erosion": r["cells_over_sky_after_erosion"], "max_z": r["max_z"]}
            return r
        return jobs.submit(f"terrain.{action}", slug, run, lane="cpu").public()

    # --- demo ----------------------------------------------------------------------------------

    @app.post("/api/demo")
    def demo(req: DemoReq):
        if not SLUG.fullmatch(req.slug):
            raise HTTPException(400, "slug: lower-case letters, digits, dashes and underscores")
        args = [req.mode, req.slug]
        if req.mode == "area":
            if not req.shot or not req.entries:
                raise HTTPException(400, "area demo: give a shot and at least one entry")
            args = ["area", req.slug, req.shot, *req.entries]
            store_of(req.slug)
        elif (config.projects_dir() / req.slug).exists():
            raise HTTPException(409, f"project {req.slug} exists already: pick a new slug")
        if not comfy_up():
            from ..comfy import service as comfy_service
            raise comfy_service.ComfyDown("ComfyUI is not answering")

        def run():
            job = current_job()
            log = config.ap_root() / "logs" / f"demo-{req.mode}-{int(time.time())}.log"
            note = (lambda s: setattr(job, "note", s[:200])) if job else (lambda s: None)
            rc = _run_demo_script(args, note, log, (lambda fn: job.on_cancel(fn)) if job else (lambda fn: None))
            if rc != 0:
                raise RuntimeError(f"demo.sh {req.mode} exited {rc}; log: {log}")
            return {"log": str(log)}
        return jobs.submit("demo", req.slug, run, lane="cpu", tag={"mode": req.mode}).public()

    # --- VLM -------------------------------------------------------------------------------------

    @app.post("/api/vlm/up")
    def vlm_up(req: VlmUpReq):
        def run():
            job = current_job()
            st = vlm.up(req.model, gpu=req.gpu, log=(lambda s: setattr(job, "note", str(s)[:200])) if job else print)
            return {"model": st["model"], "gpu": st["gpu"], "url": st["url"]}
        return jobs.submit("vlm.up", "", run, lane="gpu0", tag={"model": req.model or "default"}).public()

    @app.post("/api/vlm/down")
    def vlm_down(req: VlmDownReq):
        def run():
            job = current_job()
            return {"result": str(vlm.down(force=req.force, log=(lambda s: setattr(job, "note", str(s)[:200])) if job else print))}
        return jobs.submit("vlm.down", "", run, lane="gpu0").public()

    # --- desktop launcher ------------------------------------------------------------------------

    @app.get("/api/launcher")
    def launcher_status():
        return launcher.status()

    @app.post("/api/launcher/install")
    def launcher_install():
        return launcher.install()

    @app.post("/api/launcher/remove")
    def launcher_remove():
        return {"removed": launcher.remove()}

    # --- project repo: the end-of-day commit (each project folder is its own private git repo) ---

    def _git(store, *args, timeout=60):
        return subprocess.run(["git", "-C", str(store.root), *args], capture_output=True, text=True, timeout=timeout)

    @app.get("/api/projects/{slug}/git")
    def project_git(slug: str):
        """What a commit of the project repo would take: changed files (its .gitignore keeps media out)."""
        store = store_of(slug)
        if _git(store, "rev-parse", "--is-inside-work-tree").returncode != 0:
            return {"repo": False, "changes": []}
        st = _git(store, "status", "--porcelain=v1", "-uall")
        last = _git(store, "log", "-1", "--format=%h %s (%cr)")
        return {"repo": True, "changes": [ln for ln in st.stdout.splitlines() if ln.strip()][:500],
                "last": last.stdout.strip()}

    class CommitReq(BaseModel):
        message: str

    @app.post("/api/projects/{slug}/git/commit")
    def project_git_commit(slug: str, req: CommitReq):
        """git add -A and commit in the project's own repo (never the tool repo)."""
        store = store_of(slug)
        msg = " ".join(req.message.split())
        if not msg:
            raise HTTPException(400, "write a commit message")
        if _git(store, "rev-parse", "--show-toplevel").stdout.strip() != str(store.root.resolve()):
            raise HTTPException(400, f"{store.root} is not a git repo of its own: run git init there first")
        _git(store, "add", "-A")
        r = _git(store, "commit", "-m", msg)
        if r.returncode != 0:
            out = (r.stdout + r.stderr).strip()
            raise HTTPException(409 if "nothing to commit" in out else 400, out[-600:] or "git commit failed")
        return {"committed": _git(store, "log", "-1", "--format=%h %s").stdout.strip()}
