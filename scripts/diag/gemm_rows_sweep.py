"""Sweep F.linear for silent row-range corruption on gfx1201.

For each (dtype, rows M, in K, out N, BLAS backend) compares a single F.linear against the
same op done in 2^18-row chunks (each chunk is far below any known threshold) and reports
the first corrupted row. Exact inputs per case; chunked result is the reference.

    source scripts/rocm_build_env.sh
    HIP_VISIBLE_DEVICES=1 $AP_ROOT/envs/trellis2/bin/python scripts/diag/gemm_rows_sweep.py
"""
import torch
import torch.nn.functional as F

REF_CHUNK = 1 << 18


def first_bad_row(dtype, M, K, N, bias=True):
    g = torch.Generator(device="cuda").manual_seed(0)
    x = torch.randn(M, K, device="cuda", dtype=dtype, generator=g)
    w = torch.randn(N, K, device="cuda", dtype=dtype, generator=g) / K ** 0.5
    b = torch.randn(N, device="cuda", dtype=dtype, generator=g) if bias else None
    out = F.linear(x, w, b)
    ref = torch.cat([F.linear(x[i:i + REF_CHUNK], w, b) for i in range(0, M, REF_CHUNK)])
    tol = 1e-2 if dtype != torch.float32 else 1e-4
    bad = ((out.float() - ref.float()).abs() > tol * (1 + ref.float().abs())).any(1)
    return int(bad.nonzero()[0]) if bad.any() else None


def main():
    print(f"torch {torch.__version__}  hip {torch.version.hip}  {torch.cuda.get_device_name(0)}")
    Ms = [(1 << 19) + 7, (1 << 20) + 7, (1 << 21) + 7, (1 << 22) + 7]
    shapes = [(128, 8), (64, 8), (256, 8), (128, 64), (128, 7), (1024, 1024)]
    for blaslt in (False, True):
        torch.backends.cuda.preferred_blas_library("cublaslt" if blaslt else "cublas")
        print(f"\n=== {'hipBLASLt' if blaslt else 'rocBLAS'} ===")
        for dtype in (torch.float16, torch.bfloat16, torch.float32):
            for K, N in shapes:
                row = []
                for M in Ms:
                    if M * max(K, N) > (1 << 31):  # skip > 4 GB fp16 inputs
                        row.append("   skip")
                        continue
                    fb = first_bad_row(dtype, M, K, N)
                    row.append("     ok" if fb is None else f"bad@{fb:>9}")
                print(f"{str(dtype)[6:]:<9} K={K:<5} N={N:<5} "
                      + "  ".join(f"M=2^{M.bit_length() - 1}+7:{r}" for M, r in zip(Ms, row)),
                      flush=True)
                torch.cuda.empty_cache()


if __name__ == "__main__":
    main()
