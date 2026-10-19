"""GPU vendor differences in one place: which tool reports VRAM, which env var pins a GPU.

AMD (ROCm) is the tested path. The NVIDIA branches are untested (no NVIDIA hardware here).
"""
import json
import re
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


def vram_total_gb() -> dict[int, float]:
    """VRAM size per GPU (GB)."""
    if vendor() == "nvidia":   # untested
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=30)
        return {int(i): int(m) * 1048576 / 1e9 for i, _, m in
                (ln.partition(",") for ln in r.stdout.splitlines()) if i.strip().isdigit() and m.strip().isdigit()}
    r = subprocess.run([_rocm_smi() or ROCM_SMI, "--showmeminfo", "vram", "--json"], capture_output=True, text=True,
                       timeout=30)
    d = json.loads(r.stdout)
    return {int(k[4:]): int(v["VRAM Total Memory (B)"]) / 1e9 for k, v in d.items() if k.startswith("card")}


def vram_by_process() -> dict[int, dict[int, float]]:
    """{gpu: {pid: GB}}: VRAM each process holds per GPU. AMD: rocm-smi --showpids (VRAM per
    process) and --showpidgpus (its devices); a process on several GPUs is split evenly between
    them. (amd-smi's per-process vram_mem read ~0 for a ComfyUI holding 3 GB, 2026-10-18.)"""
    out: dict[int, dict[int, float]] = {}
    if vendor() == "nvidia":   # untested
        idx = subprocess.run(["nvidia-smi", "--query-gpu=index,pci.bus_id", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=30).stdout
        bus = {b.strip(): int(i) for i, _, b in (ln.partition(",") for ln in idx.splitlines()) if i.strip().isdigit()}
        apps = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory,gpu_bus_id", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=30).stdout
        for ln in apps.splitlines():
            parts = [x.strip() for x in ln.split(",")]
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2] in bus:
                out.setdefault(bus[parts[2]], {})[int(parts[0])] = int(parts[1]) * 1048576 / 1e9
        return out
    smi = _rocm_smi() or ROCM_SMI
    r = subprocess.run([smi, "--showpids", "--json"], capture_output=True, text=True, timeout=30)
    try:
        procs = json.loads(r.stdout).get("system", {})
    except ValueError:
        return out
    devs = parse_pidgpus(subprocess.run([smi, "--showpidgpus"], capture_output=True, text=True, timeout=30).stdout)
    for k, v in procs.items():
        if not k.startswith("PID"):
            continue
        pid, fields = int(k[3:]), [x.strip() for x in str(v).split(",")]
        if len(fields) < 3 or not fields[2].isdigit():
            continue
        gpus = devs.get(pid) or []
        for g in gpus:
            out.setdefault(g, {})[pid] = int(fields[2]) / 1e9 / len(gpus)
    return out


def parse_pidgpus(text: str) -> dict[int, list[int]]:
    """rocm-smi --showpidgpus (text): "PID 123 is using 1 DRM device(s):" then a line of indices."""
    out, pid = {}, None
    for ln in text.splitlines():
        m = re.match(r"\s*PID (\d+) is using", ln)
        if m:
            pid = int(m.group(1))
            out[pid] = []
        elif pid is not None and re.fullmatch(r"[\d\s]+", ln.strip() or "x"):
            out[pid] = [int(x) for x in ln.split()]
            pid = None
    return out


def vram_free_gb(gpu: int, besides: set[int] | None = None) -> float | None:
    """Free VRAM on a GPU (GB), counting memory held by the `besides` PIDs as free (e.g. the
    ComfyUI that will run the job, or the VLM a VLM job reuses). None if it can't be read."""
    try:
        total, used = vram_total_gb().get(gpu), vram_used_gb().get(gpu)
        own = sum(v for p, v in vram_by_process().get(gpu, {}).items() if p in (besides or set())) if besides else 0.0
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None
    if total is None or used is None:
        return None
    return max(0.0, total - used + own)


def visible_devices_env(gpu: int) -> dict[str, str]:
    """Environment entries that pin a process to one GPU (HIP for AMD, CUDA for NVIDIA, both if unknown)."""
    v = vendor()
    if v == "amd":
        return {"HIP_VISIBLE_DEVICES": str(gpu)}
    if v == "nvidia":   # untested
        return {"CUDA_VISIBLE_DEVICES": str(gpu)}
    return {"HIP_VISIBLE_DEVICES": str(gpu), "CUDA_VISIBLE_DEVICES": str(gpu)}
