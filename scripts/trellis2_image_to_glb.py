"""Image -> textured GLB with TRELLIS.2 on ROCm / gfx1201.

Run with the trellis2 venv from the TRELLIS.2 source tree on PYTHONPATH:

    source scripts/rocm_build_env.sh
    HIP_VISIBLE_DEVICES=1 PYTHONPATH=$AP_ROOT/src/TRELLIS.2 \
        $AP_ROOT/envs/trellis2/bin/python scripts/trellis2_image_to_glb.py IMAGE [--type 512]
"""
import argparse
import json
import os
import time
from pathlib import Path

# Backend selection must happen before trellis2 is imported.
os.environ.setdefault("ATTN_BACKEND", "sdpa")
os.environ.setdefault("SPARSE_ATTN_BACKEND", "sdpa")
os.environ.setdefault("SPARSE_CONV_BACKEND", "flex_gemm")
os.environ.setdefault("TORCH_BLAS_PREFER_HIPBLASLT", "0")
# expandable_segments produces silent NaNs on this stack (see CLAUDE.md).
if "expandable_segments" in os.environ.get("PYTORCH_CUDA_ALLOC_CONF", ""):
    raise SystemExit("Unset PYTORCH_CUDA_ALLOC_CONF=expandable_segments: silent NaNs on gfx1201")

import torch  # noqa: E402
from PIL import Image  # noqa: E402

AP_ROOT = Path(os.environ.get("AP_ROOT", "/mnt/storage/asset-pipeline"))
MODEL_DIR = AP_ROOT / "models" / "TRELLIS.2-4B"

# Upstream's DINOv3 and RMBG-2.0 repos are gated. The DINOv3 mirror's weights are
# sha256-identical to facebook/dinov3-vitl16-pretrain-lvd1689m; RMBG-2.0 is a BiRefNet
# finetune, and ZhengPeng7/BiRefNet is the original (MIT) with the same interface.
OVERRIDES = {
    "image_cond_model": "PIA-SPACE-LAB/dinov3-vitl-pretrain-lvd1689m",
    "rembg_model": "ZhengPeng7/BiRefNet",
}
ROCM_CONFIG = "pipeline.rocm.json"


def write_rocm_config() -> None:
    cfg = json.loads((MODEL_DIR / "pipeline.json").read_text())
    for key, repo in OVERRIDES.items():
        cfg["args"][key]["args"]["model_name"] = repo
    (MODEL_DIR / ROCM_CONFIG).write_text(json.dumps(cfg, indent=4))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image", type=Path)
    ap.add_argument("--type", default="1024_cascade",
                    choices=["512", "1024", "1024_cascade", "1536_cascade"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--decimate", type=int, default=1_000_000)
    ap.add_argument("--texture-size", type=int, default=2048)
    ap.add_argument("--out-dir", type=Path, default=AP_ROOT / "outputs" / "trellis2")
    args = ap.parse_args()

    from trellis2.pipelines import Trellis2ImageTo3DPipeline
    import o_voxel

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{args.image.stem[:24]}_{args.type}_s{args.seed}"
    stats = {"image": str(args.image), "type": args.type, "seed": args.seed,
             "torch": torch.__version__, "device": torch.cuda.get_device_name(0)}

    t = time.time()
    write_rocm_config()
    pipeline = Trellis2ImageTo3DPipeline.from_pretrained(str(MODEL_DIR), ROCM_CONFIG)
    pipeline.cuda()
    stats["load_s"] = round(time.time() - t, 1)

    torch.cuda.reset_peak_memory_stats()
    t = time.time()
    # Save what the model actually sees (background removed, cropped) for debugging.
    image = pipeline.preprocess_image(Image.open(args.image))
    image.save(args.out_dir / f"{stem}_input.png")
    mesh = pipeline.run(image, seed=args.seed, pipeline_type=args.type,
                        preprocess_image=False)[0]
    stats["generate_s"] = round(time.time() - t, 1)
    stats["raw_vertices"], stats["raw_faces"] = len(mesh.vertices), len(mesh.faces)
    mesh.simplify(16777216)  # nvdiffrast limit

    t = time.time()
    glb = o_voxel.postprocess.to_glb(
        vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs,
        coords=mesh.coords, attr_layout=mesh.layout, voxel_size=mesh.voxel_size,
        aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        decimation_target=args.decimate, texture_size=args.texture_size,
        remesh=True, remesh_band=1, remesh_project=0, verbose=True,
    )
    out = args.out_dir / f"{stem}.glb"
    glb.export(out)
    stats["export_s"] = round(time.time() - t, 1)
    stats["peak_vram_gb"] = round(torch.cuda.max_memory_allocated() / 2**30, 2)
    stats["glb"] = str(out)
    stats["glb_vertices"], stats["glb_faces"] = len(glb.vertices), len(glb.faces)
    (args.out_dir / f"{stem}.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
