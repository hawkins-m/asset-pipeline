"""World mode UI: catalog editing (yours vs auto), per-shot prompt / camera edits, regenerate."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import catalog
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import write_json
from asset_pipeline.stages import s0_frames, sw_shots, sw_site
from asset_pipeline.ui import app as ui_app

from test_city import small_city
from test_city_types import CATALOG
from test_world_frames import world  # noqa: F401  (fixture)


def _wait(client, job_id):
    for _ in range(500):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
def client():
    return TestClient(ui_app.create_app(JobQueue()))


def test_catalog_edits_are_saved_validated_and_marked_as_yours(world, client, tmp_path):  # noqa: F811
    p = tmp_path / "cat.yaml"
    p.write_text(CATALOG)
    L = catalog.apply(small_city(trees=False), catalog.load(p))
    L.shots = sw_site.load_layout(world).shots
    sw_site.save_layout(world, L)
    url = "/api/projects/w/catalog"
    got = client.get(url).json()
    assert got["city"] and [d["id"] for d in got["districts"]] == ["a", "b"]
    assert all(not d["user_fields"] for d in got["districts"])             # imported: auto
    rows = got["districts"]
    rows[1]["notes"] = "a busy market quarter with canvas awnings"
    rows[1]["mix"]["edge"] = {"rows": 2, "villas": 1}
    assert client.put(f"{url}/districts", json=rows).json() == {"saved": "districts"}
    got = client.get(url).json()
    assert got["districts"][1]["user_fields"] == ["mix", "notes"] and got["districts"][0]["user_fields"] == []
    # an unknown typology in a mix is refused, nothing saved
    rows = got["districts"]
    rows[0]["mix"]["core"] = {"nope": 1}
    r = client.put(f"{url}/districts", json=rows)
    assert r.status_code == 400 and "unknown typology nope" in r.json()["detail"]
    assert "nope" not in json.dumps(client.get(url).json()["districts"])
    # duplicate ids are refused
    t = got["typologies"]
    assert client.put(f"{url}/typologies", json=t + [t[0]]).status_code == 400
    t[0]["prompt"] = "big courtyard blocks"
    client.put(f"{url}/typologies", json=t)
    assert client.get(url).json()["typologies"][0]["user_fields"] == ["prompt"]
    checks = got["checks"] | {"max_identical_run": 2}
    client.put(f"{url}/checks", json=checks)
    got = client.get(url).json()
    assert got["checks"]["max_identical_run"] == 2 and got["section_edits"] == ["checks"]
    assert client.put(f"{url}/nope", json={}).status_code == 404


def test_shot_prompt_edits_and_reset(world, client):  # noqa: F811
    url = "/api/projects/w/shots/a/prompt"
    auto = s0_frames.prompt_for(world, "a")
    got = client.put(url, json={"append": " golden hour "}).json()
    assert got["prompt"] == f"{auto}, golden hour" and got["source"] == "append"
    shot = next(s for s in client.get("/api/projects/w/frames").json()["shots"] if s["id"] == "a")
    assert shot["prompt_parts"]["append"] == "golden hour" and shot["prompt"].endswith("golden hour")
    assert client.put(url, json={"append": "", "override": "  "}).json()["source"] == "auto"
    assert client.put("/api/projects/w/shots/zz/prompt", json={}).status_code == 404


def test_camera_edit_writes_the_effective_camera_and_keeps_auto_values(world, client, monkeypatch):  # noqa: F811
    written = []
    monkeypatch.setattr(sw_site, "add_shot", lambda store, spec: written.append(spec) or {"shots": [1]})
    job = client.put("/api/projects/w/shots/a/camera",
                     json={"nudge_pos": [0, 2, 0], "lens_override": 50, "tier_override": "tight"}).json()
    assert _wait(client, job["id"])["status"] == "done"
    spec = sw_site.load_layout(world).shots[0]
    assert spec.pos == (0, 0, 0) and spec.nudge_pos == (0, 2, 0) and spec.lens_override == 50   # auto kept apart
    assert written[0].effective().pos == pytest.approx((0, 0, 2))           # up is +Z for this camera
    edit = next(s for s in client.get("/api/projects/w/frames").json()["shots"] if s["id"] == "a")["edit"]
    assert edit["camera_edited"] and edit["lens_auto"] == 35 and edit["tier_override"] == "tight"


def test_regenerate_renders_stale_passes_first(world, client, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(sw_shots, "render", lambda store, shots: calls.append(("render", shots)))
    monkeypatch.setattr(s0_frames, "generate", lambda store, shot, n, refs, ref_strength:
                        calls.append(("generate", shot, n)) or store.root / "frames/a/batch_009")
    job = client.post("/api/projects/w/shots/a/regenerate", json={"n": 2}).json()
    assert _wait(client, job["id"])["result"] == "frames/a/batch_009"
    assert calls == [("generate", "a", 2)]                                  # passes current: no render
    gb = json.loads((world.root / "site/greybox.json").read_text())
    write_json(world.root / "site/greybox.json", gb | {"blend_sha256": "changed"})
    calls.clear()
    _wait(client, client.post("/api/projects/w/shots/a/regenerate", json={"n": 1}).json()["id"])
    assert calls == [("render", ["a"]), ("generate", "a", 1)]


def test_project_files_are_revalidated(world, client):  # noqa: F811
    r = client.get("/files/w/shots/a/depth.png")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache" and r.headers.get("etag")
    assert client.get("/files/w/shots/a/depth.png", headers={"If-None-Match": r.headers["etag"]}).status_code == 304


def test_status_counts_frames_written_by_a_running_job(world, client, monkeypatch):  # noqa: F811
    """The window chrome's progress bar counts the frames a job has written so far (the jobs
    themselves report nothing); ComfyUI is not contacted for real."""
    import threading
    monkeypatch.setattr(ui_app, "_comfy_up", lambda: False)
    go, wrote = threading.Event(), threading.Event()

    def fake_generate(store, shot, n, refs, ref_strength):
        d = store.root / "frames" / shot / "batch_100"
        d.mkdir(parents=True, exist_ok=True)
        (d / "frame_000.png").write_bytes(b"x")
        wrote.set()
        go.wait(5)
        return d
    monkeypatch.setattr(s0_frames, "generate", fake_generate)
    job = client.post("/api/projects/w/shots/a/regenerate", json={"n": 4}).json()
    assert job["tag"] == {"shot": "a", "n": 4, "shots": ["a"]}
    assert wrote.wait(5)
    st = client.get("/api/projects/w/status").json()
    p = st["progress"][str(job["id"])]
    assert (p["done"], p["total"]) == (1, 4) and p["line"] == "a · frame 2 of 4" and "eta_s" in p
    assert st["comfy"] is False and st["greybox_mtime"]
    go.set()
    _wait(client, job["id"])
    assert client.get("/api/projects/w/status").json()["progress"] == {}


def test_frames_listing_cache_follows_new_frames_and_roles(world, client):  # noqa: F811
    """The per-shot listing is cached on file times: a new batch or a role change shows."""
    assert all(not r["batches"] for r in client.get("/api/projects/w/frames").json()["shots"])
    d = world.root / "frames/a/batch_001"
    d.mkdir(parents=True)
    (d / "frame_000.png").write_bytes(b"x")
    write_json(d / "meta.json", {"frames": [{"file": "frame_000.png", "seed": 1, "edge_match": 0.5}]})
    shot = next(r for r in client.get("/api/projects/w/frames").json()["shots"] if r["id"] == "a")
    assert [f["key"] for f in shot["batches"][0]["frames"]] == ["frames/a/batch_001/frame_000.png"]
    assert client.post("/api/projects/w/role", json={"path": "frames/a/batch_001/frame_000.png", "role": "design_ref"}).status_code == 200
    shot = next(r for r in client.get("/api/projects/w/frames").json()["shots"] if r["id"] == "a")
    assert shot["batches"][0]["frames"][0]["role"] == "design_ref"
