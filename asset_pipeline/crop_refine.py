"""Library crop refinement: what of a greybox slot's id-pass region a painted frame actually
draws as that building (A/B in docs/notes/library.md "Asset library plan").

  1. R  the slot's id-pass region
  2. L  R and ADE20K building-like labels (OneFormer Swin-L, MIT): trees, sky, people,
        ground and water drop out
  3. D  L minus depth occluders: Depth Anything V2 Small (Apache-2.0) on the painting,
        fitted affinely to the greybox's own inverse depth over the frame's building pixels;
        what the painting puts clearly nearer (a tree or another building in front) drops
        out, then components under 5% of the largest
  4. S  SAM 3.1 inside D's box ("building"), kept near D; D when SAM finds too little

The labels and depth come from scripts/diag/seg_depth_dump.py in its own env
(envs/segdepth, scripts/install_segdepth.sh), on whichever GPU is free; they're cached per
frame (`seg_dir`). Only the SAM box step talks to ComfyUI.
"""
import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

from . import config, segment, vlm
from . import gpu as gpu_mod

# ADE20K classes that are a building or part of one
KEEP = {"building", "house", "skyscraper", "wall", "column", "tower", "hovel", "door", "windowpane", "railing",
        "stairs", "step", "stairway", "awning", "canopy", "booth", "tent", "fountain", "sculpture", "bannister",
        "base", "arcade", "bridge", "pier", "shed", "ceiling", "fence", "balustrade", "kiosk", "stage", "counter",
        "signboard", "glass", "screen door"}
MODELS = ("oneformer", "depth")
DUMP = Path(__file__).resolve().parents[1] / "scripts/diag/seg_depth_dump.py"
USABLE = 0.30            # kept share of the id region under which a crop is "no usable crop"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def env_python() -> Path:
    return config.ap_root() / "envs/segdepth/bin/python"


def available() -> bool:
    return env_python().is_file()


def free_gpu() -> int:
    """A GPU no TRELLIS job and no VLM server holds (0 first: ComfyUI lives on 1)."""
    st = vlm.state()
    for g in (0, 1):
        if not vlm.trellis_jobs(g) and not (st and st.get("gpu") == g):
            return g
    raise RuntimeError("both GPUs are busy (TRELLIS or the VLM): try again later")


def dump(root: Path, frames: list[str], seg_dir: Path, log=None) -> None:
    """Labels and depth for the frames not dumped yet (one subprocess for all)."""
    todo = [f for f in frames if not all((seg_dir / f"{_slug(f)}.{m}.png").is_file() for m in MODELS)]
    if not todo:
        return
    if not available():
        raise FileNotFoundError("no envs/segdepth: run scripts/install_segdepth.sh")
    gpu = free_gpu()
    cmd = [str(env_python()), str(DUMP), str(seg_dir), *todo, "--models", *MODELS]
    if log:
        log(f"labels + depth for {len(todo)} frame(s) on GPU {gpu}")
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                       env=dict(os.environ, **gpu_mod.visible_devices_env(gpu), AP_ROOT=str(config.ap_root())))
    if r.returncode != 0:
        raise RuntimeError("seg_depth_dump failed:\n" + "\n".join((r.stdout + r.stderr).splitlines()[-15:]))


def has_dump(seg_dir: Path, frame: str) -> bool:
    return all((seg_dir / f"{_slug(frame)}.{m}.png").is_file() for m in MODELS) and (seg_dir / "ade20k.json").is_file()


def keep_ids(seg_dir: Path) -> list[int]:
    names = {int(k): v for k, v in json.loads((seg_dir / "ade20k.json").read_text()).items()}
    return [k for k, v in names.items() if v.split(",")[0].strip() in KEEP]


def refine(frame_path: Path, seg_dir: Path, frame: str, R: np.ndarray, greybox_z: np.ndarray,
           client=None, noun: str = "building") -> dict[str, np.ndarray]:
    """Masks L, D, S (see the module docstring) for region R of one frame. greybox_z: the
    shot's camera depth in metres (0 = sky), at the frame's size."""
    labels = np.asarray(Image.open(seg_dir / f"{_slug(frame)}.oneformer.png"))
    depth = np.asarray(Image.open(seg_dir / f"{_slug(frame)}.depth.png")).astype(np.float64) / 65535.0
    keep = np.isin(labels, keep_ids(seg_dir))
    L = R & keep
    hit = greybox_z > 0
    inv = np.zeros_like(greybox_z, dtype=np.float64)
    inv[hit] = 1.0 / greybox_z[hit]
    fit = hit & keep
    D = L.copy()
    if fit.sum() > 500:
        x, y = inv[fit], depth[fit]
        sub = np.random.default_rng(0).choice(len(x), min(len(x), 20000), replace=False)
        A = np.stack([x[sub], np.ones(len(sub))], 1)
        coef = np.linalg.lstsq(A, y[sub], rcond=None)[0]
        for _ in range(2):          # refit on the inliers
            res = y[sub] - A @ coef
            ok = np.abs(res) < 2.5 * np.median(np.abs(res)) + 1e-6
            coef = np.linalg.lstsq(A[ok], y[sub][ok], rcond=None)[0]
        tau = max(0.06, 2.5 * float(np.median(np.abs(y - (coef[0] * x + coef[1])))))
        D = L & ~((depth - (coef[0] * inv + coef[1])) > tau)
    lab, n = ndimage.label(D)
    if n:
        sizes = ndimage.sum(D, lab, range(1, n + 1))
        D = np.isin(lab, [i + 1 for i, s in enumerate(sizes) if s >= 0.05 * sizes.max()])
    S = D
    if client is not None and D.sum() > 50:
        ys, xs = np.nonzero(D)
        dets = segment.detect_box(client, frame_path, noun, (xs.min() - 6, ys.min() - 6, xs.max() + 6, ys.max() + 6))
        if dets:
            near = ndimage.binary_dilation(D, iterations=12)
            m = np.zeros_like(D)
            for d in dets:
                if (d.mask & D).sum() >= 0.3 * d.mask.sum():
                    m |= d.mask
            m &= near
            if m.sum() > 0.3 * D.sum():
                S = m
    return {"L": L, "D": D, "S": S}


def greybox_depth(shot_dir: Path, size: tuple[int, int]) -> np.ndarray:
    """The shot's greybox camera depth in metres at `size` (0 = sky): depth_raw.png is 16-bit
    camera Z over 10 km."""
    z = np.asarray(Image.open(shot_dir / "depth_raw.png")).astype(np.float64) / 65535.0 * 10000.0
    if z.shape[::-1] != tuple(size):
        z = np.asarray(Image.fromarray(z).resize(size, Image.NEAREST))
    return z
