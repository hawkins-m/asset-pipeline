"""Background job queue for the UI: lanes with a fixed number of slots, FIFO, cancellable jobs.

Lanes (what runs where; RUNBOOK.md "What runs where"):
    "gpu0"     GPU 0: TRELLIS and the local vision model (VLM). One job at a time.
    "comfy"    GPU 1: ComfyUI (scenes, derive, sheets, frames, SAM). One job at a time.
    "cpu"      Blender, planning, export, Unreal commandlets: up to `cpu_slots` at once
               ([jobs] cpu_slots in config/backends.toml).
    "service"  starting and stopping ComfyUI. One at a time.
A job started on a busy lane waits in that lane's queue (`position` in public()) and never
runs alongside the job holding the slot. Each job runs in its own thread once it has a slot.

A running job can move to another lane (`hop`: Brief & build takes GPU 0 for the VLM brief,
then hops to the CPU for Blender, freeing GPU 0 at once) or borrow one for a section (`using`:
every local VLM call takes GPU 0 for the request, whatever lane the job is in).

Jobs that would write the same files never run together, whatever their lanes: `exclusive`
gives them a key (the project's greybox and site, its Unreal project, one library asset).

A GPU slot is taken only when the GPU has the VRAM the job needs: the queue's `policy`
(gpu_policy.GpuPolicy in the app) checks free VRAM before the job starts and keeps it queued
until there is enough, and unloads the VLM after a VLM job unless another one is waiting.

With one GPU (hardware profile rocm-1gpu: both GPU lanes on the same GPU) the "comfy" and "gpu0"
lanes share one slot (`shared`): ComfyUI, TRELLIS and the vision model run one at a time, in
submit order, and the policy frees the other's memory before a job starts. Jobs keep their lane
names; a job holding either lane holds the GPU.

A lane can have a gate (comfy.service.Gate for "comfy"): submit() asks it first (it raises
when the lane's server is down and nothing is starting it), and the job waits on it before
running. Jobs with project "" (the service jobs) show in every project's list.

Cancelling a queued job just drops it. Cancelling a running one sets its event and runs
the cancellers the job registered while running (ComfyClient registers one per prompt it
queues: delete it if pending, interrupt it if running; TRELLIS registers one that kills
its process group and releases the GPU claim). Code under a job calls check_canceled()
between steps; the job is recorded as "canceled".

Jobs live in memory; their effects (images, review.json, runs.jsonl) are on disk, so a
restart loses only the status of in-flight jobs.
"""
import contextlib
import contextvars
import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


class Canceled(Exception):
    """The job was canceled from the UI."""


class EstimateOnly(Exception):
    """Raised by submit() while estimate_only is set: the endpoint's job, not started (the UI
    asks an endpoint what it would run, with the endpoint's own checks and item counts)."""

    def __init__(self, kind: str, project: str, lane: str, tag: dict):
        super().__init__(kind)
        self.kind, self.project, self.lane, self.tag = kind, project, lane, tag


estimate_only: contextvars.ContextVar[bool] = contextvars.ContextVar("estimate_only", default=False)


_local = threading.local()

LANE_SLOTS = {"comfy": 1, "gpu0": 1, "cpu": 2, "service": 1}
VLM_KINDS = frozenset({"plan.analyze", "style.draft", "library.guide", "library.brief", "library.brief_build", "library.reroll",
                       "library.pending", "vlm.up"})


def exclusive(kind: str, project: str, tag: dict | None) -> str | None:
    """Jobs with the same key never run at the same time (they write the same files)."""
    tag = tag or {}
    head = kind.split(".", 1)[0]
    if head in ("site", "shots", "terrain", "demo") or kind == "library.plan":
        return f"{project}:site"
    if head == "ue":
        return f"{project}:ue"
    if head in ("library", "3d", "cleanup", "views") and tag.get("asset"):
        return f"{project}:asset:{tag['asset']}"
    if kind in ("library.sheets", "library.build", "library.trellis", "library.instances"):
        return f"{project}:library-batch"
    return None


