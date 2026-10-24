"""Hardware profiles: which PyTorch build to install, which GPU each job lane uses, how GPU jobs are
scheduled, and what each feature needs in VRAM (from the model manifest).

    rocm-2gpu  Ubuntu + ROCm, 2 GPUs   the tested setup: TRELLIS and the vision model on one GPU,
                                       ComfyUI on the other
    rocm-1gpu  Ubuntu + ROCm, 1 GPU    one GPU for everything: ComfyUI, TRELLIS and the vision model
                                       take turns (one shared slot; the other's memory is freed first)
    nvidia     Ubuntu + NVIDIA         UNTESTED: CUDA wheels; lanes as above by GPU count

[hardware] profile in the user config picks one ("" = detect). The lane GPUs themselves stay
[jobs] gpu0_gpu / comfy_gpu (Setup writes the profile's defaults there); scheduling shares one slot
whenever both lanes are the same GPU, whatever the profile says.
"""
from dataclasses import asdict, dataclass

from . import config, gpu


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    vendor: str              # amd | nvidia
    gpus: int                # GPUs it plans for (nvidia: 0 = by count)
    torch: str               # AP_TORCH for model venvs (scripts/torch_index.sh)
    expandable_segments: bool  # PyTorch's expandable allocator for GPU jobs
    tested: bool
    note: str


PROFILES = {p.id: p for p in (
    Profile("rocm-2gpu", "Ubuntu + ROCm, 2 GPUs", "amd", 2, "rocm7.2", False, True,
            "The tested setup. TRELLIS and the vision model share one GPU, ComfyUI has the other."),
    Profile("rocm-1gpu", "Ubuntu + ROCm, 1 GPU", "amd", 1, "rocm7.2", False, False,
            "One GPU for everything: ComfyUI, TRELLIS and the vision model run one at a time. Scheduling "
            "is tested by simulation; not run on a 1-GPU machine yet."),
    Profile("nvidia", "Ubuntu + NVIDIA (untested)", "nvidia", 0, "cu128", True, False,
            "UNTESTED: the CUDA branches exist but have never run. Expect to debug them."),
)}

# Why the ROCm profiles keep PyTorch's default allocator (docs/notes/machine-and-gpu.md)
ROCM_ALLOCATOR_NOTE = ("expandable_segments stays off on ROCm: on gfx1201 it produced silent NaNs, and "
                       "scripts/trellis2_image_to_glb.py refuses to run with it")


def detect() -> dict:
    """{vendor, count, gpus, profile}: what this machine has and the profile that fits it."""
    vendor = gpu.vendor()
    gpus = gpu.list_gpus()
    n = len(gpus)
    pid = None
    if vendor == "amd" and n:
        pid = "rocm-2gpu" if n >= 2 else "rocm-1gpu"
    elif vendor == "nvidia" and n:
        pid = "nvidia"
    return {"vendor": vendor, "count": n, "gpus": gpus, "profile": pid}


def current_id() -> str | None:
    return str(config.get("hardware.profile") or "") or detect()["profile"]


def current() -> Profile | None:
    pid = current_id()
    return PROFILES.get(pid) if pid else None


def lanes(profile_id: str, count: int) -> dict[str, int]:
    """The profile's default GPU per lane: two GPUs split, one GPU shared."""
    p = PROFILES[profile_id]
    two = (p.gpus or count) >= 2 and count >= 2
    return {"gpu0": 0, "comfy": 1 if two else 0}


def torch_tag() -> str:
    """AP_TORCH for model venvs: [hardware] torch, else the profile's (rocm7.2 without a profile)."""
    if config.get("hardware.torch"):
        return str(config.get("hardware.torch"))
    p = current()
    return p.torch if p else "rocm7.2"


def allocator_env() -> dict[str, str]:
    """Environment for GPU job processes: expandable segments only where the profile allows it."""
    pid = str(config.get("hardware.profile") or "")           # cheap: no GPU tool runs per job
    p = PROFILES.get(pid) or (PROFILES["nvidia"] if not pid and gpu.vendor() == "nvidia" else None)
    if p and p.expandable_segments and p.vendor == "nvidia":       # untested
        return {"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
    return {}


def shared(cfg: dict | None = None) -> bool:
    """One GPU for every lane: ComfyUI, TRELLIS and the vision model take turns."""
    from . import gpu_policy
    g = (cfg or gpu_policy.settings())["gpus"]
    return g.get("gpu0") == g.get("comfy")


# --- VRAM per feature ---------------------------------------------------------------------------

def floors() -> dict[str, float]:
    """Free VRAM each GPU feature needs, from the manifest: the largest model it runs at once (the
    vision model: the configured one)."""
    from . import models
    out: dict[str, float] = {}
    for m in models.load():
        if m.need == "experimental" or m.vram_gb <= 0:
            continue
        if m.need == "local.model" and not models.required(m):
            continue
        out[m.feature] = max(out.get(m.feature, 0.0), float(m.vram_gb))
    return out


def vlm_options() -> list[dict]:
    """The vision models the config can pick, with what each needs: [{select, id, vram_gb, size}]."""
    from . import models
    return sorted(({"select": m.select, "id": m.id, "vram_gb": m.vram_gb, "size": m.size}
                   for m in models.load() if m.need == "local.model"), key=lambda o: -o["vram_gb"])


def smaller_vlm(have_gb: float) -> dict | None:
    """The largest vision model that fits in have_gb, or None."""
    return next((o for o in vlm_options() if o["vram_gb"] <= have_gb), None)


FEATURE_LANE = {"comfy": "comfy", "vlm": "gpu0", "trellis": "gpu0", "segdepth": "gpu0"}


def fit(gpus: list[dict] | None = None, cfg: dict | None = None) -> dict[str, dict]:
    """{feature: {ok, need, have, gpu, why}}: does each GPU feature fit on its lane's GPU (total VRAM)?"""
    from . import gpu_policy
    gpus = gpu.list_gpus() if gpus is None else gpus
    size = {g["index"]: g["vram_gb"] for g in gpus}
    lane_gpu = (cfg or gpu_policy.settings())["gpus"]
    out = {}
    for feat, need in floors().items():
        idx = lane_gpu.get(FEATURE_LANE.get(feat, "gpu0"))
        have = size.get(idx)
        if have is None or have <= 0:
            out[feat] = {"ok": True, "need": need, "have": None, "gpu": idx, "why": "VRAM unknown"}
            continue
        ok = have >= need
        why = "" if ok else f"GPU {idx} has {have:.0f} GB; this needs about {need:.0f} GB"
        if not ok and feat == "vlm":
            alt = smaller_vlm(have)
            why += (f". A smaller vision model fits: ap config set local.model {alt['select']} "
                    f"(then ap models download {alt['id']})" if alt else ". No vision model fits this GPU")
        out[feat] = {"ok": ok, "need": need, "have": have, "gpu": idx, "why": why}
    return out


def summary() -> dict:
    p = current()
    d = detect()
    return {"profile": asdict(p) if p else None, "configured": str(config.get("hardware.profile") or ""),
            "detected": d["profile"], "vendor": d["vendor"], "count": d["count"], "shared": shared(),
            "torch": torch_tag(), "allocator": allocator_env() or None,
            "allocator_note": "" if allocator_env() else ROCM_ALLOCATOR_NOTE}
