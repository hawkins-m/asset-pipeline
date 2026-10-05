"""Stage 1: scene -> AssetPlan via the project's vision LLM, plus the editor's save path.

The LLM fills a deliberately small schema (SceneAnalysis: names, not ids; Qwen's native
0-1000 `bbox_2d`). to_plan() turns that into the stored AssetPlan: stable unique ids,
relations resolved to ids (unmatched ones dropped), boxes as 0-1 fractions.

Layout under the project:
    scenes/<file>                 scene images imported from outside stage 0
    plan/<scene name>.json        one AssetPlan per scene (+ .prev.json before re-analysis)
    plan/masks/<scene name>/<asset id>.npz   SAM 3.1 mask behind each refined box
where <scene name> is s0_style.scene_name(): batch_001__scene_002.
"""
import io
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from .. import config, review, segment
from ..comfy.client import ComfyClient
from ..llm.base import VisionLLM, structured
from ..llm.registry import llm_for
from ..project import ProjectStore, read_json, write_json
from ..schema import AssetPlan, Category, Dimensions, PlanAsset, Relation
from . import s0_style

PLAN = "plan"
SCENES = s0_style.SCENES
MAX_ASSETS = 20


class PlanExists(ValueError):
    """The plan was edited by hand; re-analysing would replace those edits."""


# --- What the LLM fills ----------------------------------------------------------------

class AnalyzedAsset(BaseModel):
    name: str = Field(description="short lowercase noun phrase, unique in this list, "
                                  "e.g. 'timber market stall'")
    noun: str = Field(description="the plain generic object noun, 1-2 words, no adjectives, "
                                  "e.g. 'market stall', 'barrel', 'pine tree', 'house'")
    category: Category
    description: str = Field(description="the object on its own: shape, parts, materials, "
                                         "colours. No scene, lighting or art style.")
    count: int = Field(ge=1, description="how many copies are visible")
    width_m: float = Field(gt=0, le=1000)
    depth_m: float = Field(gt=0, le=1000)
    height_m: float = Field(gt=0, le=1000)
    kit: str | None = Field(default=None, description="name of the modular set this piece "
                            "belongs to (e.g. 'stone wall kit'), else null")
    placement: str = Field(description="where it is in the scene, a few words")
    bbox_2d: list[int] | None = Field(default=None, description="[x1, y1, x2, y2] of one "
                                      "clearly visible copy, 0-1000 relative coordinates")


class AnalyzedRelation(BaseModel):
    subject: str = Field(description="an asset name from the list")
    relation: str = Field(description="e.g. 'on top of', 'next to', 'attached to', 'inside'")
    object: str = Field(description="an asset name from the list")


class SceneAnalysis(BaseModel):
    summary: str = Field(description="one sentence: what the scene is")
    scale_notes: str = Field(description="which things you used to judge real-world size")
    assets: list[AnalyzedAsset]
    relations: list[AnalyzedRelation] = Field(default_factory=list)


SYSTEM = ("You are a technical art director. You break a concept image into the list of 3D "
          "assets a team must model to rebuild the scene.")

PROMPT = """Break this concept scene into 3D assets.{brief}

Rules:
- One entry per distinct asset. Identical or near-identical copies are ONE entry with a count.
- A whole object is one asset. Its parts are NOT separate assets: doors, windows, roofs,
  shingles, chimneys, beams, lids, seats, legs, poles, brackets, awnings and shelves
  belong to the building, stall, barrel or bench they are part of.
- `kit` is only for pieces made to snap together into bigger structures (wall segments,
  fence sections, paving tiles), and only when two or more listed assets share that
  kit name. Everything else has kit null.
- Skip sky, clouds, light, fog, distant mountains and backdrop, and people or animals.
  Include the ground surface only if it needs a modelled material (cobbles, planks).
- Sizes are real-world metres. Judge them from things of known size (doors ~2 m tall,
  steps ~0.18 m, a person ~1.75 m) and say which you used in `scale_notes`.
- `description` is for drawing the object alone on a white background: shape, parts,
  materials, colours, wear. Don't describe the art style or the lighting.
- `bbox_2d` boxes one clearly visible copy, in 0-1000 relative coordinates.
- `relations` are only between listed assets, using their exact names.
- List at most {max_assets} assets, the most important first. A typical scene has 6-15."""