@dataclass
class Job:
    id: int
    kind: str
    project: str
    lane: str = "comfy"
    tag: dict = field(default_factory=dict)  # what the job is for (unit, asset...), for the UI
    status: str = "queued"          # queued | running | done | error | canceled
    result: Any = None
    error: str | None = None
    note: str = ""                  # what a running job is doing right now (startup log line, waiting)
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    key: str | None = None          # exclusive(): jobs with one key never run together
    holds: list = field(default_factory=list)     # lanes whose slot the job holds now
    waiting: dict | None = None     # {lane, position, reason}: waiting for a slot or for VRAM
    used_vlm: bool = False          # called the local VLM (unloaded after the job: gpu_policy)
    gated: bool = True              # needs its lane's server (False: a hosted backend in "comfy")
    est_s: float | None = None      # its estimated run time (estimates.py), for the time left
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _cancellers: list = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _queue: Any = field(default=None, repr=False)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "project": self.project, "lane": self.lane,
                "tag": self.tag, "status": self.status, "result": self.result,
                "error": self.error, "note": self.note, "created": self.created, "started": self.started,
                "finished": self.finished, "holds": list(self.holds), "est_s": self.est_s,
                "waiting": dict(self.waiting) if self.waiting else None}

    def on_cancel(self, fn: Callable[[], None]) -> Callable[[], None]:
        """Register fn to run if the job is canceled; returns a function that unregisters
        it (call it once the resource is gone). Runs fn at once if already canceled."""
        with self._lock:
            if not self.cancel_event.is_set():
                self._cancellers.append(fn)
                return lambda: self._discard(fn)
        fn()
        return lambda: None

    def _discard(self, fn) -> None:
        with self._lock:
            if fn in self._cancellers:
                self._cancellers.remove(fn)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")


def current_job() -> Job | None:
    """The job running in this thread (None outside the UI's jobs, e.g. the CLI)."""
    return getattr(_local, "job", None)


def check_canceled() -> None:
    job = current_job()
    if job and job.cancel_event.is_set():
        raise Canceled(f"job {job.id} canceled")


def hop(lane: str) -> None:
    """The running job moves to another lane: its slot is freed (the policy's after-hook runs:
    the VLM goes unless another VLM job waits), then it queues for the new lane. No-op
    outside a job."""
    job = current_job()
    if job is not None and job._queue is not None and job.lane != lane:
        job._queue._hop(job, lane)


@contextlib.contextmanager
def using(lane: str, purpose: str = "gpu"):
    """Hold a lane's slot for a section (the local VLM borrows GPU 0 for each request). No-op
    outside a job and when the job holds the lane already."""
    job = current_job()
    if purpose == "vlm" and job is not None:
        job.used_vlm = True
    if job is None or job._queue is None or lane in job.holds:
        yield
        return
    q = job._queue
    if any(h in q.group(lane) for h in job.holds):     # one shared GPU: the job holds it already
        q._check_vram(job, lane, purpose)
        yield
        return
    q._acquire(job, lane, purpose)
    try:
        yield
    finally:
        q._release(job, lane, after=False)


