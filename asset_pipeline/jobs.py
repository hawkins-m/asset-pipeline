"""Background job queue for the UI: one worker per GPU lane, cancellable jobs.

Lanes: "comfy" (ComfyUI, GPU 1: scenes, derive, sheets, SAM) and "gpu0" (the local VLM
and TRELLIS, GPU 0). Each lane runs one job at a time; the two run side by side.

Cancelling a queued job just drops it. Cancelling a running one sets its event and runs
the cancellers the job registered while running (ComfyClient registers one per prompt it
queues: delete it if pending, interrupt it if running; TRELLIS registers one that kills
its process group and releases the GPU claim). Code under a job calls check_canceled()
between steps; the worker records the job as "canceled".

Jobs live in memory; their effects (images, review.json, runs.jsonl) are on disk, so a
restart loses only the status of in-flight jobs.
"""
import itertools
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable


class Canceled(Exception):
    """The job was canceled from the UI."""


_local = threading.local()


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
    created: float = field(default_factory=time.time)
    finished: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _cancellers: list = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "project": self.project, "lane": self.lane,
                "tag": self.tag, "status": self.status, "result": self.result,
                "error": self.error, "created": self.created, "finished": self.finished}

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


def current_job() -> Job | None:
    """The job running in this thread (None outside the UI's workers, e.g. the CLI)."""
    return getattr(_local, "job", None)


def check_canceled() -> None:
    job = current_job()
    if job and job.cancel_event.is_set():
        raise Canceled(f"job {job.id} canceled")


class JobQueue:
    LANES = ("comfy", "gpu0")

    def __init__(self, lanes: tuple[str, ...] = LANES):
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._queues: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        for lane in lanes:
            self._queues[lane] = queue.Queue()
            threading.Thread(target=self._worker, args=(lane,), daemon=True, name=f"ap-jobs-{lane}").start()

    def submit(self, kind: str, project: str, fn: Callable[[], Any], lane: str = "comfy",
               tag: dict | None = None) -> Job:
        if lane not in self._queues:
            raise ValueError(f"unknown lane {lane!r}")
        with self._lock:
            job = Job(id=next(self._ids), kind=kind, project=project, lane=lane, tag=tag or {})
            self._jobs[job.id] = job
        self._queues[lane].put((job, fn))
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, project: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values() if project is None or j.project == project]

    def cancel(self, job_id: int) -> Job | None:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return job
        with job._lock:
            job.cancel_event.set()
            cancellers, job._cancellers = list(job._cancellers), []
            if job.status == "queued":   # the worker skips it
                job.status, job.finished = "canceled", time.time()
        for fn in cancellers:            # outside the lock: these do HTTP / kill processes
            try:
                fn()
            except Exception:
                traceback.print_exc()
        return job

    def _worker(self, lane: str) -> None:
        q = self._queues[lane]
        while True:
            job, fn = q.get()
            with job._lock:
                if job.cancel_event.is_set():
                    continue
                job.status = "running"
            _local.job = job
            try:
                result = fn()
                if job.cancel_event.is_set():  # finished anyway: keep the result, say so
                    job.status, job.result = "canceled", result
                else:
                    job.status, job.result = "done", result
            except Canceled:
                job.status = "canceled"
            except Exception as e:  # surfaced to the UI, never swallowed
                if job.cancel_event.is_set():
                    job.status = "canceled"  # e.g. ComfyUI reporting the interrupt
                else:
                    job.status = "error"
                    job.error = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
            finally:
                _local.job = None
            job.finished = time.time()
