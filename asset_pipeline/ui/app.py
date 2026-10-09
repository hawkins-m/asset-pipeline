"""Local review UI (FastAPI + plain HTML/JS). Run with `ap ui`; binds 127.0.0.1 only.

All state is on disk (project.json, review.json); the server holds only job status.
"""
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, Form, HTTPException, UploadFile
from fastapi import Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, TypeAdapter, ValidationError

from .. import config, review, vlm
from ..comfy.client import ComfyError
from ..jobs import JobQueue
from ..project import ProjectStore, read_json
from .. import catalog as cat
from ..schema import AssetPlan, District, Material, Overlay, RepetitionChecks, Typology, Variation
from ..stages import s0_frames, s0_style, s1_plan, s2_refs, s3_views, s5_3d, s6_cleanup, sw_shots, sw_site

STATIC = Path(__file__).parent / "static"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}


class NewProject(BaseModel):
    slug: str
    brief: str = ""


class ExploreReq(BaseModel):
    brief: str | None = None
    n: int = 8
    seed: int | None = None


class DeriveReq(BaseModel):
    scene: str
    nouns: list[str]
    per_noun: int = 2
    style_text: str = ""


class StarReq(BaseModel):
    path: str
    starred: bool = True


class AnalyzeReq(BaseModel):
    scene: str
    force: bool = False


class RefsReq(BaseModel):
    plan: str | None = None
    unit: str | None = None       # None: every unit without sheets
    n: int = 2


class CutReq(BaseModel):
    sheet: str | None = None      # None: every starred sheet without views


class ChooseReq(BaseModel):
    plan: str
    asset: str
    view: str | None = None       # None clears the choice


class UsageReq(BaseModel):
    plan: str
    asset: str
    usage: Literal["game", "cine", "hero"]


class ThreeDReq(BaseModel):
    plan: str
    asset: str
    mode: str = "1024_cascade"
    seed: int = 42


class CleanupReq(BaseModel):
    plan: str
    asset: str
    glb: str | None = None        # None: the asset's newest 3D result
    fit: Literal["height", "geomean"] = "height"


class SiteReq(BaseModel):
    force: bool = False


class ShotsReq(BaseModel):
    shots: list[str] | None = None    # None: every shot


class FramesReq(BaseModel):
    shot: str | None = None       # None: every shot without frames
    n: int = 4
    refs: list[str] = []          # approved frames added as Redux references
    ref_strength: float = 0.08


class RoleReq(BaseModel):
    path: str
    role: Literal["design_ref", "source"] | None = None
    note: str | None = None


class PromptReq(BaseModel):
    append: str = ""
    override: str | None = None     # empty / None: no override


class RefsEditReq(BaseModel):
    refs: list[str] = []
    ref_strength: float = 0.05


class CameraReq(BaseModel):
    nudge_pos: tuple[float, float, float] = (0.0, 0.0, 0.0)     # right, up, forward (m)
    nudge_target: tuple[float, float, float] = (0.0, 0.0, 0.0)
    lens_override: float | None = None
    tier_override: Literal["wide", "medium", "tight"] | None = None


class RegenReq(BaseModel):
    n: int = 4
    refs: list[str] = []
    ref_strength: float = 0.08


# catalog sections editable in the UI: (validator, list of id'd rows?)
CATALOG = {"districts": (TypeAdapter(list[District]), True), "materials": (TypeAdapter(list[Material]), True),
           "typologies": (TypeAdapter(list[Typology]), True), "overlays": (TypeAdapter(dict[str, Overlay]), False),
           "checks": (TypeAdapter(RepetitionChecks), False), "variation": (TypeAdapter(Variation), False)}


def _shot_edit(store: ProjectStore, shot: str) -> dict:
    """The user's edits to a shot next to the auto values (for the mine / auto badges)."""
    spec = next((s for s in sw_site.load_layout(store).shots if s.id == shot), None)
    if spec is None:
        return {"in_layout": False}
    return {"in_layout": True, "lens_auto": spec.lens_mm, "lens_override": spec.lens_override,
            "tier_auto": spec.tier, "tier_override": spec.tier_override, "nudge_pos": list(spec.nudge_pos),
            "nudge_target": list(spec.nudge_target), "camera_edited": spec.camera_edited(),
            "refs": list(spec.refs), "ref_strength": spec.ref_strength}


