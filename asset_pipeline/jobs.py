"""Single-worker background job queue for the UI (one GPU job at a time).

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


@dataclass
class Job:
    id: int
    kind: str
    project: str
    status: str = "queued"          # queued | running | done | error
    result: Any = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "project": self.project, "status": self.status,
                "result": self.result, "error": self.error, "created": self.created,
                "finished": self.finished}


class JobQueue:
    def __init__(self):
        self._ids = itertools.count(1)
        self._jobs: dict[int, Job] = {}
        self._q: queue.Queue[tuple[Job, Callable[[], Any]]] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True, name="ap-jobs").start()

    def submit(self, kind: str, project: str, fn: Callable[[], Any]) -> Job:
        with self._lock:
            job = Job(id=next(self._ids), kind=kind, project=project)
            self._jobs[job.id] = job
        self._q.put((job, fn))
        return job

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, project: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values() if project is None or j.project == project]

    def _worker(self) -> None:
        while True:
            job, fn = self._q.get()
            job.status = "running"
            try:
                job.result = fn()
                job.status = "done"
            except Exception as e:  # surfaced to the UI, never swallowed
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                traceback.print_exc()
            job.finished = time.time()
