"""Job lanes and cancellation (UI Stop buttons), ComfyUI prompt cancel. No GPU."""
import json
import os
import threading
import time

import httpx

from asset_pipeline import jobs
from asset_pipeline.comfy.client import ComfyClient
from asset_pipeline.jobs import JobQueue


def _wait(q, job, states=("done", "error", "canceled"), t=5):
    end = time.time() + t
    while q.get(job.id).status not in states and time.time() < end:
        time.sleep(0.01)
    return q.get(job.id).status


def test_lanes_run_side_by_side():
    q = JobQueue()
    gate = threading.Event()
    a = q.submit("comfy-thing", "p", lambda: gate.wait(5), lane="comfy")
    b = q.submit("gpu0-thing", "p", lambda: "ok", lane="gpu0")
    assert _wait(q, b) == "done"          # not stuck behind the comfy job
    gate.set()
    assert _wait(q, a) == "done"


def test_cancel_queued_job_never_runs():
    q = JobQueue()
    gate, ran = threading.Event(), []
    first = q.submit("k", "p", lambda: gate.wait(5))
    second = q.submit("k", "p", lambda: ran.append(1))
    assert q.cancel(second.id).status == "canceled"
    gate.set()
    _wait(q, first)
    time.sleep(0.05)
    assert ran == [] and q.get(second.id).status == "canceled"


def test_cancel_running_job_runs_its_cancellers_only():
    q = JobQueue()
    calls = []

    def work(name):
        job = jobs.current_job()
        job.on_cancel(lambda: calls.append(name))
        while True:
            jobs.check_canceled()
            time.sleep(0.01)

    a = q.submit("k", "p", lambda: work("a"), lane="comfy")
    b = q.submit("k", "p", lambda: work("b"), lane="gpu0")
    while q.get(a.id).status != "running" or q.get(b.id).status != "running":
        time.sleep(0.01)
    q.cancel(a.id)
    assert _wait(q, a) == "canceled" and calls == ["a"]
    assert q.get(b.id).status == "running"
    q.cancel(b.id)
    assert _wait(q, b) == "canceled" and calls == ["a", "b"]


def test_errors_after_cancel_count_as_canceled():
    q = JobQueue()

    def work():
        jobs.current_job().cancel_event.wait(5)
        raise RuntimeError("ComfyUI execution failed: interrupted")
    j = q.submit("k", "p", work)
    while q.get(j.id).status != "running":
        time.sleep(0.01)
    q.cancel(j.id)
    assert _wait(q, j) == "canceled" and q.get(j.id).error is None


def _comfy(queue_state, posts):
    def handler(request):
        if request.method == "GET" and request.url.path == "/queue":
            return httpx.Response(200, json=queue_state)
        if request.url.path == "/prompt":
            return httpx.Response(200, json={"prompt_id": "mine"})
        posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})
    return ComfyClient("http://comfy", transport=httpx.MockTransport(handler))


def test_cancel_prompt_deletes_pending_and_interrupts_only_own_running():
    posts = []
    c = _comfy({"queue_pending": [[1, "mine", {}, {}, []]], "queue_running": [[0, "other", {}, {}, []]]}, posts)
    assert c.cancel_prompt("mine") == "deleted"
    assert posts == [("/queue", {"delete": ["mine"]})]          # the other client's prompt untouched
    posts.clear()
    c = _comfy({"queue_pending": [], "queue_running": [[0, "mine", {}, {}, []]]}, posts)
    assert c.cancel_prompt("mine") == "interrupted"
    assert posts == [("/interrupt", {"prompt_id": "mine"})]
    posts.clear()
    c = _comfy({"queue_pending": [], "queue_running": [[0, "other", {}, {}, []]]}, posts)
    assert c.cancel_prompt("mine") == "finished" and posts == []


def test_prompt_queued_under_a_job_is_canceled_with_it():
    posts = []
    c = _comfy({"queue_pending": [[1, "mine", {}, {}, []]], "queue_running": []}, posts)
    q = JobQueue()
    started = threading.Event()

    def work():
        c.queue({"1": {}})
        started.set()
        while True:
            jobs.check_canceled()
            time.sleep(0.01)
    j = q.submit("k", "p", work)
    started.wait(5)
    q.cancel(j.id)
    assert _wait(q, j) == "canceled"
    assert posts == [("/queue", {"delete": ["mine"]})]


# --- lanes: slots, positions, exclusive keys, hops, borrowing ----------------------------------

