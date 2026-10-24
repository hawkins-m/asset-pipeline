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

What a job needs comes from the model manifest (config/models.toml vram_gb: TRELLIS.2, the
configured vision model); [jobs.vram_gb] in the config overrides it. With one GPU for every lane
(hardware profile rocm-1gpu) the job first frees what the other side holds: ComfyUI's models
(POST /free) before TRELLIS or the vision model, the vision model before a ComfyUI job. Memory
held by processes the app didn't start is never touched: they are named with their PID.
"""
import os
import time
from pathlib import Path

from . import config
from . import gpu as gpu_mod
from .jobs import VLM_KINDS, Canceled

DEFAULT_NEED_GB = {"gpu": 16.0, "comfy": 12.0, "vlm": 28.0}


def manifest_need() -> dict[str, float]:
    """VRAM per purpose from the model manifest: "gpu" = TRELLIS.2."""
    try:
        from . import models
        return {"gpu": max(float(m.vram_gb) for m in models.load() if m.feature == "trellis")}
    except Exception:  # noqa: BLE001 - a broken manifest never stops the queue
        return {}


def settings() -> dict:
    """[jobs] in config/backends.toml: cpu_slots, gpu per lane, vram_gb per purpose (the manifest's,
    unless [jobs.vram_gb] sets one), whether both GPU lanes share one GPU."""
    j = config.backends().get("jobs", {}) or {}
    comfy_gpu = str((config.backends().get("comfyui", {}).get("service") or {}).get("gpu", "")).strip()
    gpus = {"gpu0": int(j.get("gpu0_gpu", 0)), "comfy": int(comfy_gpu) if comfy_gpu.isdigit() else int(j.get("comfy_gpu", 1))}
    return {"cpu_slots": int(j.get("cpu_slots", 2)), "gpus": gpus, "shared": gpus["gpu0"] == gpus["comfy"],
            "vram_gb": DEFAULT_NEED_GB | manifest_need() | {k: float(v) for k, v in (j.get("vram_gb") or {}).items()},
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
        to it when the default doesn't fit. From the manifest; [local.models.*] vram_gb overrides."""
        loc = config.backends().get("local", {}) or {}
        conf = loc.get("models", {}) or {}
        try:
            from . import models
            man = {m.select: float(m.vram_gb) for m in models.load() if m.need == "local.model"}
        except Exception:  # noqa: BLE001
            man = {}
        sizes = [float(conf.get(m, {}).get("vram_gb", 0) or man.get(m, 0)) for m in (loc.get("model", "32b"), loc.get("fallback", "8b"))]
        sizes = [x for x in sizes if x > 0]
        return min(sizes) if sizes else self.cfg["vram_gb"]["vlm"]

    def need(self, purpose: str) -> float:
        return self.vlm_need() if purpose == "vlm" else self.cfg["vram_gb"][purpose]

    def _free_comfy(self, job, gpu: int) -> None:
        """One shared GPU: ask ComfyUI (if it answers) to unload its models before TRELLIS / the VLM."""
        try:
            from .comfy.client import ComfyClient
            from .comfy import service
            c = ComfyClient(service.settings()["url"], timeout_s=10)
            job.note = f"Freeing ComfyUI's models on GPU {gpu}…"
            c.free_memory()
            c.http.close()
            self.sleep(2.0)                               # its allocator hands the memory back
        except Exception:  # noqa: BLE001 - not running, or an old ComfyUI: nothing to free
            pass

    def before(self, job, lane: str, purpose: str) -> None:
        gpu = self.gpu_of(lane)
        if purpose == "none" or gpu is None:
            return
        besides: set[int] = set()
        shared = self.cfg.get("shared") or self.gpu_of("gpu0") == self.gpu_of("comfy")
        need = self.need(purpose)
        if purpose == "gpu":
            st = self.vlm.state()
            if st and st["gpu"] == gpu:
                job.note = "Unloading the vision model from GPU %d…" % gpu
                self.vlm.down(gpu=gpu, log=lambda s: None)
        elif purpose == "vlm":
            st = self.vlm.state()
            besides = _group(st["pid"]) if st and st["gpu"] == gpu else set()
        else:                                             # comfy
            besides = _group(self.comfy_pid_fn())
        freed = False
        while True:
            free = self.free_fn(gpu, besides)
            if shared and not freed and free is not None and free < need:
                freed = True                              # once per job: free the other side, then wait
                if purpose in ("gpu", "vlm"):
                    self._free_comfy(job, gpu)
                else:
                    st = self.vlm.state()
                    if st and st["gpu"] == gpu and not (job._queue and job._queue.vlm_wanted(besides=job)):
                        job.note = "Unloading the vision model from GPU %d…" % gpu
                        self.vlm.down(gpu=gpu, log=lambda s: None)
                continue
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
        return "; held by " + ", ".join(f"{_name(p)} (PID {p}) {v:.1f} GB" for p, v in top)

    def report(self, gpu: int, besides: set[int] | None = None) -> str:
        """What holds memory on the GPU, in words (for an out-of-memory message)."""
        h = self._holders(gpu, besides or set())
        return ("Held by " + h.removeprefix("; held by ")) if h else "Nothing else of note holds it"

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


# --- out of memory ------------------------------------------------------------------------------

OOM_PATTERNS = ("out of memory", "outofmemoryerror", "hiperroroutofmemory", "erroroutofdevicememory",
                "cudaerrormemoryallocation", "failed to allocate")


def is_oom(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in OOM_PATTERNS)


class GpuMemoryError(RuntimeError):
    """Not enough GPU memory after freeing and one retry. The message is for people (no traceback):
    the job panel and the error dialog show it as it is."""
    plain = True


def _policy_for(job) -> "GpuPolicy":
    q = getattr(job, "_queue", None) if job is not None else None
    return q.policy if q is not None and q.policy is not None else GpuPolicy()


def free_for_retry(lane: str, purpose: str, job=None, wait_s: float = 60.0) -> tuple[float, float | None, str]:
    """After an out-of-memory failure: free what the app can on the lane's GPU (the vision model
    for TRELLIS; on one shared GPU ComfyUI's models too), wait up to wait_s for the need. Returns
    (need GB, free GB, what holds the GPU in words). Never touches processes the app didn't start."""
    pol = _policy_for(job)
    gpu = pol.gpu_of(lane)
    need = pol.need(purpose)
    if gpu is None:
        return need, None, ""
    st = pol.vlm.state()
    if purpose == "gpu" and st and st["gpu"] == gpu:
        pol.vlm.down(gpu=gpu, log=lambda s: None)
    if pol.cfg.get("shared") or pol.gpu_of("gpu0") == pol.gpu_of("comfy"):
        class _Note:                                      # _free_comfy writes a note; outside a job nobody reads it
            note = ""
        pol._free_comfy(job or _Note(), gpu)
    end = time.monotonic() + wait_s
    free = pol.free_fn(gpu, set())
    while free is not None and free < need and time.monotonic() < end:
        if job is not None and job.cancel_event.is_set():
            raise Canceled("canceled")
        pol.sleep(min(pol.cfg["poll_s"], 1.0))
        free = pol.free_fn(gpu, set())
    return need, free, pol.report(gpu)


def oom_message(need: float, free: float | None, holders: str, lighter: str) -> str:
    have = f"{free:.1f} GB free" if free is not None else "free memory unknown"
    return f"Not enough GPU memory (needs ~{need:.0f} GB, {have}). {holders}. Try again, or {lighter}."
