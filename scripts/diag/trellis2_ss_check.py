"""Check TRELLIS.2 stage 1 (sparse structure / occupancy) on GPU against a CPU fp32 reference.

Same image, conditioning and noise. Compares:
  A. GPU flow + GPU decoder (what the pipeline does)
  B. GPU flow latent decoded on CPU fp32       -> isolates the conv3d decoder
  C. CPU fp32 flow + CPU fp32 decoder          -> isolates the flow DiT / attention
Writes max-projection images of each occupancy grid next to the outputs.

    source scripts/rocm_build_env.sh
    HIP_VISIBLE_DEVICES=1 PYTHONPATH=$AP_ROOT/src/TRELLIS.2 \
        $AP_ROOT/envs/trellis2/bin/python scripts/diag/trellis2_ss_check.py IMAGE
"""
import copy
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trellis2_image_to_glb as runner  # noqa: E402  (sets backend env vars)

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image  # noqa: E402


def occ_stats(name, occ, ref=None):
    occ = occ.bool().cpu()
    msg = f"{name:<34} voxels={int(occ.sum()):>7}"
    if ref is not None:
        ref = ref.bool().cpu()
        inter, union = (occ & ref).sum().item(), (occ | ref).sum().item()
        msg += f"  IoU_vs_ref={inter / max(union, 1):.4f}"
    print(msg, flush=True)


def projections(occ):
    g = occ.bool().cpu().numpy()[0, 0]
    tiles = [(g.any(axis=a) * 255).astype(np.uint8) for a in range(3)]
    return Image.fromarray(np.concatenate(tiles, axis=1)).resize((3 * 256, 256), Image.NEAREST)


def main():
    image_path = Path(sys.argv[1])
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    out = runner.AP_ROOT / "outputs" / "diag"
    out.mkdir(parents=True, exist_ok=True)

    from trellis2.pipelines import Trellis2ImageTo3DPipeline
    runner.write_rocm_config()
    p = Trellis2ImageTo3DPipeline.from_pretrained(str(runner.MODEL_DIR), runner.ROCM_CONFIG)
    p.cuda()

    image = p.preprocess_image(Image.open(image_path))
    cond = p.get_cond([image], 512)
    flow = p.models["sparse_structure_flow_model"]
    dec = p.models["sparse_structure_decoder"]
    params = dict(p.sparse_structure_sampler_params)
    reso = flow.resolution

    torch.manual_seed(seed)
    noise = torch.randn(1, flow.in_channels, reso, reso, reso)

    # A: GPU, as the pipeline runs it.
    flow.cuda(); dec.cuda()
    t = time.time()
    with torch.no_grad():
        z_gpu = p.sparse_structure_sampler.sample(
            flow, noise.cuda(), **cond, **params, verbose=False).samples
        occ_a = dec(z_gpu) > 0
    print(f"GPU stage 1: {time.time() - t:.1f}s  latent mean={z_gpu.mean():.4f} std={z_gpu.std():.4f}"
          f" finite={bool(torch.isfinite(z_gpu).all())}")

    # CPU fp32 copies of both models.
    flow_cpu = copy.deepcopy(flow).cpu(); flow_cpu.convert_to(torch.float32)
    dec_cpu = copy.deepcopy(dec).cpu(); dec_cpu.convert_to_fp32()
    cond_cpu = {k: v.float().cpu() for k, v in cond.items()}

    # B: GPU latent, CPU decoder.
    with torch.no_grad():
        occ_b = dec_cpu(z_gpu.float().cpu()) > 0

    # C: full CPU reference.
    t = time.time()
    with torch.no_grad():
        z_cpu = p.sparse_structure_sampler.sample(
            flow_cpu, noise, **cond_cpu, **params, verbose=True).samples
        occ_c = dec_cpu(z_cpu) > 0
    print(f"CPU stage 1: {time.time() - t:.1f}s  latent mean={z_cpu.mean():.4f} std={z_cpu.std():.4f}")
    cos = torch.nn.functional.cosine_similarity(
        z_gpu.float().cpu().flatten(), z_cpu.flatten(), dim=0).item()
    print(f"latent cosine GPU vs CPU: {cos:.5f}")

    occ_stats("C  CPU flow + CPU dec (reference)", occ_c)
    occ_stats("A  GPU flow + GPU dec (pipeline)", occ_a, occ_c)
    occ_stats("B  GPU flow + CPU dec", occ_b, occ_c)
    occ_stats("A vs B (decoder only)", occ_a, occ_b)

    stem = f"ss_{image_path.stem[:16]}_s{seed}"
    rows = [projections(o) for o in (occ_a, occ_b, occ_c)]
    sheet = Image.new("L", (rows[0].width, 3 * rows[0].height))
    for i, r in enumerate(rows):
        sheet.paste(r, (0, i * r.height))
    sheet.save(out / f"{stem}_proj.png")
    image.save(out / f"{stem}_input.png")
    print("rows: A (GPU), B (GPU latent, CPU dec), C (CPU ref); cols: proj along x, y, z")
    print("wrote", out / f"{stem}_proj.png")


if __name__ == "__main__":
    main()