def _until(cond, t=5):
    end = time.time() + t
    while not cond() and time.time() < end:
        time.sleep(0.01)
    return cond()


def test_gpu_lane_runs_one_job_and_shows_queue_positions():
    q = JobQueue()
    gate, running = threading.Event(), []

    def work(n):
        running.append(n)
        gate.wait(5)
        running.remove(n)
    a = q.submit("3d.trellis", "p", lambda: work("a"), lane="gpu0", tag={"asset": "a"})
    b = q.submit("3d.trellis", "p", lambda: work("b"), lane="gpu0", tag={"asset": "b"})
    c = q.submit("3d.trellis", "p", lambda: work("c"), lane="gpu0", tag={"asset": "c"})
    assert _until(lambda: q.get(a.id).status == "running")
    time.sleep(0.1)
    assert running == ["a"]                                   # never two on one GPU
    assert q.get(b.id).public()["waiting"] == {"lane": "gpu0", "position": 1, "reason": "slot"}
    assert q.get(c.id).public()["waiting"]["position"] == 2
    gate.set()
    for j in (a, b, c):
        assert _wait(q, j) == "done"


def test_cpu_lane_runs_up_to_its_slots():
    q = JobQueue(slots={"cpu": 2})
    gate, peak, now = threading.Event(), [], []

    def work():
        now.append(1)
        peak.append(len(now))
        gate.wait(5)
        now.pop()
    js = [q.submit("library.rebuild", "p", work, lane="cpu", tag={"asset": f"a{i}"}) for i in range(3)]
    assert _until(lambda: len(now) == 2)
    time.sleep(0.1)
    assert q.get(js[2].id).status == "queued" and q.get(js[2].id).waiting["position"] == 1
    gate.set()
    for j in js:
        assert _wait(q, j) == "done"
    assert max(peak) == 2


def test_jobs_writing_the_same_files_never_overlap():
    q = JobQueue(slots={"cpu": 3})
    gate = threading.Event()
    a = q.submit("site.build", "p", lambda: gate.wait(5), lane="cpu")
    b = q.submit("shots.render", "p", lambda: "rendered", lane="cpu")      # same greybox
    c = q.submit("site.build", "other", lambda: "other project", lane="cpu")
    d = q.submit("library.rebuild", "p", lambda: "asset", lane="cpu", tag={"asset": "x"})
    assert _wait(q, c) == "done" and _wait(q, d) == "done"               # others pass it
    assert q.get(b.id).status == "queued"
    gate.set()
    assert _wait(q, b) == "done"


def test_hop_frees_the_gpu_while_the_job_goes_on_on_the_cpu():
    q = JobQueue()
    on_cpu, release = threading.Event(), threading.Event()

    def brief_build():
        jobs.hop("cpu")
        on_cpu.set()
        release.wait(5)
        return "built"
    a = q.submit("library.brief_build", "p", brief_build, lane="gpu0", tag={"asset": "a"})
    assert on_cpu.wait(5)
    b = q.submit("3d.trellis", "p", lambda: "trellis", lane="gpu0", tag={"asset": "b"})
    assert _wait(q, b) == "done"                              # GPU 0 was free while a builds
    assert q.get(a.id).lane == "cpu" and q.get(a.id).holds == ["cpu"]
    release.set()
    assert _wait(q, a) == "done" and q.get(a.id).result == "built"


def test_a_section_borrows_a_busy_lane_in_line():
    q = JobQueue()
    gate, order = threading.Event(), []
    t = q.submit("3d.trellis", "p", lambda: (gate.wait(5), order.append("trellis")), lane="gpu0")
    assert _until(lambda: q.get(t.id).status == "running")

    def build():
        with jobs.using("gpu0", "vlm"):
            order.append("vlm")
        return jobs.current_job().used_vlm
    c = q.submit("library.build", "p", build, lane="cpu")
    assert _until(lambda: (q.get(c.id).waiting or {}).get("lane") == "gpu0")
    assert q.get(c.id).status == "running" and order == []
    gate.set()
    assert _wait(q, c) == "done" and order == ["trellis", "vlm"] and q.get(c.id).result is True


def test_canceling_a_job_waiting_for_a_borrowed_lane():
    q = JobQueue()
    gate = threading.Event()
    q.submit("3d.trellis", "p", lambda: gate.wait(5), lane="gpu0")

    def build():
        with jobs.using("gpu0", "vlm"):
            return "never"
    c = q.submit("library.build", "p", build, lane="cpu")
    assert _until(lambda: (q.get(c.id).waiting or {}).get("lane") == "gpu0")
    q.cancel(c.id)
    assert _wait(q, c) == "canceled"
    gate.set()


