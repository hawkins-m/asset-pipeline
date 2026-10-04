"""Check TRELLIS.2 stage 2 (shape SLat flow, 512) on GPU against references.

Stage 1 coords and noise are shared. Variants of the shape flow:
  gpu      GPU, default SDPA kernels (aotriton), as the pipeline runs it
  gpu_math GPU, SDPA forced to the MATH backend          -> isolates attention kernels
  cpu      CPU fp32                                      -> full reference
Each latent is decoded on GPU (the decoder needs FlexGEMM) and mesh stats are compared.

    source scripts/rocm_build_env.sh
    HIP_VISIBLE_DEVICES=1 PYTHONPATH=$AP_ROOT/src/TRELLIS.2 \
        $AP_ROOT/envs/trellis2/bin/python scripts/diag/trellis2_shape_check.py IMAGE [--skip-cpu]
"""
import copy
import sys
from contextlib import nullcontext
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trellis2_image_to_glb as runner  # noqa: E402  (sets backend env vars)

import torch  # noqa: E402
from PIL import Image  # noqa: E402
from torch.nn.attention import SDPBackend, sdpa_kernel  # noqa: E402


def mesh_stats(name, mesh):
    v, f = mesh.vertices, mesh.faces
    size = (v.max(0).values - v.min(0).values).tolist()
    print(f"{name:<9} V={len(v):>8} F={len(f):>8} F/V={len(f) / max(len(v), 1):.2f} "
          f"bbox={[round(s, 3) for s in size]}", flush=True)


def main():
    image_path = Path(sys.argv[1])
    skip_cpu = "--skip-cpu" in sys.argv
    seed = 42

    from trellis2.modules.sparse import SparseTensor
    from trellis2.pipelines import Trellis2ImageTo3DPipeline
    runner.write_rocm_config()
    p = Trellis2ImageTo3DPipeline.from_pretrained(str(runner.MODEL_DIR), runner.ROCM_CONFIG)
    p.cuda()
    p.low_vram = False

    image = p.preprocess_image(Image.open(image_path))
    torch.manual_seed(seed)
    cond = p.get_cond([image], 512)
    with torch.no_grad():
        coords = p.sample_sparse_structure(cond, 32, 1, {})
    flow = p.models["shape_slat_flow_model_512"].cuda()
    print(f"stage-1 coords: {coords.shape[0]} tokens", flush=True)

    torch.manual_seed(seed + 1)
    feats = torch.randn(coords.shape[0], flow.in_channels)
    params = dict(p.shape_slat_sampler_params)
    std = torch.tensor(p.shape_slat_normalization["std"])[None]
    mean = torch.tensor(p.shape_slat_normalization["mean"])[None]

    def run_flow(model, device, ctx=None):
        noise = SparseTensor(feats=feats.to(device), coords=coords.to(device))
        c = {k: v.to(device).float() if device == "cpu" else v for k, v in cond.items()}
        t = time.time()
        with torch.no_grad(), (ctx if ctx is not None else nullcontext()):
            slat = p.shape_slat_sampler.sample(model, noise, **c, **params, verbose=False).samples
        print(f"  flow on {device}{' (math sdpa)' if ctx else ''}: {time.time() - t:.1f}s", flush=True)
        return slat

    latents = {"gpu": run_flow(flow, "cuda"),
               "gpu_math": run_flow(flow, "cuda", sdpa_kernel([SDPBackend.MATH]))}
    if not skip_cpu:
        flow_cpu = copy.deepcopy(flow).cpu()
        flow_cpu.convert_to(torch.float32)
        latents["cpu"] = run_flow(flow_cpu, "cpu")

    ref = latents.get("cpu", latents["gpu_math"]).feats.float().cpu()
    for name, slat in latents.items():
        f = slat.feats.float().cpu()
        cos = torch.nn.functional.cosine_similarity(f, ref, dim=1)
        print(f"{name:<9} latent std={f.std():.4f} finite={bool(torch.isfinite(f).all())} "
              f"per-token cos vs ref: mean={cos.mean():.4f} min={cos.min():.4f} "
              f"frac<0.9={(cos < 0.9).float().mean():.4f}", flush=True)

    for name, slat in latents.items():
        slat = SparseTensor(feats=slat.feats.cuda(), coords=slat.coords.cuda())
        slat = slat * std.cuda() + mean.cuda()
        with torch.no_grad():
            meshes, _ = p.decode_shape_slat(slat, 512)
        mesh_stats(name, meshes[0])


if __name__ == "__main__":
    main()
