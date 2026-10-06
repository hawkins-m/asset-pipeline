"""World mode, stage 0: concept frames per shot camera, generated onto the greybox.

Each frame follows its shot's control passes (depth + canny rendered from the greybox,
sw_shots), so every shot shows the same layout. The prompt says what the camera sees,
taken from the shot's object-id pass rather than written per shot:
  - tier and the shot's own notes;
  - the visible slot types, by screen coverage;
  - the material notes of the visible districts.
The project's style anchor (Redux images + style text) is applied as everywhere else.
Consistency across shots: approved frames of other shots can be added as extra Redux
references (`refs`); depth locks the structure, so their content leakage mostly carries
materials and palette.

Layout under the project:
    frames/<shot>/batch_NNN/frame_###.png + meta.json   (stars in review.json = approved)
"""
import math
import re
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .. import review
from ..imagegen.base import ControlImage, GenRequest
from ..imagegen.registry import backend_for
from ..project import ProjectStore, read_json, write_json
from ..schema import FrameSettings, StyleAnchor
from . import sw_shots, sw_site

FRAMES = "frames"
MIN_COVERAGE = 0.01          # slot types under this share of the frame aren't named
TIER = {"wide": "wide establishing view", "medium": "eye-level view", "tight": "close-up detail view"}
# What each greybox slot type is, for the prompt (plural forms: types usually repeat).
TYPE_WORDS = {"temple": "classical temples with colonnades", "rotunda": "a domed rotunda ringed by columns",
              "stoa": "a curved colonnaded stoa", "block": "monumental civic buildings with arcades",
              "villa": "courtyard villas", "garden": "lush gardens", "canal": "a canal",
              "pool": "reflecting pools", "avenue": "broad paved avenues", "radial": "a broad paved avenue",
              "plaza": "a circular paved plaza", "terrace": "terraces", "sea": "the sea",
              "terrain": "the landscape"}
SUFFIX = "cinematic environment concept art, one coherent city, consistent architecture"


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
    """(slot type, coverage) and (district, coverage) seen by the shot, largest first."""
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
        types[s["type"]] = types.get(s["type"], 0) + v["frac"]
        if s["district"]:
            districts[s["district"]] = districts.get(s["district"], 0) + v["frac"]
    order = lambda d: sorted(d.items(), key=lambda kv: -kv[1])  # noqa: E731
    return order(types), order(districts)


def prompt_for(store: ProjectStore, shot: str) -> str:
    meta = _shot_meta(store, shot)
    s = meta["shot"]
    types, districts = visible(store, shot)
    layout = sw_site.load_layout(store)
    notes = {d.id: d.notes for d in layout.districts if d.notes}
    parts = [TIER.get(s["tier"], "view")]
    if s.get("notes"):
        parts.append(s["notes"])
    seen = [TYPE_WORDS.get(t, t) for t, f in types if f >= MIN_COVERAGE and t not in ("terrain",)]
    if seen:
        parts.append("showing " + ", ".join(dict.fromkeys(seen)))
    # the focus district first (if set), then the others by coverage; at most two
    ds = [d for d, f in districts if f >= MIN_COVERAGE]
    if s.get("district") in notes:
        ds = [s["district"]] + [d for d in ds if d != s["district"]]
    mats = [notes[d] for d in ds if d in notes][:2]
    if mats:
        parts.append("; ".join(mats))
    parts.append(SUFFIX)
    return ", ".join(p.strip().rstrip(".") for p in parts if p.strip())


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
             prompt: str | None = None, out: Path | None = None) -> Path:
    """n concept frames for one shot into a new batch dir; returns the dir.

    refs: project-relative images (e.g. an approved wide frame) added to the Redux anchor
    with `ref_strength` more total strength."""
    meta = _shot_meta(store, shot)
    if meta["greybox_sha256"] != sw_site.load_greybox(store)["blend_sha256"]:
        raise ValueError(f"shot {shot}'s passes are stale (the greybox changed): ap shots render first")
    project = store.load()
    fs = fs or settings(store)
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
    t, results = time.time(), []
    try:
        for i in range(n):  # one image per request: each gets its own seed, VRAM stays flat
            s = None if seed is None else seed + i
            req = GenRequest(prompt=prompt, width=w, height=h, n=1, seed=s, steps=fs.steps, anchor=anchor,
                             control=controls(store, shot, fs), control_model=fs.model)
            r = backend.generate(req, out, prefix=f"tmp{i:03d}")[0]
            final = out / f"frame_{len(results):03d}.png"
            r.path.rename(final)
            results.append({"file": final.name, "seed": r.seed,
                            "edge_match": edge_match(final, sw_shots.shot_dir(store, shot) / "canny.png")})
    finally:  # a canceled run keeps the frames already made
        write_json(out / "meta.json", {
            "shot": shot, "prompt": prompt, "settings": fs.model_dump(mode="json"),
            "anchor": anchor.model_dump(mode="json") if anchor else None, "refs": refs or [],
            "greybox_sha256": meta["greybox_sha256"], "size": [w, h], "backend": backend.name,
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