# --- GPU policy: VRAM before a GPU job, the VLM unloaded after its job ------------------------

class FakeVlm:
    def __init__(self, up=False):
        self.st = {"model": "32b", "gpu": 0, "pid": 999999} if up else None
        self.downs = 0

    def state(self):
        return self.st

    def down(self, gpu=None, log=None, **kw):
        self.downs += 1
        self.st = None


def _policy(free, vlm_mod, comfy_pid=None):
    from asset_pipeline import gpu_policy
    cfg = {"cpu_slots": 2, "gpus": {"gpu0": 0, "comfy": 1}, "vram_gb": {"gpu": 16.0, "comfy": 12.0, "vlm": 28.0},
           "poll_s": 0.05}
    return gpu_policy.GpuPolicy(free_fn=free, procs_fn=lambda: {0: {4242: 20.0}}, vlm_mod=vlm_mod,
                                comfy_pid_fn=lambda: comfy_pid, cfg=cfg, sleep=lambda s: time.sleep(0.01))


def test_gpu_job_queues_until_vram_is_free():
    free = {"gb": 6.0}
    q = JobQueue(policy=_policy(lambda gpu, besides: free["gb"], FakeVlm()))
    j = q.submit("3d.trellis", "p", lambda: "ok", lane="gpu0")
    assert _until(lambda: "Waiting for 16 GB" in q.get(j.id).note)
    pub = q.get(j.id).public()
    assert pub["status"] == "queued" and pub["waiting"]["reason"] == "vram" and "held by" in pub["note"]
    free["gb"] = 20.0
    assert _wait(q, j) == "done" and q.get(j.id).note == ""


def test_vram_wait_can_be_canceled():
    q = JobQueue(policy=_policy(lambda gpu, besides: 1.0, FakeVlm()))
    j = q.submit("3d.trellis", "p", lambda: "never", lane="gpu0")
    assert _until(lambda: "Waiting for" in q.get(j.id).note)
    q.cancel(j.id)
    assert _wait(q, j) == "canceled" and q.get(j.id).result is None


def test_comfy_jobs_count_comfyuis_own_memory_as_free():
    seen = []
    q = JobQueue(policy=_policy(lambda gpu, besides: seen.append((gpu, besides)) or 30.0, FakeVlm(), comfy_pid=os.getpid()))
    assert _wait(q, q.submit("refs.generate", "p", lambda: "ok", lane="comfy")) == "done"
    assert seen[0][0] == 1 and os.getpid() in seen[0][1]
    seen.clear()
    assert _wait(q, q.submit("refs.generate", "p", lambda: "ok", lane="comfy", gated=False)) == "done"
    assert seen == []                                         # a hosted backend: no local VRAM


def test_trellis_unloads_the_vlm_first():
    v = FakeVlm(up=True)
    q = JobQueue(policy=_policy(lambda gpu, besides: 30.0 if v.st is None else 3.0, v))
    assert _wait(q, q.submit("3d.trellis", "p", lambda: "ok", lane="gpu0")) == "done"
    assert v.downs == 1


def test_vlm_unloaded_after_its_job_unless_another_vlm_job_waits():
    v = FakeVlm(up=True)
    q = JobQueue(policy=_policy(lambda gpu, besides: 30.0, v))
    gate = threading.Event()
    a = q.submit("plan.analyze", "p", lambda: gate.wait(5), lane="gpu0")
    b = q.submit("library.guide", "p", lambda: "ok", lane="gpu0")
    assert _until(lambda: q.get(a.id).status == "running")
    gate.set()
    assert _wait(q, a) == "done" and _wait(q, b) == "done"
    assert v.downs == 1                                       # kept for b, unloaded after it
    v.st = {"model": "32b", "gpu": 0, "pid": 1}
    assert _wait(q, q.submit("vlm.up", "", lambda: "up", lane="gpu0")) == "done"
    assert v.downs == 1                                       # Start keeps it loaded


def test_borrowed_vlm_unloaded_when_the_job_ends_and_gpu0_is_idle():
    v = FakeVlm(up=True)
    q = JobQueue(policy=_policy(lambda gpu, besides: 30.0, v))

    def build():
        with jobs.using("gpu0", "vlm"):
            pass
        return "built"
    assert _wait(q, q.submit("library.build", "p", build, lane="cpu")) == "done"
    assert _until(lambda: v.downs == 1)
