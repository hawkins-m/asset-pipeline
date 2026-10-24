"""ap doctor and the app's feature switches. ComfyUI, GPUs, Blender and the local installs are faked:
nothing here probes the machine."""
import time

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import cli, config, doctor, gpu, preflight, setup
from asset_pipeline.jobs import JobQueue
from asset_pipeline.ui import app as ui_app
from asset_pipeline.ui import setup_api

STATS = {"system": {"comfyui_version": "0.24.0"}, "devices": []}


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Everything present except what a test takes away."""
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    config.save_user({"paths": {"ap_root": str(tmp_path)}, "unreal": {"editor_cmd": ""}})
    monkeypatch.setattr(gpu, "list_gpus", lambda: [{"index": 0, "name": "A", "vram_gb": 24}, {"index": 1, "name": "B", "vram_gb": 24}])
    monkeypatch.setattr(setup, "_probe_comfy", lambda url, timeout=1.0: STATS)
    monkeypatch.setattr(setup, "proxy_installed", lambda: tmp_path / "proxy")
    monkeypatch.setattr(doctor, "check_comfy_models", lambda url: doctor._c("comfy_models", "ComfyUI models", True, "all 5"))
    monkeypatch.setattr(setup, "_test_blender", lambda v, a: {"ok": True, "warn": False, "message": "Works: Blender 5.2"})
    ok = [preflight.Check("Vision LLM (Qwen3-VL)", True, "usable: 8b"), preflight.Check("Segmentation/depth (x)", True, "ok"),
          preflight.Check("TRELLIS.2 (y)", True, "ok", False)]
    monkeypatch.setattr(preflight, "local_checks", lambda: ok)

    class Client:
        def health(self):
            return STATS
    monkeypatch.setattr("asset_pipeline.comfy.client.connect", lambda timeout_s=None: Client())
    return tmp_path


def _by(rep):
    return {c["id"]: c for c in rep["checks"]}


def test_everything_present_but_unreal(box):
    rep = doctor.report()
    assert rep["missing"] == 0 and rep["optional_missing"] == 1
    assert _by(rep)["unreal"]["level"] == "fail" and _by(rep)["unreal"]["optional"]
    f = rep["features"]
    assert not f["unreal"]["ok"] and "not set up" in f["unreal"]["why"] and "ap setup" in f["unreal"]["why"]
    assert all(v["ok"] for k, v in f.items() if k != "unreal")


def test_comfyui_not_running_is_a_note_while_it_can_be_started(box, monkeypatch, tmp_path):
    monkeypatch.setattr(setup, "_probe_comfy", lambda url, timeout=1.0: None)
    launcher = tmp_path / "run.sh"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    config.save_user({"comfyui": {"service": {"launch": f"{launcher} --port {{port}}"}}})
    rep = doctor.report()
    assert _by(rep)["comfy"]["level"] == "warn" and rep["features"]["comfy"]["ok"]
    config.save_user({"comfyui": {"service": {"launch": "/nowhere/python main.py --port {port}"}}})
    rep = doctor.report()
    assert _by(rep)["comfy"]["level"] == "fail" and not rep["features"]["comfy"]["ok"]


def test_sdk_needs_the_proxy_installed_but_not_running(box, monkeypatch):
    monkeypatch.setattr(setup, "proxy_installed", lambda: None)
    rep = doctor.report()
    assert _by(rep)["comfy_proxy"]["level"] == "fail" and "comfy_api_proxy.sh install" in _by(rep)["comfy_proxy"]["fix"]
    assert _by(rep)["comfy"]["level"] == "ok" and not rep["features"]["comfy"]["ok"]
    from asset_pipeline.comfy.client import ComfyUnavailable

    class Down:
        def health(self):
            raise ComfyUnavailable("Comfy API proxy not reachable at http://127.0.0.1:8190 (refused). Start it with x")
    monkeypatch.setattr(setup, "proxy_installed", lambda: box / "proxy")
    monkeypatch.setattr("asset_pipeline.comfy.client.connect", lambda timeout_s=None: Down())
    rep = doctor.report()
    assert _by(rep)["comfy"]["level"] == "warn" and rep["features"]["comfy"]["ok"]   # Start ComfyUI brings it up


def test_missing_gpu_and_vision_model(box, monkeypatch):
    monkeypatch.setattr(gpu, "list_gpus", lambda: [{"index": 0, "name": "A", "vram_gb": 8}])
    monkeypatch.setattr(preflight, "local_checks", lambda: [
        preflight.Check("Vision LLM (Qwen3-VL)", False, "none installed"), preflight.Check("Segmentation/depth (x)", True, "ok"),
        preflight.Check("TRELLIS.2 (y)", False, "missing", False)])
    rep = doctor.report()
    assert "doesn't exist" in _by(rep)["gpus"]["detail"] and rep["missing"] == 2
    assert not rep["features"]["vlm"]["ok"] and not rep["features"]["trellis"]["ok"] and rep["features"]["blender"]["ok"]


def test_ap_doctor_exit_code_and_text(box, monkeypatch, capsys):
    cli.main(["doctor"])                                    # only Unreal (optional) missing: exit 0
    out = capsys.readouterr().out
    assert "Everything required is in place." in out and "Off until fixed: The Unreal tab" in out
    monkeypatch.setattr(setup, "_test_blender", lambda v, a: {"ok": False, "warn": False, "message": "blender wasn't found."})
    with pytest.raises(SystemExit) as e:
        cli.main(["doctor"])
    assert e.value.code == 1 and "fix: Install Blender" in capsys.readouterr().out


def test_api_doctor_is_lazy_and_refreshes_after_a_save(box, monkeypatch):
    setup_api._doc.update(report=None, running=False)
    runs = []
    real = doctor.report
    monkeypatch.setattr(doctor, "report", lambda: runs.append(1) or real())
    c = TestClient(ui_app.create_app(JobQueue()))
    assert not runs                                         # nothing runs until the page asks
    r = c.get("/api/doctor").json()
    for _ in range(50):
        if setup_api._doc["report"]:
            break
        time.sleep(0.05)
    r = c.get("/api/doctor").json()
    assert r.get("pending") is None and "features" in r and runs == [1]
    r = c.get("/api/doctor?refresh=1").json()
    assert runs == [1, 1] and r["missing"] == 0
    setup_api._doc.update(report=None, running=False)
