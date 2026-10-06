"""Stage 0: explore scene concepts, then derive the project's style anchor from them.

1. explore(): brief -> a batch of scene/environment concepts. Starred scenes feed stage 1.
2. derive():  for a starred scene, SAM 3 cuts out named objects (real pixels in the
              scene's style); each cutout is regenerated as a whole object on pure white,
              conditioned on the cutout itself (an object anchor, which leaks far less
              than a scene anchor; see CLAUDE.md "Style anchor findings").
3. save_anchor(): starred derived objects + style text -> project.anchor.

Layout under the project:
    style/explore/batch_NNN/scene_###.png + meta.json
    style/derive/<scene name>/cut_<noun>_<i>.png, obj_<noun>_<i>.png + meta.json
        (scene name: see scene_name(); dirs from before it may be named by the bare stem)
    style/anchor/*.png  (copies of the starred derived objects)
    style/moodboard/<group>/*  imported reference images (e.g. a PureRef export, one folder
        per group) + meta.json; derive() works on them like on scenes, and
        draft_style_text() has the vision LLM describe their shared style
"""
import random
import re
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from .. import config, review, segment
from ..comfy.client import ComfyClient
from ..imagegen.base import GenRequest
from ..imagegen.registry import backend_for
from ..project import ProjectStore, read_json, write_json
from ..schema import StyleAnchor

EXPLORE = "style/explore"
DERIVE = "style/derive"
ANCHOR = "style/anchor"
SCENES = "scenes"                 # scene images imported from outside stage 0
MOODBOARD = "style/moodboard"
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}

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


def scene_name(key: str) -> str:
    """File-safe name for a scene, unique within the project: its path below
    style/explore/ or scenes/ with "/" -> "__", no suffix (batch_001__scene_002). The
    bare stem isn't unique: every batch has a scene_000."""
    p = Path(key)
    for prefix in (EXPLORE, SCENES):
        if p.is_relative_to(prefix):
            p = p.relative_to(prefix)
            break
    return "__".join(p.with_suffix("").parts)


def derive_dir(store: ProjectStore, key: str) -> Path:
    legacy = store.root / DERIVE / Path(key).stem  # pre-scene_name() layout, still in use
    if (read_json(legacy / "meta.json", default=None) or {}).get("scene") == key:
        return legacy
    return store.root / DERIVE / scene_name(key)


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
    try:
        while i < n:  # batches keep VRAM bounded; each batch gets its own seed
            k = min(batch_size, n - i)
            s = None if seed is None else seed + i
            req = GenRequest(prompt=prompt, width=width, height=height, n=k, seed=s)
            for r in backend.generate(req, out, prefix=f"tmp{i:03d}"):
                final = out / f"scene_{len(results):03d}.png"
                r.path.rename(final)
                results.append({"file": final.name, "seed": r.seed, "batch_index": r.meta.get("batch_index")})
            i += k
    finally:  # a canceled run keeps the batches that finished
        meta = {"brief": brief, "prompt": prompt, "backend": backend.name, "size": [width, height],
                "images": results, "complete": len(results) == n}
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
    out = derive_dir(store, key)
    out.mkdir(parents=True, exist_ok=True)
    backend = backend_for("style", project) if regenerate else None
    meta = read_json(out / "meta.json", default=None) or {"scene": key, "items": []}
    t, items = time.time(), []
    stars = review.load(store)["stars"]
    try:
        for noun in [n.strip() for n in nouns if n.strip()]:
            # Re-deriving a noun replaces its unstarred items; starred ones are kept (and still
            # block duplicates). New items take free filenames, so stars never move.
            slug = _slug(noun)
            old = [m for m in meta["items"] if _slug(m["noun"]) == slug
                   and not _is_starred(store, out, m, stars)]
            _remove_items(store, out, old)
            meta["items"] = [m for m in meta["items"] if m not in old]
            # The same object must not come out twice: not for two nouns (SAM 3 returned a
            # barrel for "flower pot"), not as a part of an object already taken (a barrel's
            # lid), and not across separate derive runs on this scene (masks are stored).
            taken = [_load_mask(out, m) for m in meta["items"] + items]
            taken = [m for m in taken if m is not None]
            kept = 0
            for det in segment.detect(c, scene_path, noun):
                if kept >= per_noun:
                    break
                if det.touches_border or any(segment.overlap(det.mask, m) > 0.6 for m in taken):
                    continue
                taken.append(det.mask)
                name = _free_name(out, slug)
                kept += 1
                cut = out / f"cut_{name}.png"
                segment.cutout(scene_path, det)[1].save(cut)
                np.savez_compressed(out / f"mask_{name}.npz", mask=det.mask)
                item = {"noun": noun, "cut": cut.name, "mask": f"mask_{name}.npz",
                        "area_frac": round(det.area_frac, 4)}
                item_seed = seed + len(items)
                items.append(item)  # before the redraw, so a canceled redraw keeps the cutout
                if backend:
                    req = GenRequest(prompt=OBJECT_PROMPT.format(noun=noun), seed=item_seed,
                                     anchor=StyleAnchor(images=[cut], strength=DERIVE_STRENGTH,
                                                        style_text=style_text))
                    res = backend.generate(req, out, prefix=f"tmp_{name}")[0]
                    obj = out / f"obj_{name}.png"
                    res.path.rename(obj)
                    item["obj"] = obj.name
    finally:  # a canceled run keeps the items already cut (their files exist)
        meta["items"] += items
        meta["style_text"] = style_text
        write_json(out / "meta.json", meta)
        store.log_run({"stage": "style.derive", "scene": key, "nouns": nouns, "n": len(items),
                       "seconds": round(time.time() - t, 1)})
    return out