# --- Paths -----------------------------------------------------------------------------

def plan_name(scene_key: str) -> str:
    return s0_style.scene_name(scene_key)


def plan_file(store: ProjectStore, name: str) -> Path:
    if not re.fullmatch(r"[\w.-]+", name) or name.startswith("."):
        raise ValueError(f"bad plan name {name!r}")
    return store.root / PLAN / f"{name}.json"


def scenes(store: ProjectStore) -> list[str]:
    """Scenes that can be planned: starred stage-0 scenes, then imported ones."""
    imported = sorted(p.relative_to(store.root).as_posix() for p in (store.root / SCENES).glob("*")
                      if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    return review.starred(store, s0_style.EXPLORE + "/") + imported


def import_scene(store: ProjectStore, image: Path, data: bytes | None = None) -> str:
    """Copy an outside image (or uploaded bytes named like `image`) into scenes/."""
    name = re.sub(r"[^\w.-]+", "_", Path(image).name).lstrip(".") or "scene.png"
    if Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError(f"{name}: expected a .png, .jpg or .webp image")
    dst = store.root / SCENES / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    stem, i = dst.stem, 1
    while dst.exists():
        dst = dst.with_name(f"{stem}_{i}{dst.suffix}")
        i += 1
    if data is None:
        shutil.copyfile(image, dst)
    else:
        try:
            Image.open(io.BytesIO(data)).verify()
        except Exception as e:
            raise ValueError(f"{name}: not a readable image ({e})") from e
        dst.write_bytes(data)
    return dst.relative_to(store.root).as_posix()


def load(store: ProjectStore, name: str) -> AssetPlan | None:
    data = read_json(plan_file(store, name))
    return AssetPlan.model_validate(data) if data else None


# --- Analysis --------------------------------------------------------------------------

def analyze(store: ProjectStore, scene: Path | str, force: bool = False,
            llm: VisionLLM | None = None) -> AssetPlan:
    """Draft the plan for one scene. Refuses to replace a hand-edited plan unless force;
    the previous plan is kept as <name>.prev.json either way."""
    key = review.rel(store, scene)
    name = plan_name(key)
    path = plan_file(store, name)
    old = load(store, name)
    if old and old.edited and not force:
        raise PlanExists(f"plan {name} has edits from {old.edited:%Y-%m-%d %H:%M}; "
                         f"re-analysing replaces them (use force)")
    project = store.load()
    llm = llm or llm_for(project)
    brief = f"\nThe artist's brief for this scene: {project.brief.strip()}" if project.brief.strip() else ""
    t = time.time()
    analysis = structured(llm, PROMPT.format(brief=brief, max_assets=MAX_ASSETS),
                          [store.root / key], SceneAnalysis, system=SYSTEM)
    plan = to_plan(analysis, key, llm.name)
    if old:
        shutil.copyfile(path, path.with_suffix(".prev.json"))
    shutil.rmtree(store.root / PLAN / "masks" / name, ignore_errors=True)  # new ids, new boxes
    save(store, name, plan)
    store.log_run({"stage": "plan.analyze", "llm": llm.name, "scene": key, "plan": name,
                   "assets": len(plan.assets), "relations": len(plan.relations),
                   "seconds": round(time.time() - t, 1)})
    return plan


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "asset"


def _bbox(b: list[int] | None) -> list[float] | None:
    if not b or len(b) != 4:
        return None
    x0, y0, x1, y1 = (min(max(v, 0), 1000) / 1000 for v in b)
    return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None


def to_plan(a: SceneAnalysis, scene_key: str, llm_name: str) -> AssetPlan:
    assets = [PlanAsset(name=x.name.strip(), noun=x.noun.strip(), category=x.category, description=x.description.strip(),
                        count=x.count, kit=(x.kit or "").strip() or None,
                        placement=x.placement.strip(), bbox=_bbox(x.bbox_2d),
                        dimensions=Dimensions(width=x.width_m, depth=x.depth_m, height=x.height_m))
              for x in a.assets[:MAX_ASSETS]]
    plan = AssetPlan(scene=scene_key, summary=a.summary.strip(), scale_notes=a.scale_notes.strip(),
                     assets=assets, llm=llm_name)
    assign_ids(plan)
    by_name = {}
    for x in plan.assets:  # the LLM refers to assets by name; match loosely
        by_name.setdefault(x.name.lower(), x.id)
        by_name.setdefault(_slug(x.name), x.id)
    for r in a.relations:
        s = by_name.get(r.subject.strip().lower()) or by_name.get(_slug(r.subject))
        o = by_name.get(r.object.strip().lower()) or by_name.get(_slug(r.object))
        if s and o and s != o:
            plan.relations.append(Relation(subject=s, relation=r.relation.strip(), object=o))
    return plan


def assign_ids(plan: AssetPlan) -> None:
    """Keep existing ids, give new assets one from their name, and make all unique."""
    seen: set[str] = set()
    for x in plan.assets:
        base = x.id or _slug(x.name)
        x.id, i = base, 2
        while x.id in seen:
            x.id, i = f"{base}-{i}", i + 1
        seen.add(x.id)


# --- Box refinement (SAM 3.1) ---------------------------------------------------------

def _box_iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def refine_boxes(store: ProjectStore, name: str, client: ComfyClient | None = None,
                 max_dets: int = 8) -> AssetPlan:
    """Replace each asset's LLM box with a SAM 3.1 box for its noun (plain nouns segment
    far better than descriptive names: "alpine timber stall" returned whole houses,
    "market stall" the stalls). SAM may find several copies: take the one overlapping the
    LLM's box most; without overlap, prefer copies not cut off by the frame, then the
    largest. The LLM box is kept in bbox_llm, so this can be re-run after edits."""
    plan = load(store, name)
    if not plan:
        raise FileNotFoundError(f"no plan {name}")
    c = client or ComfyClient(config.backends()["comfyui"]["url"])
    scene = store.root / plan.scene
    w, h = Image.open(scene).size
    mdir = store.root / PLAN / "masks" / name
    mdir.mkdir(parents=True, exist_ok=True)
    t, changed = time.time(), 0
    for a in plan.assets:
        llm_box = a.bbox_llm if a.bbox_source == "sam" else a.bbox
        dets = segment.detect(c, scene, a.noun or a.name, max_dets=max_dets)
        a.sam_found = len(dets)
        if not dets:  # SAM doesn't know the name: keep (or go back to) the LLM's box
            a.bbox, a.bbox_source, a.bbox_llm, a.mask = llm_box, "llm", None, None
            continue
        boxes = [[d.bbox[0] / w, d.bbox[1] / h, d.bbox[2] / w, d.bbox[3] / h] for d in dets]
        def score(i):
            iou = _box_iou(boxes[i], llm_box) if llm_box else 0.0
            edge = dets[i].touches_border
            return (iou * (0.5 if edge else 1.0), not edge, dets[i].area_frac)
        best = max(range(len(dets)), key=score)
        mask = mdir / f"{a.id}.npz"
        np.savez_compressed(mask, mask=dets[best].mask)
        a.bbox, a.bbox_source, a.bbox_llm = [round(v, 4) for v in boxes[best]], "sam", llm_box
        a.mask = mask.relative_to(store.root).as_posix()
        changed += 1
    for old in mdir.glob("*.npz"):  # masks of deleted or renamed-id assets
        if old.stem not in {a.id for a in plan.assets}:
            old.unlink()
    write_json(plan_file(store, name), plan.model_dump(mode="json"))  # keeps `edited`
    store.log_run({"stage": "plan.refine", "plan": name, "refined": changed,
                   "assets": len(plan.assets), "seconds": round(time.time() - t, 1)})
    return plan


# --- Editor ----------------------------------------------------------------------------

def save(store: ProjectStore, name: str, plan: AssetPlan, edited: bool = False) -> AssetPlan:
    """Write a plan. From the editor (edited=True): ids are kept or assigned, relations to
    deleted assets are dropped, and the plan is marked edited."""
    assign_ids(plan)
    ids = {x.id for x in plan.assets}
    plan.relations = [r for r in plan.relations if r.subject in ids and r.object in ids]
    if edited:
        plan.edited = datetime.now(timezone.utc)
    write_json(plan_file(store, name), plan.model_dump(mode="json"))
    return plan
