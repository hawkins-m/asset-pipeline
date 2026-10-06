"""Local review UI (FastAPI + plain HTML/JS). Run with `ap ui`; binds 127.0.0.1 only.

All state is on disk (project.json, review.json); the server holds only job status.
"""
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, review, vlm
from ..comfy.client import ComfyError
from ..jobs import JobQueue
from ..project import ProjectStore, read_json
from ..schema import AssetPlan
from ..stages import s0_style, s1_plan, s2_refs, s3_views, s5_3d

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
                "explore": batches, "derived": derived,
                "jobs": [j.public() for j in jobs.list(slug)]}

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
                    "results": s5_3d.results(store, name, a.id)})
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
    def files(slug: str, path: str):
        store = _store(slug)
        target = (store.root / path).resolve()
        if not target.is_relative_to(store.root.resolve()) or not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(target)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
