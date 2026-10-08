"""World mode, stage 0: concept frames per shot camera, generated onto the greybox.

Each frame follows its shot's control passes (depth + canny rendered from the greybox,
sw_shots), so every shot shows the same layout. The prompt says what the camera sees,
taken from the shot's object-id pass rather than written per shot:
  - tier and the shot's own notes;
  - the visible slot types, by screen coverage;
  - the visible materials (layout `materials`, by coverage), or, for a layout without
    materials, the notes of the visible districts. A material's reference image goes in as
    Redux masked to its slots (from the id pass), so it only restyles those surfaces.
The project's style anchor (Redux images + style text) is applied as everywhere else.
Consistency across shots: approved frames of other shots can be added as extra Redux
references (`refs`); depth locks the structure, so their content leakage mostly carries
materials and palette.

Tight shots are for mood and material only: their frames never seed assets
(`asset_sources`), though they can still serve as Redux references.

Layout under the project:
    frames/<shot>/batch_NNN/frame_###.png + meta.json   (stars in review.json = approved)
    frames/<shot>/batch_NNN/mask_<material>.png         (regional ref masks, when used)
"""
import math
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .. import review
from ..imagegen.base import ControlImage, GenRequest, RegionalRef
from ..imagegen.registry import backend_for
from ..project import ProjectStore, read_json, write_json
from ..schema import FrameSettings, Material, StyleAnchor
from . import sw_shots, sw_site

FRAMES = "frames"
MIN_COVERAGE = 0.01          # slot types under this share of the frame aren't named
MAX_MATERIALS = 2            # materials named in a prompt, largest coverage first
MATERIAL_COVERAGE = 0.03     # a material must cover this share of the frame to be named
MAX_TYPES = 3                # building types named in a prompt, largest coverage first
GROUND_TYPES = {"terrain", "sea", "street", "avenue", "radial", "plaza", "ring"}  # implied by materials
MOOD_TIERS = ("tight",)      # frames of these shots set mood/material, never assets
TIER = {"wide": "wide establishing view", "medium": "eye-level view", "tight": "close-up detail view"}
# What each greybox slot type is, for the prompt (plural forms: types usually repeat).
TYPE_WORDS = {"temple": "classical temples with colonnades", "rotunda": "a domed rotunda ringed by columns",
              "stoa": "a curved colonnaded stoa", "block": "monumental civic buildings with arcades",
              "villa": "courtyard villas", "garden": "lush gardens", "canal": "a canal",
              "pool": "reflecting pools", "avenue": "broad paved avenues", "radial": "a broad paved avenue",
              "plaza": "a circular paved plaza", "terrace": "terraces", "sea": "the sea",
              "terrain": "the landscape",
              # city mode
              "housing": "dense mid-rise courtyard housing blocks", "houses": "low-rise houses with gardens",
              "street": "narrow streets", "park": "parks, gardens and tree-lined green corridors",
              "market": "a market square with stalls", "quay": "harbour piers"}
SUFFIX = "cinematic architectural concept art"


def frames_dir(store: ProjectStore, shot: str) -> Path:
    return store.root / FRAMES / shot


