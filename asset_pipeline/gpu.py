"""GPU vendor differences in one place: which tool reports VRAM, which env var pins a GPU.

AMD (ROCm) is the tested path. The NVIDIA branches are untested (no NVIDIA hardware here).
"""
import json
import shutil
import subprocess

ROCM_SMI = "/opt/rocm/bin/rocm-smi"


def _rocm_smi() -> str | None:
    return ROCM_SMI if shutil.which(ROCM_SMI) else shutil.which("rocm-smi")


def vendor() -> str | None:
    """"amd" if rocm-smi exists, else "nvidia" if nvidia-smi is on PATH, else None."""
    if _rocm_smi():
        return "amd"
    if shutil.which("nvidia-smi"):   # untested
        return "nvidia"
    return None


def vram_used_gb() -> dict[int, float]:
    """VRAM in use per GPU (GB, 1e9 bytes): card N = GPU N (AMD), index N (NVIDIA)."""
    if vendor() == "nvidia":   # untested
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
        out = {}
        for line in r.stdout.splitlines():
            idx, _, mib = line.partition(",")
            if idx.strip().isdigit() and mib.strip().isdigit():
                out[int(idx)] = int(mib) * 1048576 / 1e9   # nvidia-smi's "MiB"
        return out
    r = subprocess.run([_rocm_smi() or ROCM_SMI, "--showmeminfo", "vram", "--json"], capture_output=True, text=True,
                       timeout=30)
    d = json.loads(r.stdout)
    return {int(k[4:]): int(v["VRAM Total Used Memory (B)"]) / 1e9 for k, v in d.items() if k.startswith("card")}


def visible_devices_env(gpu: int) -> dict[str, str]:
    """Environment entries that pin a process to one GPU (HIP for AMD, CUDA for NVIDIA, both if unknown)."""
    v = vendor()
    if v == "amd":
        return {"HIP_VISIBLE_DEVICES": str(gpu)}
    if v == "nvidia":   # untested
        return {"CUDA_VISIBLE_DEVICES": str(gpu)}
    return {"HIP_VISIBLE_DEVICES": str(gpu), "CUDA_VISIBLE_DEVICES": str(gpu)}
