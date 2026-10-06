"""Job lanes and cancellation (UI Stop buttons), ComfyUI prompt cancel. No GPU."""
import json
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
