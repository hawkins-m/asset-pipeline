"""The UI endpoints for the remaining runbook commands (areas panel, preflight, library/frames/
shots/site/terrain/Unreal/VLM/demo/launcher). Every slow function is monkeypatched: nothing here
runs ComfyUI, Blender, Unreal, the VLM or the demo script, and nothing touches a real home."""
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import areas, kit_proc, launcher, preflight, ue, vlm
from asset_pipeline.jobs import JobQueue
from asset_pipeline.ui import app as ui_app
from asset_pipeline.ui import tools_api

from test_areas import store  # noqa: F401  (fixture: a tmp project "demo" with a library plan)

STATIC = Path(ui_app.__file__).parent / "static"


def _wait(client, job):
    for _ in range(500):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error", "canceled"):
            return j
        time.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.fixture
def client(store, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ui_app, "_comfy_up", lambda: True)
    return TestClient(ui_app.create_app(JobQueue()))


U = "/api/projects/demo"


def test_areas_define_status_checklist(client, store):  # noqa: F811
    got = client.get(f"{U}/areas").json()
    assert got["areas"] == {} and got["shots"] == ["med-a", "med-b"] and got["has_plan"]
    assert client.post(f"{U}/areas", json={"name": "Bad Name", "shots": ["med-a"]}).status_code == 400
    assert client.post(f"{U}/areas", json={"name": "a", "shots": ["nope"]}).status_code == 400
    assert client.post(f"{U}/areas", json={"name": "a", "shots": ["med-a"]}).json()["shots"] == ["med-a"]
    rows = client.get(f"{U}/areas/status", params={"area": "a"}).json()["rows"]
    assert {r["asset"] for r in rows} == {"hall", "row-house-brick", "stall"}
    assert client.get(f"{U}/areas/status", params={"area": "zzz"}).status_code == 404
    assert client.get(f"{U}/areas/status").status_code == 400
    assert {r["asset"] for r in client.get(f"{U}/areas/status", params={"district": "south"}).json()["rows"]} == {"row-house-brick"}
    ck = client.post(f"{U}/areas/a/checklist").json()
    assert ck["rel"] == "areas/a.md" and "| hall |" in ck["text"] and (store.root / "areas" / "a.md").is_file()


