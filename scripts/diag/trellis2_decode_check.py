"""Hunt the 1024-mode geometry loss: check the shape decode and mesh extraction for
row-range truncation (the gfx1201 bug class: rows past 2^19 / 2^20 / ... silently wrong).

1. Generates a shape latent (default 1024_cascade) and decodes it with forward hooks on
   every decoder submodule. For each output with > 2^19 rows it compares per-chunk stats
   and flags chunks that are all-zero or whose spread collapses vs the first chunk.
2. Captures flexible_dual_grid_to_mesh's inputs: intersected-flag rate per row chunk.
3. Re-runs o_voxel's hashmap insert/lookup on those coords and checks every lookup
   against an exact torch ground truth (sort + searchsorted).

    source scripts/rocm_build_env.sh
    HIP_VISIBLE_DEVICES=1 PYTHONPATH=$AP_ROOT/src/TRELLIS.2 \
        $AP_ROOT/envs/trellis2/bin/python scripts/diag/trellis2_decode_check.py IMAGE [TYPE]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import trellis2_image_to_glb as runner  # noqa: E402  (sets backend env vars)

import torch  # noqa: E402
from PIL import Image  # noqa: E402

CHUNK = 1 << 19


def chunk_report(feats):
    """Per-chunk (std, frac_all_zero_rows) of a [N, C] tensor."""
    out = []
    for lo in range(0, feats.shape[0], CHUNK):
        c = feats[lo:lo + CHUNK].float()
        out.append((lo, c.std().item(), (c.abs().sum(1) == 0).float().mean().item()))
    return out


def main():
    image_path = Path(sys.argv[1])
    ptype = sys.argv[2] if len(sys.argv) > 2 else "1024_cascade"

    from trellis2.models.sc_vaes import fdg_vae
    from trellis2.pipelines import Trellis2ImageTo3DPipeline
    from o_voxel import _C
    from o_voxel.convert.flexible_dual_grid import _init_hashmap

    runner.write_rocm_config()
    p = Trellis2ImageTo3DPipeline.from_pretrained(str(runner.MODEL_DIR), runner.ROCM_CONFIG)
    p.cuda()
    image = p.preprocess_image(Image.open(image_path))
    with torch.no_grad():
        _, (shape_slat, _tex, res) = p.run(image, seed=42, pipeline_type=ptype,
                                          preprocess_image=False, return_latent=True)
    print(f"{ptype}: decode resolution {res}, latent tokens {shape_slat.feats.shape[0]}", flush=True)

    # 1. Hooks on every decoder submodule.
    dec = p.models["shape_slat_decoder"]
    findings = []

    def hook(name):
        def fn(_mod, _inp, out):
            feats = getattr(out, "feats", out if isinstance(out, torch.Tensor) else None)
            if not isinstance(feats, torch.Tensor) or feats.dim() != 2 or feats.shape[0] <= CHUNK:
                return
            rep = chunk_report(feats)
            base = rep[0][1]
            bad = [(lo, s, z) for lo, s, z in rep[1:] if z > 0.5 or s < 0.05 * base]
            findings.append((name, feats.shape[0], rep, bad))
        return fn

    handles = [m.register_forward_hook(hook(n)) for n, m in dec.named_modules() if n]

    # 2. Capture mesh-extraction inputs.
    captured = {}
    orig = fdg_vae.flexible_dual_grid_to_mesh

    def capture(coords, dual_vertices, intersected_flag, split_weight, **kw):
        captured.update(coords=coords, flags=intersected_flag, grid=kw.get("grid_size"))
        v, f = orig(coords, dual_vertices, intersected_flag, split_weight, **kw)
        captured.update(V=v.shape[0], F=f.shape[0])
        return v, f

    fdg_vae.flexible_dual_grid_to_mesh = capture
    with torch.no_grad():
        p.decode_shape_slat(shape_slat, res)
    fdg_vae.flexible_dual_grid_to_mesh = orig
    for h in handles:
        h.remove()

    print(f"\n[1] decoder submodules with > {CHUNK} rows: {len(findings)}")
    first_bad = next((f for f in findings if f[3]), None)
    for name, n, rep, bad in findings:
        if bad or name.count(".") <= 1:
            print(f"  {'BAD ' if bad else 'ok  '}{name:<48} N={n:>9} "
                  + " ".join(f"[{lo >> 19}]std={s:.3g}{'/zero' if z > 0.5 else ''}" for lo, s, z in rep))
    print("  first suspicious module:", first_bad[0] if first_bad else "none")

    # 2. Intersected-flag rate per chunk (healthy ~ constant across chunks, ~1 flag/voxel).
    coords, flags = captured["coords"], captured["flags"]
    n = coords.shape[0]
    print(f"\n[2] mesh extraction: voxels N={n}  V={captured['V']}  F={captured['F']}  "
          f"F/V={captured['F'] / max(captured['V'], 1):.3f}")
    per = flags.float().sum(1)
    print("  intersected flags/voxel per chunk:",
          " ".join(f"[{lo >> 19}]{per[lo:lo + CHUNK].mean().item():.3f}" for lo in range(0, n, CHUNK)))

    # 3. Hashmap lookup vs exact ground truth.
    grid = torch.as_tensor([captured["grid"]] * 3 if isinstance(captured["grid"], int)
                           else captured["grid"], device=coords.device).int()
    R = int(grid[0])
    hm = _init_hashmap(grid, 2 * n, device=coords.device)
    _C.hashmap_insert_3d_idx_as_val_cuda(
        *hm, torch.cat([torch.zeros_like(coords[:, :1]), coords], dim=-1), *grid.tolist())
    # Query: every voxel's own coords plus its +x/+y/+z neighbours (hits and misses).
    q = torch.cat([coords + torch.tensor(o, device=coords.device, dtype=coords.dtype)
                   for o in ([0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1])])
    q = q[((q >= 0) & (q < R)).all(1)]
    got = _C.hashmap_lookup_3d_cuda(
        *hm, torch.cat([torch.zeros_like(q[:, :1]), q], dim=-1), *grid.tolist()).long()
    lin = lambda c: (c[:, 0].long() * R + c[:, 1].long()) * R + c[:, 2].long()  # noqa: E731
    keys, order = torch.sort(lin(coords))
    ql = lin(q)
    pos = torch.searchsorted(keys, ql).clamp(max=n - 1)
    hit = keys[pos] == ql
    want = torch.where(hit, order[pos], torch.full_like(pos, 0xFFFFFFFF))
    got = got & 0xFFFFFFFF
    wrong = got != want
    print(f"\n[3] hashmap: {q.shape[0]} lookups, {int(hit.sum())} expected hits, "
          f"{int(wrong.sum())} wrong  (false miss {int((wrong & hit).sum())}, "
          f"false hit {int((wrong & ~hit).sum())})")
    if wrong.any():
        idx = want[wrong & hit]
        if idx.numel():
            print(f"  false misses by stored row index: min={int(idx.min())} max={int(idx.max())}"
                  f" (first missing row >= 2^k: k={int(torch.log2(idx.min().float().clamp(min=1)))})")


if __name__ == "__main__":
    main()
