// Repro: hipMemcpy2D device-to-device silently copies only the first 2^20 rows on
// ROCm 7.2.x / gfx1201 (returns hipSuccess). Expected output: first_bad_row=-1 for all.
//   source scripts/rocm_build_env.sh
//   $ROCM_HOME/bin/hipcc --offload-arch=gfx1201 -w scripts/diag/hip_memcpy2d_repro.hip -o /tmp/m2d && /tmp/m2d
#include <hip/hip_runtime.h>
#include <cstdio>
#include <vector>
// hipMemcpy2D device-to-device, 12-byte rows (float3), pitch == width.
int main() {
  for (size_t rows : {size_t(1) << 20, (size_t(1) << 20) + 1, size_t(3) << 20}) {
    size_t w = 12, bytes = rows * w;
    std::vector<unsigned char> h(bytes);
    for (size_t i = 0; i < bytes; i++) h[i] = (unsigned char)(i * 7 + 1);
    void *src, *dst;
    (void)hipMalloc(&src, bytes); (void)hipMalloc(&dst, bytes);
    (void)hipMemcpy(src, h.data(), bytes, hipMemcpyHostToDevice);
    (void)hipMemset(dst, 0, bytes);
    hipError_t e = hipMemcpy2D(dst, w, src, w, w, rows, hipMemcpyDeviceToDevice);
    std::vector<unsigned char> o(bytes);
    (void)hipMemcpy(o.data(), dst, bytes, hipMemcpyDeviceToHost);
    long first_bad = -1;
    for (size_t i = 0; i < bytes; i++) if (o[i] != h[i]) { first_bad = (long)(i / w); break; }
    printf("rows=%zu err=%s first_bad_row=%ld\n", rows, hipGetErrorString(e), first_bad);
    (void)hipFree(src); (void)hipFree(dst);
  }
}
