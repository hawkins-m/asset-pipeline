"""Stage 0: explore scene concepts, then derive the project's style anchor from them.

1. explore(): brief -> a batch of scene/environment concepts. Starred scenes feed stage 1.
2. derive():  for a starred scene, SAM 3 cuts out named objects (real pixels in the
              scene's style); each cutout is regenerated as a whole object on pure white,
              conditioned on the cutout itself (an object anchor, which leaks far less
              than a scene anchor; see CLAUDE.md "Style anchor findings").
3. save_anchor(): starred derived objects + style text -> project.anchor.

Layout under the project:
    style/explore/batch_NNN/scene_###.png + meta.json
    style/derive/<scene stem>/cut_<noun>_<i>.png, obj_<noun>_<i>.png + meta.json
    style/anchor/*.png  (copies of the starred derived objects)
"""
import re
import shutil
import time
from pathlib import Path

from .. import config, review, segment
from ..comfy.client import ComfyClient
from ..imagegen.base import GenRequest
from ..imagegen.registry import backend_for
from ..project import ProjectStore, read_json, write_json
from ..schema import StyleAnchor

EXPLORE = "style/explore"
DERIVE = "style/derive"
ANCHOR = "style/anchor"

SCENE_SUFFIX = "environment concept art, wide establishing view, cohesive art direction"
OBJECT_PROMPT = ("a single {noun}, complete and fully visible, centered, isolated on a plain "
                 "pure white background, no ground, no shadow, game asset concept")
# Measured safe for object anchors (subject kept at 0.08 and 0.15; scene anchors flip at 0.12).
DERIVE_STRENGTH = 0.15
# Saved-anchor default. Anchor-set dependent: lanterns were clean at 0.15, but a
# stall-heavy set pasted awnings/produce into a cart at 0.08-0.12 and was clean at 0.05.
ANCHOR_STRENGTH = 0.06


def _next_batch(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in root.iterdir() if (m := re.fullmatch(r"batch_(\d+)", p.name))]
    return root / f"batch_{max(nums, default=0) + 1:03d}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32] or "object"


def explore(store: ProjectStore, brief: str | None = None, n: int = 8, seed: int | None = None,
            width: int = 1344, height: int = 768, batch_size: int = 4) -> Path:
    """Generate n scene concepts from the brief into a new batch dir; returns the dir."""
    project = store.load()
    brief = (brief or project.brief).strip()
    if not brief:
        raise ValueError("no brief: pass one or set it on the project")
    if not project.brief:
        project.brief = brief
        store.save(project)
    backend = backend_for("style", project)
    out = _next_batch(store.root / EXPLORE)
    prompt = f"{brief.rstrip(' ,.')}, {SCENE_SUFFIX}"
    t, results, i = time.time(), [], 0
    while i < n:  # batches keep VRAM bounded; each batch gets its own seed
        k = min(batch_size, n - i)
        s = None if seed is None else seed + i
        req = GenRequest(prompt=prompt, width=width, height=height, n=k, seed=s)
        for r in backend.generate(req, out, prefix=f"tmp{i:03d}"):
            final = out / f"scene_{len(results):03d}.png"
            r.path.rename(final)
            results.append({"file": final.name, "seed": r.seed, "batch_index": r.meta.get("batch_index")})
        i += k
    meta = {"brief": brief, "prompt": prompt, "backend": backend.name, "size": [width, height],
            "images": results}
    write_json(out / "meta.json", meta)
    store.log_run({"stage": "style.explore", "backend": backend.name, "dir": str(out),
                   "n": len(results), "seconds": round(time.time() - t, 1)})
    return out


def starred_scenes(store: ProjectStore) -> list[Path]:
    """Starred explore images: the scenes stage 1 analyses."""
    return [store.root / k for k in review.starred(store, EXPLORE + "/")]


def derive(store: ProjectStore, scene: Path | str, nouns: list[str], per_noun: int = 2,
           regenerate: bool = True, style_text: str = "", seed: int = 1,
           client: ComfyClient | None = None) -> Path:
    """Cut named objects out of a scene and regenerate them as clean objects on white."""
    key = review.rel(store, scene)
    scene_path = store.root / key
    project = store.load()
    style_text = style_text or (project.anchor.style_text if project.anchor else "")
    c = client or ComfyClient(config.backends()["comfyui"]["url"])
    out = store.root / DERIVE / Path(key).stem
    out.mkdir(parents=True, exist_ok=True)
    backend = backend_for("style", project) if regenerate else None
    t, items, taken = time.time(), [], []
    for noun in [n.strip() for n in nouns if n.strip()]:
        # SAM 3 can return the same object for different nouns (a barrel as "flower pot");
        # skip masks that mostly overlap one already taken.
        dets = [d for d in segment.detect(c, scene_path, noun) if not d.touches_border
                and all(segment.iou(d.mask, m) < 0.8 for m in taken)]
        for i, det in enumerate(dets[:per_noun]):
            taken.append(det.mask)
            name = f"{_slug(noun)}_{i}"
            cut = out / f"cut_{name}.png"
            segment.cutout(scene_path, det)[1].save(cut)
            item = {"noun": noun, "cut": cut.name, "area_frac": round(det.area_frac, 4)}
            if backend:
                req = GenRequest(prompt=OBJECT_PROMPT.format(noun=noun), seed=seed + len(items),
                                 anchor=StyleAnchor(images=[cut], strength=DERIVE_STRENGTH,
                                                    style_text=style_text))
                res = backend.generate(req, out, prefix=f"tmp_{name}")[0]
                obj = out / f"obj_{name}.png"
                res.path.rename(obj)
                item["obj"] = obj.name
            items.append(item)
    meta = read_json(out / "meta.json", default={"scene": key, "items": []})
    meta["items"] = [m for m in meta["items"] if m["cut"] not in {i["cut"] for i in items}] + items
    meta["style_text"] = style_text
    write_json(out / "meta.json", meta)
    store.log_run({"stage": "style.derive", "scene": key, "nouns": nouns, "n": len(items),
                   "seconds": round(time.time() - t, 1)})
    return out


def _derive_style_text(store: ProjectStore, keys: list[str]) -> str:
    for k in keys:
        meta = read_json(store.root / Path(k).parent / "meta.json", default={}) or {}
        if meta.get("style_text"):
            return meta["style_text"]
    return ""


def save_anchor(store: ProjectStore, strength: float = ANCHOR_STRENGTH, style_text: str | None = None) -> StyleAnchor:
    """Copy starred derived objects into style/anchor/ and set the project's anchor."""
    keys = review.starred(store, DERIVE + "/")
    if not keys:
        raise ValueError("star some derived objects (style/derive/...) first")
    project = store.load()
    adir = store.root / ANCHOR
    if adir.exists():
        shutil.rmtree(adir)
    adir.mkdir(parents=True)
    images = []
    for k in keys:
        dst = adir / k.removeprefix(DERIVE + "/").replace("/", "__")
        shutil.copyfile(store.root / k, dst)
        images.append(dst)
    old = project.anchor
    if style_text is None:  # keep the current text, else the one used when deriving
        style_text = old.style_text if old and old.style_text else _derive_style_text(store, keys)
    project.anchor = StyleAnchor(images=images, strength=strength, style_text=style_text,
                                 lora=old.lora if old else None)
    store.save(project)
    write_json(adir / "anchor.json", project.anchor.model_dump(mode="json") | {"sources": keys})
    store.log_run({"stage": "style.anchor", "images": keys, "strength": strength})
    return project.anchor
