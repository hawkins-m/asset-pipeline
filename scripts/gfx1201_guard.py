"""Process-wide workaround for silent GEMM corruption on gfx1201 (ROCm 7.2).

rocBLAS and hipBLASLt return wrong results, with no error, once a GEMM has too many rows
(M). The limit depends on dtype and output width; measured with
scripts/diag/gemm_rows_sweep.py:
  fp32: M > 2^19.  fp16/bf16 with N=8: M > 2^20.  fp16/bf16 with N=64: M > 2^22.
With a bias term even rows below the limit can come out wrong.

install() wraps torch.nn.functional.linear, which every nn.Linear (and TRELLIS.2's
SparseLinear) calls, so inputs with more than CHUNK rows are processed in CHUNK-row
slices. The result is mathematically identical. Set GFX1201_GUARD=0 to disable it for
A/B checks.
"""
import os

import torch
import torch.nn.functional as F

CHUNK = 1 << 18
_orig_linear = F.linear


def _chunked_linear(input, weight, bias=None):
    k = input.shape[-1] if input.dim() else 0
    rows = input.numel() // k if k else 0
    if not input.is_cuda or input.dim() < 2 or rows <= CHUNK:
        return _orig_linear(input, weight, bias)
    x = input.reshape(rows, k)
    out = torch.empty(rows, weight.shape[0], dtype=torch.result_type(x, weight), device=x.device)
    for i in range(0, rows, CHUNK):
        out[i:i + CHUNK] = _orig_linear(x[i:i + CHUNK], weight, bias)
    return out.reshape(*input.shape[:-1], weight.shape[0])


def install() -> bool:
    """Patch F.linear. Returns True if the guard is active."""
    if os.environ.get("GFX1201_GUARD", "1") == "0":
        return False
    F.linear = _chunked_linear
    return True
