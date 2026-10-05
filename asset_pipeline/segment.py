"""Text-prompted segmentation via SAM 3.1 in ComfyUI, and cutout helpers.

Shared by stage 0 (objects cut from starred scenes to seed the style anchor) and
stage 3 (cutting views out of reference sheets).
"""
import io
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import config
from .comfy import workflow
from .comfy.client import ComfyClient


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
    dets = []
    for out in client.run(graph, manifest["outputs"]):
        m = np.asarray(Image.open(io.BytesIO(out.data)).convert("L")) > 127
        if m.shape[::-1] != size:  # SAM returns source resolution; resize defensively
            m = np.asarray(Image.fromarray(m).resize(size, Image.NEAREST))
        frac = float(m.mean())
        if frac >= min_area_frac:
            edge = bool(m[0].any() or m[-1].any() or m[:, 0].any() or m[:, -1].any())
            dets.append(Detection(mask=m, bbox=_bbox(m), area_frac=frac, touches_border=edge))
    return sorted(dets, key=lambda d: -d.area_frac)


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