class JobQueue:
    LANES = ("comfy", "gpu0", "cpu", "service")

    GPU_LANES = ("comfy", "gpu0")

    def __init__(self, lanes: tuple[str, ...] = LANES, gates: dict | None = None, error_text=None,
                 slots: dict | None = None, policy=None, estimator=None, recorder=None, shared: bool = False):
        self.gates = dict(gates or {})    # lane -> object with check_submit() and wait(job)
        self.error_text = error_text      # job error -> the text the UI shows (ui.messages.app_text)
        self.policy = policy              # before(job, lane, purpose) / after(job, lane): VRAM, VLM unload
        self.estimator = estimator        # job -> its estimated seconds (estimates.job_seconds), or None
        self.recorder = recorder          # job -> None: a finished job's run time (estimates.record)
        self.slots = {lane: max(1, int((slots or {}).get(lane, LANE_SLOTS.get(lane, 1)))) for lane in lanes}
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._cv = threading.Condition()
        self._holders: dict[str, list[Job]] = {lane: [] for lane in lanes}
        self._waiting: dict[str, list[Job]] = {lane: [] for lane in lanes}
        # one GPU: the GPU lanes share one slot (ComfyUI, TRELLIS and the vision model take turns)
        self.shared = bool(shared) and all(lane in self.slots for lane in self.GPU_LANES)

    def group(self, lane: str) -> tuple[str, ...]:
        """The lanes sharing `lane`'s slots: both GPU lanes on one GPU, else the lane alone."""
        return self.GPU_LANES if self.shared and lane in self.GPU_LANES else (lane,)

    def _line(self, lane: str) -> list[Job]:
        """Everyone waiting for `lane`'s slots, in submit order across a shared group."""
        g = self.group(lane)
        if len(g) == 1:
            return self._waiting[lane]
        return sorted((o for ln in g for o in self._waiting[ln]), key=lambda o: o.id)

    def submit(self, kind: str, project: str, fn: Callable[[], Any], lane: str = "comfy",
               tag: dict | None = None, gated: bool = True) -> Job:
        """gated=False: the job runs in a gated lane without needing its server (e.g. an image
        stage on a hosted backend in the "comfy" lane)."""
        if lane not in self.slots:
            raise ValueError(f"unknown lane {lane!r}")
        if estimate_only.get():
            raise EstimateOnly(kind, project, lane, dict(tag or {}))
        gate = self.gates.get(lane) if gated else None
        if gate is not None:
            gate.check_submit()
        with self._cv:
            job = Job(id=next(self._ids), kind=kind, project=project, lane=lane, tag=tag or {},
                      key=exclusive(kind, project, tag), gated=gated, _queue=self)
            if self.estimator is not None:
                try:
                    job.est_s = round(self.estimator(job))
                except Exception:  # noqa: BLE001 - an estimate never stops a job
                    job.est_s = None
            self._jobs[job.id] = job
            self._waiting[lane].append(job)       # in line from now: positions follow submit order
            self._position(job, lane, "slot")
        threading.Thread(target=self._run, args=(job, fn, gate), daemon=True, name=f"ap-job-{job.id}").start()
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, project: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values() if project is None or j.project in (project, "")]

    def active(self) -> "list[Job]":
        return [j for j in self._jobs.values() if j.active]

    def cancel(self, job_id: int) -> Job | None:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return job
        with job._lock:
            job.cancel_event.set()
            cancellers, job._cancellers = list(job._cancellers), []
            if job.status == "queued":   # its thread drops it
                job.status, job.finished = "canceled", time.time()
        with self._cv:
            self._cv.notify_all()
        for fn in cancellers:            # outside the lock: these do HTTP / kill processes
            try:
                fn()
            except Exception:
                traceback.print_exc()
        return job

    # --- slots --------------------------------------------------------------------------------

    def purpose(self, job: Job, lane: str) -> str:
        """What a job does on a lane, for the VRAM check: "vlm", "gpu" (TRELLIS and other GPU 0
        work), "comfy" (ComfyUI on its GPU, unless the job runs on a hosted backend), "none"."""
        if lane == "gpu0":
            return "none" if job.kind == "vlm.down" else "vlm" if job.kind in VLM_KINDS else "gpu"
        if lane == "comfy":
            return "comfy" if job.gated else "none"
        return "none"

    def _position(self, job: Job, lane: str, reason: str) -> None:
        q = self._line(lane)
        job.waiting = {"lane": lane, "position": q.index(job) + 1 if job in q else 1, "reason": reason}

    def _blocked(self, job: Job) -> bool:
        """A job not started yet whose exclusive key a started one holds (a job keeps its key
        while it hops between lanes)."""
        return job.key is not None and job.status == "queued" and not job.holds and any(
            o is not job and o.key == job.key and (o.holds or o.status == "running") for o in self._jobs.values())

    def _may_take(self, job: Job, lane: str) -> bool:
        free = self.slots[lane] - sum(len(self._holders[ln]) for ln in self.group(lane))
        if free <= 0:
            return False
        ahead = [o for o in self._line(lane) if not self._blocked(o)]
        return job in ahead[:free]

    def _acquire(self, job: Job, lane: str, purpose: str | None = None) -> None:
        """Wait in the lane's line for a slot, then (GPU lanes) for the VRAM the job needs."""
        with self._cv:
            if job not in self._waiting[lane]:
                self._waiting[lane].append(job)
            try:
                while True:
                    if job.cancel_event.is_set():
                        raise Canceled(f"job {job.id} canceled")
                    if self._may_take(job, lane):
                        break
                    for o in self._line(lane):
                        self._position(o, o.waiting["lane"] if o.waiting else lane, "slot")
                    self._cv.wait(0.5)
            finally:
                self._waiting[lane].remove(job)
                for o in self._line(lane):
                    self._position(o, o.waiting["lane"] if o.waiting else lane, "slot")
            self._holders[lane].append(job)
            job.holds.append(lane)
            job.waiting = {"lane": lane, "position": 1, "reason": "vram"} if self.policy else None
        if self.policy is not None:
            try:
                self.policy.before(job, lane, purpose or self.purpose(job, lane))
            except BaseException:
                self._release(job, lane, after=False)
                raise
        job.waiting = None

    def _check_vram(self, job: Job, lane: str, purpose: str | None = None) -> None:
        """The VRAM check (and freeing the other model) without taking a slot: a job on one
        shared GPU borrowing the other lane's work (a ComfyUI job calling the vision model)."""
        if self.policy is not None:
            self.policy.before(job, lane, purpose or self.purpose(job, lane))

    def _release(self, job: Job, lane: str, after: bool = True) -> None:
        if after and self.policy is not None:
            try:
                self.policy.after(job, lane)
            except Exception:
                traceback.print_exc()
        with self._cv:
            if job in self._holders[lane]:
                self._holders[lane].remove(job)
            if lane in job.holds:
                job.holds.remove(lane)
            self._cv.notify_all()

    def _hop(self, job: Job, lane: str) -> None:
        if lane not in self.slots:
            raise ValueError(f"unknown lane {lane!r}")
        old = job.lane
        self._release(job, old)
        job.lane = lane
        self._acquire(job, lane)

    def vlm_wanted(self, besides: Job | None = None) -> bool:
        """Another unfinished job that uses (or will use) the local VLM."""
        return any(j is not besides and j.active and (j.kind in VLM_KINDS or j.used_vlm)
                   for j in self._jobs.values())

    def _run(self, job: Job, fn, gate) -> None:
        _local.job = job
        try:
            self._acquire(job, job.lane)
            with job._lock:
                if job.cancel_event.is_set():
                    raise Canceled(f"job {job.id} canceled")
                job.status, job.started = "running", time.time()
            if gate is not None:
                gate.wait(job)
            result = fn()
            if job.cancel_event.is_set():  # finished anyway: keep the result, say so
                status = "canceled"
            else:
                status = "done"
            job.result = result
        except Canceled:
            status = "canceled"
        except Exception as e:  # surfaced to the UI, never swallowed
            if job.cancel_event.is_set():
                status = "canceled"  # e.g. ComfyUI reporting the interrupt
            else:
                status = "error"
                # errors written for people (plain = True) show as they are; the traceback goes to the log
                job.error = str(e) if getattr(e, "plain", False) else f"{type(e).__name__}: {e}"
                if isinstance(e, OSError) and getattr(e, "filename", None):     # which file, and why
                    from .filestore import write_error_text
                    from .config import ap_root
                    job.error = write_error_text(e, ap_root() / "projects")
                if self.error_text:
                    job.error = self.error_text(job.error)
                traceback.print_exc()
        finally:
            # the VLM goes after a job that used it, unless another VLM job waits; decided while
            # holding GPU 0, so no VLM request of ours is in flight. A job that only borrowed GPU 0
            # takes it back only when nobody holds or waits for it (whoever does decides instead:
            # a VLM job keeps the model, any other GPU 0 job unloads it before it starts).
            if self.policy is not None and job.started and (job.used_vlm or job.kind in VLM_KINDS) \
                    and "gpu0" in self.slots and ("gpu0" in job.holds or self._take_idle(job, "gpu0")):
                self._release(job, "gpu0")
            for lane in list(job.holds):
                self._release(job, lane, after=False)
            _local.job = None
        with self._cv:
            job.waiting = None
            job.status = status
            job.finished = job.finished if status == "canceled" and job.finished else time.time()
            self._cv.notify_all()
        if self.recorder is not None and status == "done":
            try:
                self.recorder(job)
            except Exception:  # noqa: BLE001
                traceback.print_exc()

    def _take_idle(self, job: Job, lane: str) -> bool:
        with self._cv:
            if any([o for o in self._holders[ln] if o is not job] or self._waiting[ln] for ln in self.group(lane)):
                return False
            self._holders[lane].append(job)
            job.holds.append(lane)
            return True
