"""Long-job estimates: recorded run times, the runs.jsonl fallback, defaults, item scaling, the
estimate-only request (asks an endpoint what it would start, starts nothing), the Areas batch
estimate, time left in the Jobs panel, and Stop on a long batch. No GPU, no Blender."""
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import areas, estimates, kit_proc
from asset_pipeline.jobs import EstimateOnly, JobQueue, estimate_only
from asset_pipeline.ui import app as ui_app

from test_areas import store  # noqa: F401  (fixture: a tmp project "demo" with a library plan)

U = "/api/projects/demo"
STATIC = Path(ui_app.__file__).parent / "static"


def _jsonl(p, rows):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_per_item_prefers_recorded_runs_then_runs_jsonl_then_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    estimates._cache.clear()
    s, src = estimates.per_item("library.build")
    assert s == estimates.DEFAULTS["library.build"] and "default" in src
    _jsonl(tmp_path / "projects/x/runs.jsonl", [{"stage": "3d.trellis", "mode": "1024_cascade", "seconds": v} for v in (100, 200, 300)])
    s, src = estimates.per_item("library.trellis", "1024_cascade")
    assert s == 200 and "runs.jsonl" in src
    assert estimates.per_item("3d.trellis", "512")[0] == pytest.approx(180 * 0.6)       # no 512 runs logged yet
    _jsonl(estimates.log_path(), [{"kind": "library.trellis", "variant": "1024_cascade", "items": 2, "seconds": s}
                                  for s in (400, 500, 600)])
    s, src = estimates.per_item("library.trellis", "1024_cascade")
    assert s == 250 and "recorded" in src                        # per item: 500 s for 2 items


def test_items_scale_frames_sheets_and_batches(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))                  # defaults only: no recorded runs
    assert estimates.items("frames.generate", {"shots": ["a", "b", "c"], "n": 4}) == 12
    assert estimates.items("refs.generate", {"units": ["u1", "u2"], "n": 2}) == 4
    assert estimates.items("library.build", {"n": 18}) == 18
    e = estimates.estimate("library.build", "cpu", {"n": 18})
    assert e["seconds"] == 18 * e["per_item_s"] and e["long"] and e["lanes"] == ["cpu"]
    r = estimates.estimate("library.reroll", "gpu0", {})
    assert r["lanes"] == ["gpu0", "cpu"] and not r["long"] and "GPU 0 busy" in r["blocks"][0]


def test_finished_jobs_are_recorded_and_get_an_estimate(tmp_path):
    log = tmp_path / "times.jsonl"
    q = JobQueue(estimator=lambda j: 42, recorder=lambda j: estimates.record(j, log))
    j = q.submit("library.build", "p", lambda: time.sleep(0.05), lane="cpu", tag={"n": 3})
    assert j.est_s == 42 and j.public()["est_s"] == 42
    for _ in range(200):
        if q.get(j.id).status == "done":
            break
        time.sleep(0.01)
    time.sleep(0.05)
    rec = json.loads(log.read_text())
    assert rec["kind"] == "library.build" and rec["items"] == 3 and rec["seconds"] >= 0.05
    bad = q.submit("x", "p", lambda: 1 / 0, lane="cpu")                 # failures aren't recorded
    while q.get(bad.id).status != "error":
        time.sleep(0.01)
    time.sleep(0.05)
    assert len(log.read_text().splitlines()) == 1


def test_estimate_only_submit_starts_nothing():
    q = JobQueue()
    tok = estimate_only.set(True)
    try:
        with pytest.raises(EstimateOnly) as e:
            q.submit("3d.trellis", "p", lambda: pytest.fail("ran"), lane="gpu0", tag={"mode": "512"})
    finally:
        estimate_only.reset(tok)
    assert e.value.kind == "3d.trellis" and e.value.tag == {"mode": "512"} and q.list() == []


@pytest.fixture
def client(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ui_app, "_comfy_up", lambda: True)
    q = JobQueue()
    return TestClient(ui_app.create_app(q)), q


def test_estimate_header_asks_the_endpoint_and_starts_no_job(client, store):  # noqa: F811
    c, q = client
    areas.define(store, "a", shots=["med-a"])
    r = c.post(f"{U}/areas/batch", json={"action": "build", "area": "a"}, headers={"X-AP-Estimate": "1"}).json()
    e = r["estimate"]
    assert e["kind"] == "library.build" and e["items"] == 1 and e["lanes"] == ["cpu"] and q.list() == []
    # a route that changes files before it starts its job can't be estimated
    assert c.post(f"{U}/library/asset/hall/rebuild", json={"brief": {}}, headers={"X-AP-Estimate": "1"}).status_code == 400
    # the endpoint's own checks still answer (nothing to do: 400, no estimate)
    assert c.post(f"{U}/areas/batch", json={"action": "build", "district": "south"}, headers={"X-AP-Estimate": "1"}).status_code == 400
    assert "/api/projects/{slug}/3d" in c.get("/api/estimate/routes").json()["routes"]
    assert "library.build" in c.get("/api/estimate/kinds").json()["kinds"]


def test_areas_batches_carry_their_estimate(client, store):  # noqa: F811
    c, _ = client
    areas.define(store, "a", shots=["med-a"])
    b = c.get(f"{U}/areas/status", params={"area": "a"}).json()["batches"]
    assert b["build"]["estimate"]["items"] == 1 and b["build"]["estimate"]["seconds"] > 0
    dry = c.post(f"{U}/areas/batch", json={"action": "build", "area": "a", "dry_run": True}).json()
    assert dry["estimate"]["kind"] == "library.build" and "long" in dry["estimate"]


def test_uncounted_job_shows_time_left_from_its_estimate():
    class J:
        kind, tag, created, est_s = "site.build", {}, 0.0, 100
    out = ui_app._progress.__globals__["_with_estimate"]({"done": None, "total": None, "elapsed_s": 30}, J(), 30)
    assert out["eta_s"] == 70 and not out["over"]
    out = ui_app._progress.__globals__["_with_estimate"]({"elapsed_s": 130}, J(), 130)
    assert out["eta_s"] == 0 and out["over"]


def test_stop_cancels_a_long_batch_between_items(client, store, monkeypatch):  # noqa: F811
    c, q = client
    areas.define(store, "a", shots=["med-a"])
    started, gate = [], threading.Event()

    def slow(st, a, log=print, surface=None):
        started.append(a)
        gate.wait(5)
    monkeypatch.setattr(kit_proc, "build_any", slow)
    j = c.post(f"{U}/areas/batch", json={"action": "build", "area": "a", "force": True}).json()
    for _ in range(200):
        if started:
            break
        time.sleep(0.01)
    assert c.post(f"/api/jobs/{j['id']}/cancel").json()["id"] == j["id"]
    gate.set()
    for _ in range(300):
        if c.get(f"/api/jobs/{j['id']}").json()["status"] in ("canceled", "done", "error"):
            break
        time.sleep(0.01)
    assert c.get(f"/api/jobs/{j['id']}").json()["status"] == "canceled"


def test_ui_asks_before_long_jobs():
    net, est = (STATIC / "net.js").read_text(), (STATIC / "estimate.js").read_text()
    assert "longJobGate(path, body)" in net and "START_CANCELED" in net
    assert 'src="/static/estimate.js"' in (STATIC / "index.html").read_text()
    assert 'button: "Start"' in est and "X-AP-Estimate" in est
    areas_js = (STATIC / "areas.js").read_text()
    assert '{confirmed: true}' in areas_js and "longJobHtml(e)" in areas_js       # one dialog, not two