def _is_starred(store: ProjectStore, out: Path, item: dict, stars: dict) -> bool:
    return any(stars.get((out / item[k]).relative_to(store.root).as_posix())
               for k in ("cut", "obj") if item.get(k))


def _free_name(out: Path, slug: str) -> str:
    i = 0
    while (out / f"cut_{slug}_{i}.png").exists():
        i += 1
    return f"{slug}_{i}"


def _load_mask(out: Path, item: dict) -> np.ndarray | None:
    path = out / item.get("mask", "")
    return np.load(path)["mask"] if item.get("mask") and path.is_file() else None


def _remove_items(store: ProjectStore, out: Path, items: list[dict]) -> None:
    files = [out / item[k] for item in items for k in ("cut", "obj", "mask") if item.get(k)]
    review.forget(store, [f.relative_to(store.root).as_posix() for f in files])
    for f in files:
        f.unlink(missing_ok=True)


def _derive_style_text(store: ProjectStore, keys: list[str]) -> str:
    for k in keys:
        meta = read_json(store.root / Path(k).parent / "meta.json", default={}) or {}
        if meta.get("style_text"):
            return meta["style_text"]
    return ""


# --- moodboard -----------------------------------------------------------------------------

def import_moodboard(store: ProjectStore, group: str, sources: list[Path]) -> list[str]:
    """Copy images (files, or every image in the given folders) into style/moodboard/<group>/.
    PureRef's .pur format is proprietary: export the board's images to a folder first."""
    g = _slug(group)
    files = []
    for src in map(Path, sources):
        if src.is_dir():
            files += sorted(p for p in src.rglob("*") if p.suffix.lower() in IMAGE_EXT)
        elif src.suffix.lower() == ".pur":
            raise ValueError(f"{src}: PureRef files can't be read; export the images to a folder")
        elif src.suffix.lower() in IMAGE_EXT:
            files.append(src)
        else:
            raise ValueError(f"{src}: expected an image or a folder of images")
    out = store.root / MOODBOARD / g
    out.mkdir(parents=True, exist_ok=True)
    meta = read_json(out / "meta.json", default=None) or {"group": group, "images": []}
    keys = []
    for f in files:
        try:
            Image.open(f).verify()
        except Exception as e:
            raise ValueError(f"{f}: not a readable image ({e})") from e
        name = re.sub(r"[^\w.-]+", "_", f.name).lstrip(".") or "image.png"
        dst, i = out / name, 1
        while dst.exists():
            dst = out / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        shutil.copyfile(f, dst)
        meta["images"].append({"file": dst.name, "source": f.name})
        keys.append(dst.relative_to(store.root).as_posix())
    write_json(out / "meta.json", meta)
    store.log_run({"stage": "style.moodboard", "group": g, "n": len(keys)})
    return keys