class DraftReq(BaseModel):
    groups: list[str] | None = None


class TextReq(BaseModel):
    text: str


class AnchorReq(BaseModel):
    strength: float = s0_style.ANCHOR_STRENGTH
    style_text: str | None = None   # None keeps the project's current style text


def _store(slug: str) -> ProjectStore:
    try:
        return ProjectStore.open(slug)
    except (FileNotFoundError, ValueError) as e:
        raise HTTPException(404, str(e))


def _images(store: ProjectStore, sub: str) -> list[str]:
    d = store.root / sub
    if not d.is_dir():
        return []
    return sorted(p.relative_to(store.root).as_posix() for p in d.rglob("*")
                  if p.suffix.lower() in IMAGE_EXT and not p.name.startswith("tmp"))


def _uncut(store: ProjectStore, sheet: str) -> bool:
    try:
        unit = s3_views._unit_for_sheet(store, sheet)
    except ValueError:
        return False
    if unit.kind == "material":
        return False
    meta = read_json(s3_views.view_dir(store, unit) / "meta.json", default=None) or {"views": []}
    return not any(v["sheet"] == sheet for v in meta["views"])


def create_app(jobs: JobQueue | None = None) -> FastAPI:
    app = FastAPI(title="asset-pipeline")
    jobs = jobs or JobQueue()

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/projects")
    def projects():
        root = config.projects_dir()
        return sorted(p.name for p in root.iterdir() if (p / "project.json").is_file()) if root.is_dir() else []

    @app.post("/api/projects")
    def new_project(req: NewProject):
        try:
            store = ProjectStore.create(req.slug, brief=req.brief)
        except (ValueError, FileExistsError) as e:
            raise HTTPException(400, str(e))
        return store.load().model_dump(mode="json")

    @app.get("/api/projects/{slug}")
    def project(slug: str):
        store = _store(slug)
        batches = []
        for b in sorted((store.root / s0_style.EXPLORE).glob("batch_*")):
            meta = read_json(b / "meta.json", default={})
            batches.append({"name": b.name, "prompt": meta.get("prompt", ""),
                            "images": _images(store, f"{s0_style.EXPLORE}/{b.name}")})
        derived = []
        for d in sorted((store.root / s0_style.DERIVE).glob("*")):
            meta = read_json(d / "meta.json", default={})
            derived.append({"scene": meta.get("scene"), "dir": d.name,
                            "images": _images(store, f"{s0_style.DERIVE}/{d.name}")})
        return {"project": store.load().model_dump(mode="json"),
                "stars": review.load(store)["stars"],
                "explore": batches, "derived": derived, "moodboard": s0_style.moodboard(store),
                "jobs": [j.public() for j in jobs.list(slug)]}

    @app.post("/api/projects/{slug}/role")
    def frame_role(slug: str, req: RoleReq):
        store = _store(slug)
        try:
            return {"key": review.set_role(store, req.path, req.role, req.note), "role": req.role}
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/projects/{slug}/star")
    def star(slug: str, req: StarReq):
        store = _store(slug)
        try:
            return {"key": review.set_star(store, req.path, req.starred), "starred": req.starred}
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/projects/{slug}/style/explore")
    def explore(slug: str, req: ExploreReq):
        store = _store(slug)
        job = jobs.submit("style.explore", slug, lambda: str(
            s0_style.explore(store, req.brief, n=req.n, seed=req.seed).relative_to(store.root)),
            lane="comfy")
        return job.public()

    @app.post("/api/projects/{slug}/style/derive")
    def derive(slug: str, req: DeriveReq):
        store = _store(slug)
        try:
            review.rel(store, req.scene)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        job = jobs.submit("style.derive", slug, lambda: str(s0_style.derive(
            store, req.scene, req.nouns, per_noun=req.per_noun,
            style_text=req.style_text).relative_to(store.root)), lane="comfy",
            tag={"scene": req.scene})
        return job.public()

    @app.post("/api/projects/{slug}/style/anchor")
    def anchor(slug: str, req: AnchorReq):
        store = _store(slug)
        try:
            a = s0_style.save_anchor(store, strength=req.strength, style_text=req.style_text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return a.model_dump(mode="json")

    @app.post("/api/projects/{slug}/moodboard")
    async def upload_moodboard(slug: str, files: list[UploadFile], group: str = Form("moodboard")):
        store = _store(slug)
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for f in files:
                p = Path(tmp) / Path(f.filename or "image.png").name
                p.write_bytes(await f.read())
                paths.append(p)
            try:
                return {"keys": s0_style.import_moodboard(store, group, paths)}
            except ValueError as e:
                raise HTTPException(400, str(e))

    @app.post("/api/projects/{slug}/style/draft")
    def style_draft(slug: str, req: DraftReq):
        store = _store(slug)
        if not s0_style.moodboard(store):
            raise HTTPException(400, "no moodboard images yet")
        return jobs.submit("style.draft", slug, lambda: s0_style.draft_style_text(store, req.groups).style_text,
                           lane="gpu0").public()

    @app.post("/api/projects/{slug}/style/text")
    def style_text(slug: str, req: TextReq):
        return s0_style.set_style_text(_store(slug), req.text).model_dump(mode="json")

    @app.get("/api/projects/{slug}/plans")
    def plans(slug: str):
        store = _store(slug)
        out = []
        for key in s1_plan.scenes(store):
            name = s1_plan.plan_name(key)
            plan = s1_plan.load(store, name)
            out.append({"name": name, "scene": key,
                        "plan": plan.model_dump(mode="json") if plan else None})
        return out

    @app.post("/api/projects/{slug}/plans/analyze")
    def analyze(slug: str, req: AnalyzeReq):
        store = _store(slug)
        try:
            key = review.rel(store, req.scene)
            old = s1_plan.load(store, s1_plan.plan_name(key))
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        if old and old.edited and not req.force:  # checked here too so the UI can confirm
            raise HTTPException(409, "this plan has edits; re-analysing replaces them")
        def run():
            name = s1_plan.plan_name(s1_plan.analyze(store, key, force=req.force).scene)
            try:
                s1_plan.refine_boxes(store, name)
            except ComfyError:
                pass  # ComfyUI not up: the LLM's boxes stay; "Refine boxes" later
            return name
        job = jobs.submit("plan.analyze", slug, run, lane="gpu0", tag={"scene": key})
        return job.public()

    @app.post("/api/projects/{slug}/plans/{name}/refine")
    def refine(slug: str, name: str):
        store = _store(slug)
        try:
            if not s1_plan.load(store, name):
                raise HTTPException(404, f"no plan {name}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return jobs.submit("plan.refine", slug, lambda: s1_plan.refine_boxes(store, name)
                           and name, lane="comfy", tag={"plan": name}).public()

    @app.put("/api/projects/{slug}/plans/{name}")
    def save_plan(slug: str, name: str, plan: AssetPlan):
        store = _store(slug)
        try:
            old = s1_plan.load(store, name)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not old:
            raise HTTPException(404, f"no plan {name}")
        plan.scene, plan.llm, plan.created = old.scene, old.llm, old.created
        return s1_plan.save(store, name, plan, edited=True).model_dump(mode="json")

    @app.post("/api/projects/{slug}/scenes")
    async def upload_scene(slug: str, file: UploadFile):
        store = _store(slug)
        try:
            return {"scene": s1_plan.import_scene(store, Path(file.filename or "scene.png"),
                                                  await file.read())}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/projects/{slug}/refs")
    def refs(slug: str):
        store = _store(slug)
        return [{"plan": u.plan, "key": u.key, "kind": u.kind, "title": u.title,
                 "assets": [a.model_dump(mode="json", include={"id", "name", "category", "count",
                                                                "dimensions", "description"})
                            for a in u.assets],
                 "sheets": s2_refs.sheets(store, u)} for u in s2_refs.units(store)]

    @app.post("/api/projects/{slug}/refs/generate")
    def refs_generate(slug: str, req: RefsReq):
        store = _store(slug)
        if req.unit:
            match = [u for u in s2_refs.units(store, req.plan) if u.key == req.unit]
            if not match:
                raise HTTPException(404, f"no unit {req.unit}")
            fn = lambda: len(s2_refs.generate(store, match[0], n=req.n))  # noqa: E731
        else:
            fn = lambda: len(s2_refs.generate_missing(store, n=req.n, plan=req.plan))  # noqa: E731
        tag = {"plan": req.plan, "unit": req.unit} if req.unit else {"missing": True}
        return jobs.submit("refs.generate", slug, fn, lane="comfy", tag=tag).public()

    # --- stages 3-4: views, review, 3D -------------------------------------------------

    @app.get("/api/projects/{slug}/review")
    def review_state(slug: str):
        store = _store(slug)
        stars = review.load(store)["stars"]
        pending = [k for k in review.starred(store, s2_refs.REFS + "/") if _uncut(store, k)]
        assets = []
        for name in [s1_plan.plan_name(k) for k in s1_plan.scenes(store)]:
            p = s1_plan.load(store, name)
            for a in (p.assets if p else []):
                if not a.include or a.category == "terrain":
                    continue
                assets.append({"plan": name, "asset": a.model_dump(mode="json", include={
                    "id", "name", "category", "count", "dimensions", "kit", "usage"}),
                    "views": s3_views.views(store, name, a.id),
                    "chosen": review.chosen(store, name, a.id),
                    "results": s5_3d.results(store, name, a.id),
                    "cleanups": s6_cleanup.results(store, name, a.id)})
        return {"pending_sheets": pending, "assets": assets,
                "starred_sheets": sum(1 for k, v in stars.items() if v and k.startswith(s2_refs.REFS + "/"))}

    @app.post("/api/projects/{slug}/views/cut")
    def views_cut(slug: str, req: CutReq):
        store = _store(slug)
        if req.sheet:
            try:
                review.rel(store, req.sheet)
            except (ValueError, FileNotFoundError) as e:
                raise HTTPException(400, str(e))
            fn = lambda: len(s3_views.cut(store, req.sheet))  # noqa: E731
            tag = {"sheet": req.sheet}
        else:
            fn = lambda: s3_views.cut_starred(store, log=lambda m: None)  # noqa: E731
            tag = {"starred": True}
        return jobs.submit("views.cut", slug, fn, lane="comfy", tag=tag).public()

    @app.post("/api/projects/{slug}/review/choose")
    def choose(slug: str, req: ChooseReq):
        store = _store(slug)
        try:
            return {"chosen": review.choose(store, req.plan, req.asset, req.view)}
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))

    @app.post("/api/projects/{slug}/review/usage")
    def usage(slug: str, req: UsageReq):
        store = _store(slug)
        try:
            s1_plan.set_usage(store, req.plan, req.asset, req.usage)
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        return {"usage": req.usage}

    @app.post("/api/projects/{slug}/3d")
    def make_3d(slug: str, req: ThreeDReq):
        store = _store(slug)
        if not review.chosen(store, req.plan, req.asset):
            raise HTTPException(400, "choose a view first")
        if req.mode not in s5_3d.MODES:
            raise HTTPException(400, f"mode must be one of {', '.join(s5_3d.MODES)}")
        return jobs.submit("3d.trellis", slug, lambda: s5_3d.run(store, req.plan, req.asset,
                                                                 mode=req.mode, seed=req.seed),
                           lane="gpu0", tag={"plan": req.plan, "asset": req.asset}).public()

    @app.get("/api/vlm")
    def vlm_status():
        return vlm.status()

    @app.post("/api/projects/{slug}/cleanup")
    def cleanup(slug: str, req: CleanupReq):
        store = _store(slug)
        if not req.glb and not s5_3d.results(store, req.plan, req.asset):
            raise HTTPException(400, "make 3D first")
        return jobs.submit("cleanup", slug, lambda: s6_cleanup.run(store, req.plan, req.asset,
                                                                   glb=req.glb, fit=req.fit),
                           lane="cpu", tag={"plan": req.plan, "asset": req.asset}).public()

    # --- world mode: site greybox and shots ----------------------------------------------

    @app.get("/api/projects/{slug}/site")
    def site(slug: str):
        store = _store(slug)
        has_layout = (sw_site.site_dir(store) / "layout.json").is_file()
        has_greybox = (sw_site.site_dir(store) / "greybox.json").is_file()
        out = {"layout": has_layout, "summary": None, "shots": [], "previews": [],
               "blend": str(sw_site.blend_path(store))}
        if has_greybox:
            summ = sw_site.summary(store)
            summ.pop("pieces")
            out["summary"] = summ
            out["previews"] = _images(store, f"{sw_site.SITE}/preview")
            for r in sw_shots.status(store):
                d = sw_shots.shot_dir(store, r["id"])
                r["passes"] = {p: f"{sw_shots.SHOTS}/{r['id']}/{p}.png"
                               for p in ("preview", "depth", "canny", "ids") if (d / f"{p}.png").is_file()}
                out["shots"].append(r)
        return out

    @app.post("/api/projects/{slug}/site/init")
    def site_init(slug: str, req: SiteReq):
        store = _store(slug)
        try:
            layout = sw_site.init(store, force=req.force)
        except FileExistsError as e:
            raise HTTPException(409, str(e))
        return {"plots": len(layout.plots), "shots": len(layout.shots)}

    @app.post("/api/projects/{slug}/site/build")
    def site_build(slug: str, req: SiteReq):
        store = _store(slug)
        if not (sw_site.site_dir(store) / "layout.json").is_file():
            raise HTTPException(400, "no site/layout.json: initialise the site first")
        if sw_site.edited(store) and not req.force:  # checked here too so the UI can confirm
            raise HTTPException(409, "greybox.blend was edited in Blender; rebuilding replaces those edits")
        return jobs.submit("site.build", slug, lambda: len(sw_site.build(store, force=req.force)["slots"]),
                           lane="cpu").public()

    @app.post("/api/projects/{slug}/site/extract")
    def site_extract(slug: str):
        store = _store(slug)
        return jobs.submit("site.extract", slug, lambda: len(sw_site.extract(store)["slots"]), lane="cpu").public()

    @app.post("/api/projects/{slug}/site/preview")
    def site_preview(slug: str):
        store = _store(slug)
        return jobs.submit("site.preview", slug, lambda: len(sw_shots.preview_site(store)), lane="cpu").public()

    @app.post("/api/projects/{slug}/shots/render")
    def shots_render(slug: str, req: ShotsReq):
        store = _store(slug)
        tag = {"shot": req.shots[0]} if req.shots and len(req.shots) == 1 else {"all": True}
        return jobs.submit("shots.render", slug, lambda: len(sw_shots.render(store, req.shots)),
                           lane="cpu", tag=tag).public()

    @app.get("/api/projects/{slug}/frames")
    def frames(slug: str):
        store = _store(slug)
        if not (sw_site.site_dir(store) / "greybox.json").is_file():
            return {"shots": [], "settings": s0_frames.settings(store).model_dump()}
        out = []
        for r in sw_shots.status(store):
            d = f"{sw_shots.SHOTS}/{r['id']}"
            try:
                parts = s0_frames.prompt_parts(store, r["id"])
            except FileNotFoundError:
                parts = None
            out.append(r | {"depth": f"{d}/depth.png", "canny": f"{d}/canny.png", "preview": f"{d}/preview.png",
                            "prompt": parts["prompt"] if parts else None, "prompt_parts": parts,
                            "edit": _shot_edit(store, r["id"]), "batches": s0_frames.batches(store, r["id"]),
                            "mood": r["tier"] in s0_frames.MOOD_TIERS})
        return {"shots": out, "settings": s0_frames.settings(store).model_dump()}

    @app.post("/api/projects/{slug}/frames/generate")
    def frames_generate(slug: str, req: FramesReq):
        store = _store(slug)
        rows = {r["id"]: r for r in sw_shots.status(store)} if (sw_site.site_dir(store) / "greybox.json").is_file() else {}
        if req.shot:
            if req.shot not in rows:
                raise HTTPException(404, f"no shot {req.shot}")
            todo = [req.shot]
        else:
            todo = [s for s in rows if not s0_frames.batches(store, s)]
        bad = [s for s in todo if not rows[s]["rendered"] or rows[s]["stale"]]
        if bad:
            raise HTTPException(400, f"render the passes first (missing or stale): {', '.join(bad)}")
        if not todo:
            raise HTTPException(400, "every shot has frames already")
        fn = lambda: [str(s0_frames.generate(store, s, n=req.n, refs=req.refs, ref_strength=req.ref_strength)  # noqa: E731
                          .relative_to(store.root)) for s in todo]
        tag = {"shot": req.shot} if req.shot else {"missing": True}
        return jobs.submit("frames.generate", slug, fn, lane="comfy", tag=tag).public()

    # --- per-shot edits (prompt, camera) and regenerate ------------------------------------

    @app.put("/api/projects/{slug}/shots/{shot}/prompt")
    def shot_prompt(slug: str, shot: str, req: PromptReq):
        store = _store(slug)
        try:
            sw_site.update_shot(store, shot, prompt_append=req.append.strip(),
                                prompt_override=(req.override or "").strip() or None)
        except KeyError as e:
            raise HTTPException(404, str(e))
        try:
            return s0_frames.prompt_parts(store, shot)
        except FileNotFoundError:
            return {}

    @app.put("/api/projects/{slug}/shots/{shot}/refs")
    def shot_refs(slug: str, shot: str, req: RefsEditReq):
        store = _store(slug)
        try:
            refs = [review.rel(store, r) for r in req.refs]
            spec = sw_site.update_shot(store, shot, refs=refs, ref_strength=req.ref_strength)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except (ValueError, FileNotFoundError, ValidationError) as e:
            raise HTTPException(400, str(e))
        return {"refs": spec.refs, "ref_strength": spec.ref_strength}

    @app.put("/api/projects/{slug}/shots/{shot}/camera")
    def shot_camera(slug: str, shot: str, req: CameraReq):
        """Save the camera edits and write the camera into the .blend (a CPU job); the
        shot's passes go stale, the others stay valid."""
        store = _store(slug)
        try:
            spec = sw_site.update_shot(store, shot, nudge_pos=req.nudge_pos, nudge_target=req.nudge_target,
                                       lens_override=req.lens_override or None, tier_override=req.tier_override)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except ValidationError as e:
            raise HTTPException(400, str(e))
        return jobs.submit("shots.camera", slug, lambda: len(sw_site.add_shot(store, spec)["shots"]),
                           lane="cpu", tag={"shot": shot}).public()

    @app.post("/api/projects/{slug}/shots/{shot}/regenerate")
    def shot_regenerate(slug: str, shot: str, req: RegenReq):
        """Re-render the shot's passes if they're missing or stale, then n new frames."""
        store = _store(slug)
        rows = {r["id"]: r for r in sw_shots.status(store)}
        if shot not in rows:
            raise HTTPException(404, f"no shot {shot}")

        def run():
            r = {x["id"]: x for x in sw_shots.status(store)}[shot]
            if not r["rendered"] or r["stale"]:
                sw_shots.render(store, [shot])
            return str(s0_frames.generate(store, shot, n=req.n, refs=req.refs, ref_strength=req.ref_strength)
                       .relative_to(store.root))
        return jobs.submit("frames.generate", slug, run, lane="comfy", tag={"shot": shot}).public()

    # --- city catalog: districts, materials, typologies, overlays, checks -----------------

    @app.get("/api/projects/{slug}/catalog")
    def catalog_get(slug: str):
        store = _store(slug)
        if not (sw_site.site_dir(store) / "layout.json").is_file():
            return {"city": False}
        L = sw_site.load_layout(store)
        stats = read_json(sw_site.site_dir(store) / "city.json", default=None) or {}
        plan_png = sw_site.site_dir(store) / "city_plan.png"
        return {"city": L.city is not None,
                "districts": [d.model_dump(mode="json") for d in L.districts],
                "materials": [m.model_dump(mode="json") for m in L.materials],
                "typologies": [t.model_dump(mode="json") for t in L.city.typologies] if L.city else [],
                "overlays": {k: o.model_dump(mode="json") for k, o in L.city.overlays.items()} if L.city else {},
                "checks": L.city.checks.model_dump(mode="json") if L.city else None,
                "variation": L.city.variation.model_dump(mode="json") if L.city else None,
                "section_edits": L.city.user_fields if L.city else [],
                "report": {"warnings": stats.get("warnings", []), "repetition": stats.get("repetition"),
                           "footprint_share": stats.get("footprint_share"), "buildings": stats.get("buildings")},
                "plan_image": f"{sw_site.SITE}/city_plan.png" if plan_png.is_file() else None,
                "plan_mtime": plan_png.stat().st_mtime if plan_png.is_file() else None}

    @app.put("/api/projects/{slug}/catalog/{section}")
    def catalog_put(slug: str, section: str, body: dict | list = Body(...)):
        store = _store(slug)
        if section not in CATALOG:
            raise HTTPException(404, f"no catalog section {section}")
        L = sw_site.load_layout(store)
        if L.city is None and section not in ("districts", "materials"):
            raise HTTPException(400, "the layout has no city spec")
        adapter, rows = CATALOG[section]
        try:
            new = adapter.validate_python(body)
        except ValidationError as e:
            raise HTTPException(400, str(e))
        if rows:
            ids = [x.id for x in new]
            if len(set(ids)) != len(ids) or not all(ids):
                raise HTTPException(400, f"{section}: every row needs a unique id")
            old = L.districts if section == "districts" else L.materials if section == "materials" \
                else L.city.typologies
            new = cat.mark_user_edits(old, new)
            if section == "districts":
                L.districts = new
            elif section == "materials":
                L.materials = new
            else:
                L.city.typologies = new
        else:
            setattr(L.city, section, new)
            if section not in L.city.user_fields:
                L.city.user_fields = sorted(L.city.user_fields + [section])
        if L.city is not None:
            try:
                cat.check(L)
            except ValueError as e:
                raise HTTPException(400, str(e))
        sw_site.save_layout(store, L)
        return {"saved": section}

    @app.post("/api/projects/{slug}/site/plan")
    def site_plan(slug: str):
        store = _store(slug)
        return jobs.submit("site.plan", slug, lambda: sw_site.plan_city(store)["buildings"], lane="cpu").public()

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: int):
        j = jobs.cancel(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j.public()

    @app.get("/api/jobs/{job_id}")
    def job(job_id: int):
        j = jobs.get(job_id)
        if not j:
            raise HTTPException(404, "no such job")
        return j.public()

    @app.get("/files/{slug}/{path:path}")
    def files(slug: str, path: str, request: Request):
        store = _store(slug)
        target = (store.root / path).resolve()
        if not target.is_relative_to(store.root.resolve()) or not target.is_file():
            raise HTTPException(404, "not found")
        # revalidate every time: a re-rendered pass or preview keeps its name, so a cached
        # copy would go stale. An unchanged file answers 304 (ETag from mtime and size).
        st = target.stat()
        etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
        headers = {"Cache-Control": "no-cache", "ETag": etag}
        if etag in request.headers.get("if-none-match", ""):
            return Response(status_code=304, headers=headers)
        return FileResponse(target, headers=headers)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