def _next_batch(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    nums = [int(m.group(1)) for p in root.iterdir() if (m := re.fullmatch(r"batch_(\d+)", p.name))]
    return root / f"batch_{max(nums, default=0) + 1:03d}"


def _shot_meta(store: ProjectStore, shot: str) -> dict:
    meta = read_json(sw_shots.shot_dir(store, shot) / "meta.json", default=None)
    if not meta:
        raise FileNotFoundError(f"shot {shot} has no passes yet (ap shots render)")
    return meta


def visible(store: ProjectStore, shot: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """(slot type, coverage) and (district, coverage) seen by the shot, largest first. A
    slot that belongs to a catalog typology counts as that typology (a landmark ensemble's
    colonnades and lagoon count as the landmark)."""
    ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json", default=None)
    if not ids:
        raise FileNotFoundError(f"shot {shot} has no id pass (ap shots render)")
    slots = {s["id"]: s for s in sw_site.load_greybox(store)["slots"]}
    types: dict[str, float] = {}
    districts: dict[str, float] = {}
    for sid, v in ids["slots"].items():
        s = slots.get(sid)
        if not s:
            continue
        key = s.get("typology") or s["type"]
        types[key] = types.get(key, 0) + v["frac"]
        if s["district"]:
            districts[s["district"]] = districts.get(s["district"], 0) + v["frac"]
    order = lambda d: sorted(d.items(), key=lambda kv: -kv[1])  # noqa: E731
    return order(types), order(districts)


def _matches(m: Material, slot: dict) -> bool:
    found = sw_site.material_of(slot, [m])
    return found is not None


def visible_materials(store: ProjectStore, shot: str) -> list[tuple[Material, float]]:
    """(material, coverage) seen by the shot, largest first; a slot counts for the first
    material that matches it."""
    ids = read_json(sw_shots.shot_dir(store, shot) / "ids.json", default=None)
    if not ids:
        raise FileNotFoundError(f"shot {shot} has no id pass (ap shots render)")
    mats = sw_site.load_layout(store).materials
    slots = {s["id"]: s for s in sw_site.load_greybox(store)["slots"]}
    cover: dict[str, float] = {}
    for sid, v in ids["slots"].items():
        m = next((m for m in mats if sid in slots and _matches(m, slots[sid])), None)
        if m:
            cover[m.id] = cover.get(m.id, 0) + v["frac"]
    by_id = {m.id: m for m in mats}
    return [(by_id[k], f) for k, f in sorted(cover.items(), key=lambda kv: -kv[1])]


def material_mask(store: ProjectStore, shot: str, material: Material) -> np.ndarray:
    """Boolean mask of the pixels whose slot this material covers (exact: the id pass)."""
    d = sw_shots.shot_dir(store, shot)
    ids = read_json(d / "ids.json")
    mats = sw_site.load_layout(store).materials
    slots = {s["id"]: s for s in sw_site.load_greybox(store)["slots"]}
    keys = []
    for sid, v in ids["slots"].items():
        first = next((m for m in mats if sid in slots and _matches(m, slots[sid])), None)
        if first is not None and first.id == material.id:
            c = v["color"]
            keys.append((c[0] << 16) | (c[1] << 8) | c[2])
    rgb = np.array(Image.open(d / "ids.png").convert("RGB"))
    return np.isin(sw_shots._key(rgb), np.array(keys, np.int64))


def typology_mask(store: ProjectStore, shot: str, typology: str) -> np.ndarray:
    """Boolean mask of the pixels of the slots that belong to a typology (id pass)."""
    d = sw_shots.shot_dir(store, shot)
    ids = read_json(d / "ids.json")
    slots = {s["id"]: s for s in sw_site.load_greybox(store)["slots"]}
    keys = [(v["color"][0] << 16) | (v["color"][1] << 8) | v["color"][2] for sid, v in ids["slots"].items()
            if sid in slots and (slots[sid].get("typology") or slots[sid]["type"]) == typology]
    rgb = np.array(Image.open(d / "ids.png").convert("RGB"))
    return np.isin(sw_shots._key(rgb), np.array(keys, np.int64))


def landmark_refs(store: ProjectStore, shot: str, out: Path) -> list[RegionalRef]:
    """Masked Redux refs for the visible catalog typologies that have a reference image
    (landmarks): the reference shapes that building only."""
    layout = sw_site.load_layout(store)
    cat = {t.id: t for t in (layout.city.typologies if layout.city else []) if t.ref and t.ref_strength > 0}
    types, _ = visible(store, shot)
    refs = []
    for k, f in types:
        t = cat.get(k)
        if t is None or f < MIN_COVERAGE:
            continue
        mask = out / f"mask_typology_{k}.png"
        Image.fromarray(typology_mask(store, shot, k).astype(np.uint8) * 255).save(mask)
        refs.append(RegionalRef(image=store.root / review.rel(store, t.ref), mask=mask, strength=t.ref_strength))
    return refs


def regional_refs(store: ProjectStore, shot: str, out: Path) -> list[RegionalRef]:
    """Masked Redux refs for the visible materials that have a reference image; the masks
    are written into `out`."""
    refs = []
    for m, f in visible_materials(store, shot):
        if not m.ref or f < MIN_COVERAGE or m.ref_strength <= 0:
            continue
        mask = out / f"mask_{m.id}.png"
        Image.fromarray(material_mask(store, shot, m).astype(np.uint8) * 255).save(mask)
        refs.append(RegionalRef(image=store.root / review.rel(store, m.ref), mask=mask,
                                strength=m.ref_strength))
    return refs


def auto_prompt(store: ProjectStore, shot: str) -> str:
    """The prompt built from what the camera sees, kept short: tier and the shot's notes,
    the largest few building types (catalog phrases), the largest few materials, and one
    district identity line (the shot's own district, else the largest on screen)."""
    meta = _shot_meta(store, shot)
    s = meta["shot"]
    types, districts = visible(store, shot)
    layout = sw_site.load_layout(store)
    notes = {d.id: d.notes for d in layout.districts if d.notes}
    cat = {t.id: t for t in (layout.city.typologies if layout.city else [])}
    parts = [s["notes"]] if s.get("notes") else [TIER.get(s["tier"], "view")]
    # landmarks (catalog types with a reference image) on screen are always named, first
    shown = [t for t, f in types if f >= MIN_COVERAGE and t not in GROUND_TYPES]
    marks = [t for t in shown if t in cat and cat[t].ref]
    shown = (marks + [t for t in shown if t not in marks])[:max(MAX_TYPES, len(marks))]
    seen = [(cat[t].prompt if t in cat and cat[t].prompt else TYPE_WORDS.get(t, t.replace("_", " "))) for t in shown]
    if seen:
        parts.append("showing " + ", ".join(dict.fromkeys(seen)))
    by_id = {m.id: m for m in layout.materials}
    mark_mats = [by_id[cat[t].material] for t in marks if cat[t].material in by_id]
    mats = [m.words for m in mark_mats]
    mats += [m.words for m, f in visible_materials(store, shot)
             if f >= MATERIAL_COVERAGE and m not in mark_mats][:max(0, MAX_MATERIALS - len(mats))]
    ds = [d for d, f in districts if f >= MIN_COVERAGE]
    if s.get("district") in notes:
        ds = [s["district"]] + [d for d in ds if d != s["district"]]
    ds = [d for d in ds if d in notes]
    if not layout.materials:     # no materials: the districts' notes carry them (at most two)
        mats = [notes[d] for d in ds][:2]
    elif ds:
        parts.append(notes[ds[0]])
    if mats:
        parts.append("; ".join(mats))
    parts.append(SUFFIX)
    return ", ".join(p.strip().rstrip(".") for p in parts if p.strip())


def _shot_spec(store: ProjectStore, shot: str):
    return next((s for s in sw_site.load_layout(store).shots if s.id == shot), None)


def prompt_parts(store: ProjectStore, shot: str) -> dict:
    """The auto prompt, the user's append / override, and the prompt actually used."""
    auto = auto_prompt(store, shot)
    spec = _shot_spec(store, shot)
    append = spec.prompt_append.strip() if spec else ""
    override = (spec.prompt_override or "").strip() if spec else ""
    if override:
        used, source = override, "override"
    elif append:
        used, source = f"{auto}, {append}", "append"
    else:
        used, source = auto, "auto"
    return {"auto": auto, "append": append, "override": override or None, "prompt": used, "source": source}


def prompt_for(store: ProjectStore, shot: str) -> str:
    return prompt_parts(store, shot)["prompt"]


def settings(store: ProjectStore) -> FrameSettings:
    p = store.load()
    return p.frames or FrameSettings()


def controls(store: ProjectStore, shot: str, fs: FrameSettings) -> list[ControlImage]:
    d = sw_shots.shot_dir(store, shot)
    out = [ControlImage(kind="depth", image=d / "depth.png", strength=fs.depth_strength,
                        start=0.0, end=fs.depth_end)]
    if fs.model == "union" and fs.canny_strength > 0:
        out.append(ControlImage(kind="canny", image=d / "canny.png", strength=fs.canny_strength,
                                start=0.0, end=fs.canny_end))
    return out


def generate(store: ProjectStore, shot: str, n: int = 4, seed: int | None = None,
             fs: FrameSettings | None = None, refs: list[str] | None = None, ref_strength: float = 0.08,
             prompt: str | None = None, out: Path | None = None, material_refs: bool = True) -> Path:
    """n concept frames for one shot into a new batch dir; returns the dir.

    refs: project-relative images (e.g. an approved wide frame) added to the Redux anchor
    with `ref_strength` more total strength. material_refs: apply the visible materials'
    reference images, masked to their slots (False: wording only)."""
    meta = _shot_meta(store, shot)
    if sw_shots.is_stale(meta, sw_site.load_greybox(store), shot):
        raise ValueError(f"shot {shot}'s passes are stale (the greybox or its camera changed): ap shots render first")
    project = store.load()
    fs = fs or settings(store)
    source = "given" if prompt else prompt_parts(store, shot)["source"]
    prompt = prompt or prompt_for(store, shot)
    anchor = project.anchor
    ref_paths = [store.root / review.rel(store, r) for r in refs or []]
    if ref_paths:
        base = anchor or StyleAnchor(strength=0.0)
        anchor = StyleAnchor(images=list(base.images) + ref_paths, strength=base.strength + ref_strength,
                             style_text=base.style_text, lora=base.lora)
    w, h = meta["shot"]["resolution"]
    backend = backend_for("frames", project)
    out = out or _next_batch(frames_dir(store, shot))
    out.mkdir(parents=True, exist_ok=True)
    regional = (regional_refs(store, shot, out) + landmark_refs(store, shot, out)
                if material_refs and fs.model == "union" else [])
    t, results = time.time(), []
    try:
        for i in range(n):  # one image per request: each gets its own seed, VRAM stays flat
            s = None if seed is None else seed + i
            req = GenRequest(prompt=prompt, width=w, height=h, n=1, seed=s, steps=fs.steps, anchor=anchor,
                             control=controls(store, shot, fs), control_model=fs.model, regional=regional)
            r = backend.generate(req, out, prefix=f"tmp{i:03d}")[0]
            final = out / f"frame_{len(results):03d}.png"
            r.path.rename(final)
            results.append({"file": final.name, "seed": r.seed,
                            "edge_match": edge_match(final, sw_shots.shot_dir(store, shot) / "canny.png")})
    finally:  # a canceled run keeps the frames already made
        write_json(out / "meta.json", {
            "shot": shot, "prompt": prompt, "prompt_source": source, "settings": fs.model_dump(mode="json"),
            "anchor": anchor.model_dump(mode="json") if anchor else None, "refs": refs or [],
            "material_refs": [{"image": review.rel(store, r.image), "mask": r.mask.name, "strength": r.strength}
                              for r in regional],
            "greybox_sha256": meta["greybox_sha256"], "geometry_sha256": meta.get("geometry_sha256"),
            "size": [w, h], "backend": backend.name,
            "frames": results, "complete": len(results) == n})
        store.log_run({"stage": "frames.generate", "shot": shot, "dir": str(out), "n": len(results),
                       "model": fs.model, "seconds": round(time.time() - t, 1)})
    return out


def batches(store: ProjectStore, shot: str) -> list[dict]:
    out = []
    for b in sorted(frames_dir(store, shot).glob("batch_*")):
        meta = read_json(b / "meta.json", default={}) or {}
        out.append({"dir": b.relative_to(store.root).as_posix(), "prompt": meta.get("prompt", ""),
                    "settings": meta.get("settings"), "refs": meta.get("refs", []),
                    "frames": [{"key": (b / f["file"]).relative_to(store.root).as_posix(), **f}
                               for f in meta.get("frames", []) if (b / f["file"]).is_file()]})
    return out


def approved(store: ProjectStore, shot: str | None = None) -> list[str]:
    """Starred frames (of one shot, or all)."""
    return review.starred(store, f"{FRAMES}/{shot}/" if shot else f"{FRAMES}/")


def mood_shots(store: ProjectStore) -> set[str]:
    return {s["id"] for s in sw_site.load_greybox(store)["shots"] if s["tier"] in MOOD_TIERS}


def asset_sources(store: ProjectStore) -> list[str]:
    """Starred frames the asset library may derive assets from: frames of the current
    greybox's shots, except mood shots (archived frames of an old layout never count)."""
    shots = {s["id"] for s in sw_site.load_greybox(store)["shots"]}
    mood = mood_shots(store)
    return [k for k in approved(store) if k.split("/")[1] in shots - mood]


# --- structure check -----------------------------------------------------------------------

def _edges(gray: np.ndarray, frac: float = 0.12) -> np.ndarray:
    """Strongest `frac` of Sobel gradient magnitudes (a rough, threshold-free canny)."""
    g = gray.astype(np.float32)
    gx = np.zeros_like(g)
    gy = np.zeros_like(g)
    gx[1:-1, 1:-1] = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2])
    gy[1:-1, 1:-1] = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:])
    mag = np.hypot(gx, gy)
    # at most `frac` of the pixels; never flat areas (a plain frame has no edges at all)
    return mag > max(float(np.quantile(mag, 1 - frac)), 0.05 * float(mag.max()), 1e-6)


