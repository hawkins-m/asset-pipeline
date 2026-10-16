"""Per-pixel ADE20K labels and relative depth for painted frames (runs in envs/segdepth).

    HIP_VISIBLE_DEVICES=0 $AP_ROOT/envs/segdepth/bin/python scripts/diag/seg_depth_dump.py \
        OUT_DIR FRAME [FRAME ...] [--models oneformer depth mask2former]

Writes per frame <OUT_DIR>/<frame slug>.<model>.png (uint8 ADE20K class index per pixel;
depth: uint16 relative inverse depth, near = high) and <OUT_DIR>/ade20k.json (index -> name).
Models from $AP_ROOT/models (scripts/install_segdepth.sh):
  oneformer   OneFormer Swin-L ADE20K, semantic task (MIT)
  depth       Depth Anything V2 Small (Apache-2.0; Base/Large/Giant are CC-BY-NC-4.0)
  mask2former EXPERIMENTAL, A/B only, not by default: Mask2Former Swin-L ADE20K semantic
              (HF card "other"; install_segdepth.sh mask2former)
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import torch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gfx1201_guard  # noqa: E402

MODELS = Path(os.environ.get("AP_ROOT", "/mnt/storage/asset-pipeline")) / "models"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("frames", nargs="+")
    ap.add_argument("--models", nargs="+", default=["oneformer", "depth"])
    a = ap.parse_args()
    gfx1201_guard.install()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    imgs = [(f, Image.open(f).convert("RGB")) for f in a.frames]
    labels = None
    if "oneformer" in a.models:
        from transformers import OneFormerForUniversalSegmentation, OneFormerProcessor
        p = MODELS / "oneformer_ade20k_swin_large"
        proc = OneFormerProcessor.from_pretrained(p)
        model = OneFormerForUniversalSegmentation.from_pretrained(p).to(dev).eval()
        labels = model.config.id2label
        for f, im in imgs:
            t = time.time()
            x = proc(images=im, task_inputs=["semantic"], return_tensors="pt").to(dev)
            with torch.no_grad():
                o = model(**x)
            seg = proc.post_process_semantic_segmentation(o, target_sizes=[im.size[::-1]])[0]
            Image.fromarray(seg.cpu().numpy().astype(np.uint8)).save(out / f"{slug(f)}.oneformer.png")
            print(f"oneformer {f}: {time.time() - t:.1f}s", flush=True)
        del model
    if "mask2former" in a.models:
        from transformers import AutoImageProcessor, Mask2FormerForUniversalSegmentation
        p = MODELS / "mask2former-swin-large-ade-semantic"
        proc = AutoImageProcessor.from_pretrained(p)
        model = Mask2FormerForUniversalSegmentation.from_pretrained(p).to(dev).eval()
        labels = labels or model.config.id2label
        for f, im in imgs:
            t = time.time()
            x = proc(images=im, return_tensors="pt").to(dev)
            with torch.no_grad():
                o = model(**x)
            seg = proc.post_process_semantic_segmentation(o, target_sizes=[im.size[::-1]])[0]
            Image.fromarray(seg.cpu().numpy().astype(np.uint8)).save(out / f"{slug(f)}.mask2former.png")
            print(f"mask2former {f}: {time.time() - t:.1f}s", flush=True)
        del model
    if "depth" in a.models:
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
        p = MODELS / "Depth-Anything-V2-Small-hf"
        proc = AutoImageProcessor.from_pretrained(p)
        model = AutoModelForDepthEstimation.from_pretrained(p).to(dev).eval()
        for f, im in imgs:
            t = time.time()
            x = proc(images=im, return_tensors="pt").to(dev)
            with torch.no_grad():
                d = model(**x).predicted_depth
            d = torch.nn.functional.interpolate(d[:, None], size=im.size[::-1], mode="bicubic")[0, 0]
            d = d.float().cpu().numpy()
            d = (d - d.min()) / max(float(d.max() - d.min()), 1e-6)     # relative inverse depth, near = 1
            Image.fromarray((d * 65535).astype(np.uint16)).save(out / f"{slug(f)}.depth.png")
            print(f"depth {f}: {time.time() - t:.1f}s", flush=True)
    if labels:
        (out / "ade20k.json").write_text(json.dumps({int(k): v for k, v in labels.items()}, indent=0))


if __name__ == "__main__":
    main()