def moodboard(store: ProjectStore) -> dict[str, list[str]]:
    """Group -> project-relative image keys."""
    root = store.root / MOODBOARD
    return {d.name: sorted(p.relative_to(store.root).as_posix() for p in d.iterdir()
                           if p.suffix.lower() in IMAGE_EXT)
            for d in sorted(root.iterdir()) if d.is_dir()} if root.is_dir() else {}


def contact_sheet(paths: list[Path], out: Path, tile: int = 512, cols: int = 3) -> Path:
    """Images letterboxed into one grid image (one image costs the vision LLM far fewer
    tokens than nine: Qwen3-VL spends ~2.4k tokens on a 1568 px image)."""
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("RGB", (tile * cols, tile * rows), "white")
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        im.thumbnail((tile, tile), Image.LANCZOS)
        sheet.paste(im, ((i % cols) * tile + (tile - im.width) // 2, (i // cols) * tile + (tile - im.height) // 2))
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    return out


class StyleDraft(BaseModel):
    style_text: str = Field(description="one line, comma-separated: architecture and forms, materials, "
                                        "palette, light, vegetation, rendering style; at most 60 words")
    palette: str = Field(description="the dominant colours, a few words")
    avoid: str = Field(description="what these references avoid, a few words")


STYLE_PROMPT = """These are moodboard references for one project{brief}.
Describe the style they share, as a prompt suffix for an image model that will draw new
places and objects in this style. Describe the look (architecture and forms, materials,
palette, light, vegetation, rendering style), not the subjects of these particular
pictures: no named buildings, people or places."""


def draft_style_text(store: ProjectStore, groups: list[str] | None = None, max_images: int = 9,
                     seed: int = 0, llm=None) -> StyleDraft:
    """Ask the project's vision LLM for a style text from (a sample of) the moodboard.
    Doesn't save it: `ap moodboard style --save` or the anchor form does."""
    from ..llm.base import structured
    from ..llm.registry import llm_for
    board = moodboard(store)
    keys = [k for g, ks in board.items() if not groups or g in groups for k in ks]
    if not keys:
        raise ValueError("no moodboard images (ap moodboard import first)")
    rng = random.Random(seed)
    if len(keys) > max_images:  # round-robin over the groups, random order within each
        pools = [rng.sample(ks, len(ks)) for g, ks in board.items() if not groups or g in groups]
        pick = []
        while len(pick) < max_images:
            for pool in pools:
                if pool and len(pick) < max_images:
                    pick.append(pool.pop())
        keys = pick
    sheet = contact_sheet([store.root / k for k in keys], store.root / MOODBOARD / "_sheet.png")
    project = store.load()
    brief = f" (brief: {project.brief.strip()})" if project.brief.strip() else ""
    llm = llm or llm_for(project)
    draft = structured(llm, STYLE_PROMPT.format(brief=brief), [sheet], StyleDraft,
                       system="You are an art director writing style guides for image generation.")
    store.log_run({"stage": "style.draft_text", "images": keys, "llm": llm.name})
    return draft


def set_style_text(store: ProjectStore, text: str) -> StyleAnchor:
    """Set the project's style text, keeping its anchor images (or none yet)."""
    project = store.load()
    project.anchor = (project.anchor or StyleAnchor(strength=ANCHOR_STRENGTH)).model_copy(update={"style_text": text})
    store.save(project)
    return project.anchor


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
