"""The stale-code bar: the commit ap ui started on vs the repo's, and Restart (no real exec)."""
import time

from fastapi.testclient import TestClient

from asset_pipeline.jobs import JobQueue
from asset_pipeline.ui import app as ui_app


def test_version_offers_a_restart_only_for_a_newer_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    heads = iter(["aaa", "aaa", "bbb"])
    monkeypatch.setattr(ui_app, "git_head", lambda code=None: next(heads))
    monkeypatch.setattr(ui_app, "newer_commit", lambda a, b, code=None: a != b)
    c = TestClient(ui_app.create_app(jobs=JobQueue()))
    v = c.get("/api/version").json()
    assert v["commit"] == "aaa" and not v["restart"]
    assert c.get("/api/version").json()["restart"]


def test_restart_refuses_while_jobs_run_unless_forced(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    done = []
    monkeypatch.setitem(ui_app.app_restart, "fn", lambda: done.append(1))
    q = JobQueue()
    c = TestClient(ui_app.create_app(jobs=q))
    assert c.post("/api/restart").json() == {"restarting": True}
    job = q.submit("library.reroll", "demo", lambda: time.sleep(1.0))
    r = c.post("/api/restart")
    assert r.status_code == 409 and "library.reroll" in r.json()["detail"]
    assert c.post("/api/restart?force=true").status_code == 200
    time.sleep(0.7)
    assert done == [1, 1]
    q.cancel(job.id)


def test_newer_commit_needs_a_descendant(tmp_path):
    assert not ui_app.newer_commit("", "abc") and not ui_app.newer_commit("abc", "abc")