def test_batch_dry_run_counts_and_build_job(client, store, monkeypatch):  # noqa: F811
    areas.define(store, "a", shots=["med-a"])
    plan = client.post(f"{U}/areas/batch", json={"action": "build", "area": "a", "dry_run": True}).json()
    assert plan["count"] == 1 and plan["items"] == ["hall"]
    assert "no brief yet" in plan["notes"][0]                         # row-house-brick has none
    built = []
    monkeypatch.setattr(kit_proc, "build_any", lambda s, a, log=print, surface=None: built.append(a))
    j = _wait(client, client.post(f"{U}/areas/batch", json={"action": "build", "area": "a"}).json())
    assert j["status"] == "done" and built == ["hall"] and j["result"] == {"done": 1, "failed": []}
    # a failing item is reported and the job fails with the names
    monkeypatch.setattr(kit_proc, "build_any", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    j = _wait(client, client.post(f"{U}/areas/batch", json={"action": "build", "area": "a"}).json())
    assert j["status"] == "error" and "hall: RuntimeError: boom" in j["error"]
    # scope is required; nothing to do is a 400
    assert client.post(f"{U}/areas/batch", json={"action": "build"}).status_code == 400
    from asset_pipeline import library_instances
    inst = []
    monkeypatch.setattr(library_instances, "build_entries", lambda st, es, near, radius, skip_fresh=True: inst.append((es, skip_fresh)) or {es[0]: []})
    r = client.post(f"{U}/areas/batch", json={"action": "instances", "area": "a", "force": True}).json()
    assert r["lane"] == "cpu" and _wait(client, r)["status"] == "done" and inst == [(["hall"], False)]
    assert client.post(f"{U}/areas/batch", json={"action": "build", "district": "south"}).status_code == 400   # nothing to build
    # trellis: the stall has no chosen view
    t = client.post(f"{U}/areas/batch", json={"action": "trellis", "area": "a", "dry_run": True}).json()
    assert t["count"] == 0 and "no view chosen" in t["notes"][0]
    s = client.post(f"{U}/areas/batch", json={"action": "sheets", "area": "a", "dry_run": True}).json()
    assert s["items"] == ["stall"] and "row-house-brick" in s["notes"][0]     # hall has a brief: needs no sheets


def test_status_carries_each_batch_and_trellis_never_changes_a_build_method(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline import review
    from asset_pipeline.stages import s5_3d, sw_library
    areas.define(store, "a", shots=["med-a"])
    b = client.get(f"{U}/areas/status", params={"area": "a"}).json()["batches"]
    assert set(b) == {"sheets", "build", "trellis", "instances"} and b["build"]["items"] == ["hall"]
    assert b["sheets"]["items"] == ["stall"] and b["trellis"]["items"] == []
    routes = {r["asset"]: r["route"] for r in client.get(f"{U}/areas/status", params={"area": "a"}).json()["rows"]}
    monkeypatch.setattr(review, "chosen", lambda st, plan, a: "views/x.png")
    t = client.post(f"{U}/areas/batch", json={"action": "trellis", "area": "a", "dry_run": True}).json()
    assert t["items"] == [a for a, r in routes.items() if r == "trellis"] and "hall" not in t["items"]
    ran = []
    monkeypatch.setattr(s5_3d, "run", lambda st, plan, a, mode=None: ran.append(a))
    dec_before = sw_library.decisions(store)
    assert _wait(client, client.post(f"{U}/areas/batch", json={"action": "trellis", "area": "a"}).json())["status"] == "done"
    assert ran == t["items"] and sw_library.decisions(store) == dec_before
    assert {r["asset"]: r["route"] for r in client.get(f"{U}/areas/status", params={"area": "a"}).json()["rows"]} == routes


def test_sheets_batch_runs_in_the_comfy_lane(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline.stages import s2_refs, sw_library
    areas.define(store, "a", shots=["med-a"])
    seen = {}

    class Unit:
        key = "stall"

    def asset(i):
        return type("A", (), {"id": i, "include": True})()

    from asset_pipeline import library_entries
    monkeypatch.setattr(sw_library, "asset_plan", lambda *a, **k: pytest.fail("would drop the other included assets"))
    monkeypatch.setattr(library_entries, "sync_plan", lambda st, extra=(): seen.update(extra=list(extra)))
    monkeypatch.setattr(s2_refs, "units", lambda st, plan=None: [Unit()])
    monkeypatch.setattr(s2_refs, "sheets", lambda st, u: [])
    made = []
    monkeypatch.setattr(s2_refs, "generate", lambda st, u, n=2, **k: made.append(u.key))
    r = client.post(f"{U}/areas/batch", json={"action": "sheets", "area": "a", "assets": ["stall"]}).json()
    assert r["lane"] == "comfy" and r["kind"] == "library.sheets"
    assert _wait(client, r)["status"] == "done" and made == ["stall"] and seen["extra"] == ["stall"]


def test_brief_build_runs_brief_when_missing_then_builds(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline import library_review as lr
    calls = []
    monkeypatch.setattr(lr, "rebuild", lambda st, a, log=None: calls.append(("rebuild", a)) or {"images": []})
    r = client.post(f"{U}/library/asset/hall/brief-build", json={}).json()          # has a brief: build only
    assert r["lane"] == "cpu" and _wait(client, r)["status"] == "done" and calls == [("rebuild", "hall")]   # no VLM: no GPU
    assert client.post(f"{U}/library/asset/stall/brief-build", json={}).status_code == 400   # no brief, no sheets
    from asset_pipeline import building_proc
    monkeypatch.setattr(building_proc, "front_view", lambda st, sh, a: {"best": (sh[0], 1, 0.9), "scores": []})
    monkeypatch.setattr(building_proc, "brief", lambda *a, **k: calls.append(("brief", a[1], a[2], a[3], a[4], a[5])) or {})
    r = client.post(f"{U}/library/asset/hall/brief-build",
                    json={"sheet": "s.png", "force_brief": True, "form": "cube", "dims": [10, 10, 10]}).json()
    assert r["lane"] == "gpu0"                                   # the brief on GPU 0, then the build on the CPU
    done = _wait(client, r)
    assert done["status"] == "done" and done["lane"] == "cpu" and done["holds"] == []
    assert ("brief", "hall", "s.png", 1, "cube", [10.0, 10.0, 10.0]) in calls


def test_preflight_lists_checks_with_fixes(client, monkeypatch):
    C = preflight.Check
    monkeypatch.setattr(preflight, "run", lambda ue=False: [
        C("ComfyUI (sdk backend)", False, "no proxy"), C("Vision LLM (Qwen3-VL)", False, "none installed"),
        C("Blender", True, "5.2"), C("TRELLIS.2 (irregular assets; not on the demo path)", False, "missing", required=False)])
    r = client.post("/api/preflight", json={"ue": True}).json()
    by = {c["name"]: c for c in r["checks"]}
    assert not r["ok"] and r["ue"]
    assert by["ComfyUI (sdk backend)"]["fix"] == {"kind": "start_comfy", "label": "Start ComfyUI"}
    assert by["Vision LLM (Qwen3-VL)"]["fix"]["command"].startswith("scripts/install_llamacpp_vlm.sh")
    assert by["Blender"]["fix"] is None and by["TRELLIS.2 (irregular assets; not on the demo path)"]["fix"]["kind"] == "install"
    monkeypatch.setattr(preflight, "run", lambda ue=False: [C("Blender", True, "ok")])
    assert client.post("/api/preflight", json={}).json()["ok"]


def test_library_pending_silhouette_and_review_round(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline import library_review as lr
    from asset_pipeline import look
    monkeypatch.setattr(look, "pending_slots", lambda st, shot, draft=True: [{"slot": "x", "frame_share": 0.1, "state": ["no build"]}])
    j = _wait(client, client.post(f"{U}/library/pending", json={"shot": "med-a"}).json())
    assert j["result"][0]["slot"] == "x"
    monkeypatch.setattr(look, "silhouette_of", lambda st, e, shot: {"iou": 0.8})
    j = _wait(client, client.post(f"{U}/library/silhouette", json={"shot": "med-a", "entries": ["hall"]}).json())
    assert j["result"] == {"hall": {"iou": 0.8}}
    assert client.post(f"{U}/library/silhouette", json={"shot": "med-a", "entries": []}).status_code == 400
    monkeypatch.setattr(lr, "task_list", lambda st: "TASKS")
    monkeypatch.setattr(lr, "done", lambda st, assets: 3)
    assert client.get(f"{U}/library/review/tasks").json()["text"] == "TASKS"
    assert client.post(f"{U}/library/review/done", json={"assets": ["hall"]}).json() == {"round": 3}
    assert client.post(f"{U}/library/review/done", json={"assets": []}).status_code == 400


def test_frames_lock_shots_add_vegetation_overlap(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline import vegetation
    from asset_pipeline.stages import s0_frames, sw_site
    got = []
    monkeypatch.setattr(s0_frames, "lock", lambda st, shot, frame: got.append((shot, frame)) or frame)
    assert client.post(f"{U}/frames/lock", json={"shot": "med-a"}).status_code == 400
    assert client.post(f"{U}/frames/lock", json={"shot": "med-a", "greybox": True}).json()["locked"] == "greybox"
    client.post(f"{U}/frames/lock", json={"shot": "med-a", "unlock": True})
    client.post(f"{U}/frames/lock", json={"shot": "med-a", "frame": "frames/med-a/batch_001/f.png"})
    assert got == [("med-a", "greybox"), ("med-a", None), ("med-a", "frames/med-a/batch_001/f.png")]
    added = []
    monkeypatch.setattr(sw_site, "add_shot", lambda st, shot: added.append(shot))
    body = {"id": "s-1", "pos": [0, -100, 2], "look_at": [0, 0, 10], "lens": 28, "tier": "wide", "res": "1920x1080"}
    j = _wait(client, client.post(f"{U}/shots/add", json=body).json())
    assert j["status"] == "done" and added[0].id == "s-1" and added[0].resolution == (1920, 1080) and added[0].lens_mm == 28
    assert client.post(f"{U}/shots/add", json=body | {"tier": "huge"}).status_code == 422
    monkeypatch.setattr(vegetation, "street_tree_slots", lambda st: {"counts": {"street": 4, "plaza": 1}})
    monkeypatch.setattr(vegetation, "preview", lambda st, shots: [Path("p.png")])
    j = _wait(client, client.post(f"{U}/site/vegetation", json={"preview": ["med-a"]}).json())
    assert j["result"]["counts"] == {"street": 4, "plaza": 1} and j["result"]["previews"] == ["p.png"]
    monkeypatch.setattr(sw_site, "plan_overlap", lambda st, shot: {"iou": {"road": 0.5}})
    assert _wait(client, client.post(f"{U}/site/overlap", json={"shot": "med-a"}).json())["result"] == {"iou": {"road": 0.5}}


def test_terrain_apply_and_ue_need_confirmation(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline import terrain_build as tb
    monkeypatch.setattr(tb, "apply", lambda st: "applied")
    monkeypatch.setattr(tb, "ue_assets", lambda st: "ue assets")
    assert client.post(f"{U}/terrain/apply", json={}).status_code == 409
    assert client.post(f"{U}/terrain/ue", json={}).status_code == 409
    assert client.post(f"{U}/terrain/bogus", json={}).status_code == 422
    j = _wait(client, client.post(f"{U}/terrain/apply", json={"confirm": True}).json())
    assert j["status"] == "done" and j["result"] == "applied" and j["lane"] == "cpu"
    monkeypatch.setattr(tb, "build", lambda st: {"ranges": [{"name": "r", "height_factor": 1}], "cells_over_sky_after_erosion": 0, "max_z": 9})
    assert _wait(client, client.post(f"{U}/terrain/build", json={}).json())["result"]["max_z"] == 9


def test_unreal_endpoints_with_ue_functions_patched(client, store, monkeypatch):  # noqa: F811
    from asset_pipeline import layout_history, look
    rep = {"ok": True, "assets": {"imported": [1, 2], "skipped": [3]}, "actors": {"created": [1], "kept_transform": [], "reset": [1, 2, 3]},
           "sample_errors": [], "warnings": ["w"], "seconds": 1.5, "orphans": [], "pruned": []}
    calls = []
    monkeypatch.setattr(ue, "import_", lambda st, **kw: calls.append(kw) or rep)
    monkeypatch.setattr(ue, "backups", lambda st: [])
    monkeypatch.setattr(ue, "editor_running", lambda up: False)
    assert client.post(f"{U}/ue/import", json={"reset_layout": True}).status_code == 409        # no backup yet
    assert client.post(f"{U}/ue/import", json={"prune": True}).status_code == 409
    assert client.post(f"{U}/ue/import", json={"only_stale": True}).status_code == 400
    j = _wait(client, client.post(f"{U}/ue/import", json={}).json())                              # plain import needs none
    assert j["result"]["imported"] == 2 and j["result"]["reset"] == 3 and calls[-1]["reset_layout"] is False
    monkeypatch.setattr(ue, "backups", lambda st: [Path("20261016-1")])
    monkeypatch.setattr(layout_history, "check", lambda st: {"current": ["a"], "stale": ["s2", "s1"], "hand": ["h"]})
    j = _wait(client, client.post(f"{U}/ue/import", json={"reset_layout": True, "only_stale": True, "prune": True, "expect": 4}).json())
    assert j["status"] == "done" and calls[-1]["reset_only"] == ["s1", "s2"] and calls[-1]["prune"] and calls[-1]["expect"] == 4
    assert calls[-1]["confirm"] is None                                                           # headless: never prunes past the check
    monkeypatch.setattr(ue, "editor_running", lambda up: True)
    monkeypatch.setattr(ue, "uproject", lambda st: Path(__file__))                                # an existing file
    assert client.post(f"{U}/ue/import", json={}).status_code == 409                              # editor open
    monkeypatch.setattr(ue, "uproject", lambda st: Path("/nonexistent.uproject"))
    assert client.get(f"{U}/ue/stale").status_code == 400                                         # no UE project
    monkeypatch.setattr(ue, "uproject", lambda st: Path(__file__))
    assert client.get(f"{U}/ue/stale").json() == {"current": 1, "stale": ["s1", "s2"], "hand": ["h"]}
    monkeypatch.setattr(ue, "render_shots", lambda st, shots=None: {"shots": [{"file": "a.png"}], "failed": [], "got": shots})
    assert _wait(client, client.post(f"{U}/ue/render", json={"shots": ["med-a"]}).json())["result"]["got"] == ["med-a"]
    monkeypatch.setattr(ue, "pull_layout", lambda st: {"actors": [1, 2]})
    assert _wait(client, client.post(f"{U}/ue/pull-layout").json())["result"] == {"actors": 2}
    monkeypatch.setattr(look, "run", lambda st, name: {"record": {"render": name + ".png", "ok": True}})
    (store.root / "site" / "look_tests").mkdir(parents=True)
    (store.root / "site" / "look_tests" / "gate.json").write_text("{}")
    (store.root / "site" / "look_tests" / "defaults.json").write_text("{}")
    assert client.get(f"{U}/ue/looks").json() == {"looks": ["gate"]}
    assert _wait(client, client.post(f"{U}/ue/look", json={"name": "gate"}).json())["result"]["render"] == "gate.png"
    monkeypatch.setattr(ue, "init", lambda st: Path("/x/P.uproject"))
    assert _wait(client, client.post(f"{U}/ue/init").json())["result"] == "/x/P.uproject"


def test_vlm_up_and_down_are_jobs(client, monkeypatch):
    seen = []
    monkeypatch.setattr(vlm, "up", lambda model=None, gpu=0, idle_unload=None, log=print: seen.append(("up", model, gpu))
                        or {"model": "8b", "gpu": gpu, "url": "http://x"})
    monkeypatch.setattr(vlm, "down", lambda gpu=None, wait_s=900, force=False, log=print: seen.append(("down", force)) or "stopped")
    r = client.post("/api/vlm/up", json={"model": "8b"}).json()
    assert r["lane"] == "gpu0" and _wait(client, r)["result"]["model"] == "8b"
    assert _wait(client, client.post("/api/vlm/down", json={"force": True}).json())["result"] == {"result": "stopped"}
    assert seen == [("up", "8b", None), ("down", True)]        # None: vlm.up takes the configured GPU


def test_demo_runs_the_script_as_a_job(client, store, monkeypatch):  # noqa: F811
    ran = []

    def fake(args, note, log, cancel_hook):
        ran.append(args)
        note("=== greybox")
        return 0
    monkeypatch.setattr(tools_api, "_run_demo_script", fake)
    assert client.post("/api/demo", json={"mode": "starter", "slug": "Bad Slug"}).status_code == 400
    assert client.post("/api/demo", json={"mode": "starter", "slug": "demo"}).status_code == 409        # exists
    j = _wait(client, client.post("/api/demo", json={"mode": "starter", "slug": "fresh"}).json())
    assert j["status"] == "done" and ran == [["starter", "fresh"]]
    assert client.post("/api/demo", json={"mode": "area", "slug": "demo", "shot": "med-a"}).status_code == 400
    _wait(client, client.post("/api/demo", json={"mode": "area", "slug": "demo", "shot": "med-a", "entries": ["hall"]}).json())
    assert ran[-1] == ["area", "demo", "med-a", "hall"]
    monkeypatch.setattr(tools_api, "_run_demo_script", lambda *a: 3)
    assert _wait(client, client.post("/api/demo", json={"mode": "starter", "slug": "fresh2"}).json())["status"] == "error"
    monkeypatch.setattr(ui_app, "_comfy_up", lambda: False)
    c2 = TestClient(ui_app.create_app(JobQueue()))
    assert c2.post("/api/demo", json={"mode": "starter", "slug": "fresh3"}).status_code == 409


def test_launcher_files_and_script(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("AP_ROOT", str(tmp_path / "root"))
    out = launcher.install(home=tmp_path, ap_bin="/venv/bin/ap", update_db=False)
    d = Path(out["desktop"]).read_text()
    assert out["desktop"] == str(tmp_path / ".local/share/applications/terraformer.desktop")
    for line in ("Name=Terraformer", f"Exec={tmp_path}/.local/bin/terraformer-launch", "Categories=Graphics;3DGraphics;", "Terminal=false",
                 f"Icon={tmp_path}/.local/share/icons/hicolor/scalable/apps/terraformer.svg"):
        assert line in d
    assert Path(out["icon"]).read_text().lstrip().startswith("<?xml") and "<svg" in Path(out["icon"]).read_text()
    s = Path(out["script"]).read_text()
    assert Path(out["script"]).stat().st_mode & 0o111
    assert "PORT=8700" in s and "AP_BIN=/venv/bin/ap" in s and f"AP_ROOT={tmp_path}/root" in s
    assert "/api/version" in s and "setsid nohup" in s and "logs/ui.log" in s and "seq 1 60" in s and "xdg-open" in s
    assert launcher.status(tmp_path)["installed"]
    assert len(launcher.remove(home=tmp_path, update_db=False)) == 3 and not launcher.status(tmp_path)["installed"]


def test_launcher_endpoint_and_cli_use_the_home(tmp_path, monkeypatch, client, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda x: None)          # no update-desktop-database
    assert not client.get("/api/launcher").json()["installed"]
    client.post("/api/launcher/install")
    assert (tmp_path / ".local/share/applications/terraformer.desktop").is_file() and client.get("/api/launcher").json()["installed"]
    assert len(client.post("/api/launcher/remove").json()["removed"]) == 3
    from asset_pipeline import cli
    cli.main(["launcher", "install"])
    assert "desktop:" in capsys.readouterr().out and (tmp_path / ".local/bin/terraformer-launch").is_file()
    cli.main(["launcher", "remove"])
    assert not (tmp_path / ".local/bin/terraformer-launch").exists()


def test_menu_actions_and_scripts_are_wired():
    html = (STATIC / "index.html").read_text()
    shell = (STATIC / "shell.js").read_text()
    for f in ("tools.js", "areas.js", "world.js"):
        assert f'src="/static/{f}"' in html
    defined = set(re.findall(r"^\s*(\w+): \{run:", shell, re.M))
    for f in ("tools.js", "areas.js", "world.js", "library.js", "workflow.js", "unreal.js"):
        src = (STATIC / f).read_text()
        defined |= set(re.findall(r"(\w+): \{run:", src))
    defined |= {"terrain_" + k for k in "analyse build outer apply landscape ue".split()}
    used = set()
    for f in ("tools.js", "world.js"):
        for m in re.finditer(r'\["([^"]+)", "(\w+)"\]', (STATIC / f).read_text()):
            used.add(m.group(2))
    assert used and used <= defined | {"L", "W", "U"}, used - defined
    world = (STATIC / "world.js").read_text()
    for item in ("Build all…", "TRELLIS all…", "Instances…", "Pending slots…", "Silhouette check…", "Import…", "Stale report…",
                 "Render shots…", "Look scene…", "Pull layout", "Generate frames…", "Lock a shot…", "Vegetation slots…", "Plan overlap…"):
        assert f'"{item}"' in world, item
    assert '"Areas…"' in world and '"Preflight…"' in (STATIC / "tools.js").read_text()


def test_commit_project_commits_in_the_projects_own_repo(client, store):  # noqa: F811
    import subprocess
    assert client.get(f"{U}/git").json() == {"repo": False, "changes": []}
    assert client.post(f"{U}/git/commit", json={"message": "x"}).status_code == 400      # not a repo of its own
    git = lambda *a: subprocess.run(["git", "-C", str(store.root), *a], capture_output=True, text=True)  # noqa: E731
    git("init", "-q"); git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
    git("config", "core.hooksPath", "/dev/null")
    g = client.get(f"{U}/git").json()
    assert g["repo"] and any("project.json" in c for c in g["changes"])
    assert client.post(f"{U}/git/commit", json={"message": "  "}).status_code == 400
    r = client.post(f"{U}/git/commit", json={"message": "day one"}).json()
    assert r["committed"].endswith("day one") and client.get(f"{U}/git").json()["changes"] == []
    assert client.post(f"{U}/git/commit", json={"message": "again"}).status_code == 409       # nothing to commit


def test_band_materials_are_read_once_on_gpu0(store, monkeypatch):  # noqa: F811
    from asset_pipeline import building_proc, kit_proc
    from asset_pipeline.project import write_json
    seen = []
    monkeypatch.setattr(building_proc, "band_types", lambda st, b, log=None: seen.append(1) or b["measured"].update(
        materials={k: {"type": "marble", "why": ""} for k in ("base", "wall", "top")}))
    sp = kit_proc.spec_path(store, "hall")
    write_json(sp, {"kind": "building", "form": "cube", "view": "v.png", "measured": {}})
    assert building_proc.read_band_materials(store, "hall") is True
    assert building_proc.read_band_materials(store, "hall") is False and seen == [1]   # kept in the brief
    write_json(sp, {"kind": "building", "form": "cube", "view": "v.png", "measured": {}, "surface": "patch"})
    assert building_proc.read_band_materials(store, "hall") is False                 # sheet tiles: no VLM


def test_unreal_steps_state_and_a_look_preset_from_the_approved_shot(client, store, monkeypatch):  # noqa: F811
    """The Unreal tab's numbered steps read /unreal: last import, shot renders, look presets and the
    approved shots; with no preset, one is created for a shot locked on a frame (never a greybox lock)."""
    import json as _json
    from asset_pipeline.stages import s0_frames, s7_export
    monkeypatch.setattr(s0_frames, "locked", lambda st: {"med-a": "frames/med-a/batch_001/f.png", "med-b": "greybox"})
    monkeypatch.setattr(s0_frames, "mood_shots", lambda st: set())
    monkeypatch.setattr(ue, "uproject", lambda st: Path("/nonexistent.uproject"))
    u = client.get(f"{U}/unreal").json()
    assert u["imported"] is None and u["renders"] is None and u["looks"] == []
    assert u["approved_shots"] == (["med-a"] if u["greybox"] else [])
    work = s7_export.export_dir(store) / "ue"
    (work / "shots").mkdir(parents=True)
    (work / "ap_import_report.json").write_text(_json.dumps({"ok": True}))
    (work / "shots" / "med-a.png").write_bytes(b"")
    u = client.get(f"{U}/unreal").json()
    assert u["imported"]["ok"] is True and u["renders"]["count"] == 1
    assert client.post(f"{U}/ue/looks/new", json={"shot": "med-b"}).status_code == 400            # greybox lock: no frame
    assert client.post(f"{U}/ue/looks/new", json={"shot": "nope"}).status_code == 400
    assert client.post(f"{U}/ue/looks/new", json={"shot": "med-a"}).json() == {"name": "med-a"}
    assert _json.loads((store.root / "site/look_tests/med-a.json").read_text()) == {"shot": "med-a"}   # no class map: no from_frame
    assert client.post(f"{U}/ue/looks/new", json={"shot": "med-a"}).status_code == 409
    assert client.get(f"{U}/unreal").json()["looks"] == [{"name": "med-a", "shot": "med-a", "runs": 0}]
    (store.root / "site/reconcile/med-a").mkdir(parents=True)
    (store.root / "site/reconcile/med-a/classes.png").write_bytes(b"")
    from asset_pipeline import look
    assert look.new_preset(store, "med-a", "second") == "second"
    assert _json.loads((store.root / "site/look_tests/second.json").read_text())["backdrop"] == {"from_frame": True}
