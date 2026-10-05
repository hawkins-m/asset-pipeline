"""Local review UI (FastAPI + plain HTML/JS). Run with `ap ui`; binds 127.0.0.1 only.

All state is on disk (project.json, review.json); the server holds only job status.
"""
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, review
from ..jobs import JobQueue
from ..project import ProjectStore, read_json
from ..stages import s0_style

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
            s0_style.explore(store, req.brief, n=req.n, seed=req.seed).relative_to(store.root)))
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
            style_text=req.style_text).relative_to(store.root)))
        return job.public()

    @app.post("/api/projects/{slug}/style/anchor")
    def anchor(slug: str, req: AnchorReq):
        store = _store(slug)
        try:
            a = s0_style.save_anchor(store, strength=req.strength, style_text=req.style_text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return a.model_dump(mode="json")

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