def _dilate(m: np.ndarray, r: int) -> np.ndarray:
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            if dy * dy + dx * dx > r * r:
                continue
            out[max(dy, 0):m.shape[0] + min(dy, 0), max(dx, 0):m.shape[1] + min(dx, 0)] |= \
                m[max(-dy, 0):m.shape[0] + min(-dy, 0), max(-dx, 0):m.shape[1] + min(-dx, 0)]
    return out


def edge_match(frame: Path, canny: Path, radius: int = 2) -> float:
    """How closely a frame keeps the greybox's layout: the share of greybox edges the frame
    also has (within `radius` px), corrected for chance. A busy image has edges near
    everything, so raw recall would reward noise: subtract the share of the frame its
    edges cover anyway and rescale. 1 = every greybox edge, ~0 = no better than chance.
    Detail the frame adds on top of the layout doesn't lower it."""
    ref = np.array(Image.open(canny).convert("L")) > 127
    img = np.array(Image.open(frame).convert("L").resize(ref.shape[::-1]))
    if not ref.any():
        return math.nan
    hit = _dilate(_edges(img), radius)
    m = radius + 1  # the Sobel border and the dilation's edge would bias the coverage low
    recall, cover = float((hit & ref).sum() / ref.sum()), float(hit[m:-m, m:-m].mean())
    if cover >= 0.999:
        return 0.0
    return round(max(0.0, (recall - cover) / (1 - cover)), 4)
