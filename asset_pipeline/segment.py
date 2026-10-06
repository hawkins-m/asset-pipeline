"""Text-prompted segmentation via SAM 3.1 in ComfyUI, and cutout helpers.

Shared by stage 0 (objects cut from starred scenes to seed the style anchor) and
stage 3 (cutting views out of reference sheets).
"""
import io
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from . import config
from .comfy import workflow
from .comfy.client import ComfyClient, ComfyError


@dataclass
class Detection:
    mask: np.ndarray            # bool [H, W], same size as the source image
    bbox: tuple[int, int, int, int]  # x0, y0, x1, y1 (exclusive)
    area_frac: float            # mask area / image area
    touches_border: bool        # likely cut off by the frame (mask reaches an image edge)


def _bbox(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def overlap(a: np.ndarray, b: np.ndarray) -> float:
    """Intersection over the smaller mask: ~1 when one mask lies inside the other (an
    object and one of its parts), unlike IoU."""
    small = min(a.sum(), b.sum())
    return float(np.logical_and(a, b).sum() / small) if small else 0.0


def iou(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 0.0


def sam3_prompt(noun: str, max_dets: int) -> str:
    """SAM 3's text encoder parses "a:N, b:M" (comma = category, :N = max detections),
    so strip those characters from the noun itself."""
    clean = re.sub(r"[,:()]", " ", noun).strip()
    clean = re.sub(r"\s+", " ", clean)
    if not clean:
        raise ValueError(f"empty segmentation prompt from {noun!r}")
    return f"{clean}:{max_dets}"


def detect(client: ComfyClient, image: Path, noun: str, max_dets: int = 8,
           threshold: float = 0.5, min_area_frac: float = 0.002) -> list[Detection]:
    """Up to max_dets objects SAM 3 finds for `noun`, largest first. Tiny masks dropped.
    (A plain prompt without ":N" returns at most one object.)"""
    name = config.backends()["comfyui"]["workflows"].get("segment", "sam3_detect")
    graph, manifest = workflow.load_template(name)
    graph = workflow.fill(graph, manifest, {"image": client.upload_image(image),
                                            "text": sam3_prompt(noun, max_dets),
                                            "threshold": threshold})
    size = Image.open(image).size
    try:
        outputs = client.run(graph, manifest["outputs"])
    except ComfyError as e:
        if _no_detections(e):
            return []
        raise
    dets = []
    for out in outputs:
        m = np.asarray(Image.open(io.BytesIO(out.data)).convert("L")) > 127
        if m.shape[::-1] != size:  # SAM returns source resolution; resize defensively
            m = np.asarray(Image.fromarray(m).resize(size, Image.NEAREST))
        frac = float(m.mean())
        if frac >= min_area_frac:
            edge = bool(m[0].any() or m[-1].any() or m[:, 0].any() or m[:, -1].any())
            dets.append(Detection(mask=m, bbox=_bbox(m), area_frac=frac, touches_border=edge))
    return sorted(dets, key=lambda d: -d.area_frac)


def _no_detections(e: ComfyError) -> bool:
    """SAM 3 finding nothing makes the graph fail: the empty mask batch crashes the image
    output node ("index 0 is out of bounds for dimension 0 with size 0")."""
    err = e.execution_error()
    return (err.get("exception_type") == "IndexError"
            and "with size 0" in err.get("exception_message", ""))


def white_components(image: Path, n_max: int = 8, white: int = 235, min_gap: int = 8,
                     min_area_frac: float = 0.002) -> list[Detection]:
    """Fallback for sheets on white when SAM misses: background = near-white pixels
    connected to the border (so white details inside an object stay opaque); objects =
    foreground split at runs of >= min_gap empty columns. Largest first."""
    im = Image.open(image).convert("RGB")
    fg = np.asarray(im).min(axis=2) < white
    # Flood the background from the border on a padded binary image.
    # .copy(): an image from fromarray shares the array read-only, and floodfill then
    # silently changes nothing (Pillow 12).
    pad = Image.fromarray(np.pad(~fg, 1, constant_values=True).astype(np.uint8) * 255).copy()
    ImageDraw.floodfill(pad, (0, 0), 128)
    obj = np.asarray(pad)[1:-1, 1:-1] != 128
    cols = obj.any(axis=0)
    runs, start, gap = [], None, 0
    for x, on in enumerate(list(cols) + [False] * min_gap):
        if on:
            start, gap = (x if start is None else start), 0
        elif start is not None:
            gap += 1
            if gap >= min_gap:
                runs.append((start, x - gap + 1))
                start, gap = None, 0
    dets = []
    for x0, x1 in runs:
        m = np.zeros_like(obj)
        m[:, x0:x1] = obj[:, x0:x1]
        frac = float(m.mean())
        if frac >= min_area_frac:
            edge = bool(m[0].any() or m[-1].any() or m[:, 0].any() or m[:, -1].any())
            dets.append(Detection(mask=m, bbox=_bbox(m), area_frac=frac, touches_border=edge))
    return sorted(dets, key=lambda d: -d.area_frac)[:n_max]


def cutout(image: Path | Image.Image, det: Detection, pad_frac: float = 0.08,
           square: bool = True) -> tuple[Image.Image, Image.Image]:
    """(RGBA cutout, RGB on white) cropped to the mask with padding."""
    src = (Image.open(image) if isinstance(image, Path) else image).convert("RGB")
    rgba = src.copy()
    rgba.putalpha(Image.fromarray((det.mask * 255).astype(np.uint8)))
    x0, y0, x1, y1 = det.bbox
    w, h = x1 - x0, y1 - y0
    side_w, side_h = (max(w, h),) * 2 if square else (w, h)
    pw, ph = int(side_w * (1 + 2 * pad_frac)), int(side_h * (1 + 2 * pad_frac))
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    box = (cx - pw // 2, cy - ph // 2, cx - pw // 2 + pw, cy - ph // 2 + ph)
    rgba = rgba.crop(box)  # PIL pads out-of-bounds with transparent black
    white = Image.new("RGB", rgba.size, "white")
    white.paste(rgba, mask=rgba.getchannel("A"))
    return rgba, white
