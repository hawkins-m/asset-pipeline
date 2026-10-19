"""VRAM checks and VLM unloading for the job queue's GPU lanes (jobs.JobQueue(policy=...)).

Before a job takes a GPU lane:
  - a GPU 0 job that isn't the vision model (TRELLIS) unloads the VLM if it's loaded there
    (no VLM request of ours is in flight: requests hold the GPU 0 slot too);
  - the job waits, still queued, until the GPU has the VRAM it needs ([jobs.vram_gb] in
    config/backends.toml; the VLM's from its [local.models.*] vram_gb). Memory held by the
    process the job will use counts as free: ComfyUI's for "comfy" jobs, the loaded VLM's for
    VLM jobs. What else holds the GPU (the user's Blender, an Unreal editor) is named in the note.
After a job that used the VLM (or when it hops off GPU 0, as Brief & build does), the VLM is
unloaded unless another VLM job is queued or running. `vlm.up` (Start in Vision LLM status)
keeps it loaded until the next VLM job ends, or until its idle unload.
"""
import os
import time
from pathlib import Path

from . import config
from . import gpu as gpu_mod
from .jobs import VLM_KINDS, Canceled

DEFAULT_NEED_GB = {"gpu": 16.0, "comfy": 12.0, "vlm": 28.0}


def settings() -> dict:
    """[jobs] in config/backends.toml: cpu_slots, gpu per lane, vram_gb per purpose."""
    j = config.backends().get("jobs", {}) or {}
    comfy_gpu = str((config.backends().get("comfyui", {}).get("service") or {}).get("gpu", "")).strip()
    return {"cpu_slots": int(j.get("cpu_slots", 2)),
            "gpus": {"gpu0": int(j.get("gpu0_gpu", 0)), "comfy": int(comfy_gpu) if comfy_gpu.isdigit() else int(j.get("comfy_gpu", 1))},
            "vram_gb": DEFAULT_NEED_GB | {k: float(v) for k, v in (j.get("vram_gb") or {}).items()},
            "poll_s": float(j.get("vram_poll_s", 3.0))}


def _group(pid: int | None) -> set[int]:
    """A process and everything in its process group (the VLM's launcher script and server)."""
    if not pid:
        return set()
    out = {pid}
    for d in Path("/proc").glob("[0-9]*"):
        try:
            if int(d.joinpath("stat").read_text().rsplit(")", 1)[1].split()[2]) == pid:
                out.add(int(d.name))
        except (OSError, ValueError, IndexError):
            continue
    return out


def _name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return f"pid {pid}"


class GpuPolicy:
    def __init__(self, free_fn=None, procs_fn=None, vlm_mod=None, comfy_pid_fn=None, cfg: dict | None = None,
                 sleep=time.sleep):
        self.cfg = cfg or settings()
        self.free_fn = free_fn or gpu_mod.vram_free_gb
        self.procs_fn = procs_fn or gpu_mod.vram_by_process
        self._vlm = vlm_mod
        self.comfy_pid_fn = comfy_pid_fn or self._comfy_pid
        self.sleep = sleep

    @property
    def vlm(self):
        if self._vlm is None:
            from . import vlm
            self._vlm = vlm
        return self._vlm

    @staticmethod
    def _comfy_pid() -> int | None:
        from .comfy import service
        return service.listener_pid(service.settings()["port"])

    def gpu_of(self, lane: str) -> int | None:
        return self.cfg["gpus"].get(lane)

    def vlm_need(self) -> float:
        """The smallest configured model that may serve (default, then fallback): vlm.up falls back
        to it when the default doesn't fit."""
        loc = config.backends().get("local", {}) or {}
        models = loc.get("models", {}) or {}
        sizes = [float(models.get(m, {}).get("vram_gb", 0) or 0) for m in (loc.get("model", "32b"), loc.get("fallback", "8b"))]
        sizes = [x for x in sizes if x > 0]
        return min(sizes) if sizes else self.cfg["vram_gb"]["vlm"]

    def before(self, job, lane: str, purpose: str) -> None:
        gpu = self.gpu_of(lane)
        if purpose == "none" or gpu is None:
            return
        besides: set[int] = set()
        if purpose == "gpu":
            st = self.vlm.state()
            if st and st["gpu"] == gpu:
                job.note = "Unloading the vision model from GPU %d…" % gpu
                self.vlm.down(gpu=gpu, log=lambda s: None)
            need = self.cfg["vram_gb"]["gpu"]
        elif purpose == "vlm":
            st = self.vlm.state()
            besides = _group(st["pid"]) if st and st["gpu"] == gpu else set()
            need = self.vlm_need()
        else:                                             # comfy
            besides = _group(self.comfy_pid_fn())
            need = self.cfg["vram_gb"]["comfy"]
        while True:
            free = self.free_fn(gpu, besides)
            if free is None or free >= need:             # unreadable (no rocm-smi): don't block
                if job.note.startswith(("Waiting for", "Unloading the vision")):
                    job.note = ""
                return
            if job.cancel_event.is_set():
                raise Canceled(f"job {job.id} canceled")
            job.note = f"Waiting for {need:.0f} GB of free VRAM on GPU {gpu} ({free:.1f} GB free{self._holders(gpu, besides)})"
            end = time.monotonic() + self.cfg["poll_s"]
            while time.monotonic() < end and not job.cancel_event.is_set():
                self.sleep(0.25)

    def _holders(self, gpu: int, besides: set[int]) -> str:
        try:
            procs = {p: v for p, v in self.procs_fn().get(gpu, {}).items() if p not in besides and v >= 0.5}
        except Exception:  # noqa: BLE001
            return ""
        if not procs:
            return ""
        top = sorted(procs.items(), key=lambda kv: -kv[1])[:3]
        return "; held by " + ", ".join(f"{_name(p)} {v:.1f} GB" for p, v in top)

    def after(self, job, lane: str) -> None:
        """A job leaves GPU 0: unload the VLM if this job used it and no other VLM job waits."""
        if lane != "gpu0" or job.kind == "vlm.up" or not (job.used_vlm or job.kind in VLM_KINDS):
            return
        q = job._queue
        if q is not None and q.vlm_wanted(besides=job):
            return
        gpu = self.gpu_of(lane)
        st = self.vlm.state()
        if st and st["gpu"] == gpu:
            job.note = "Unloading the vision model…"
            self.vlm.down(gpu=gpu, log=lambda s: None)
