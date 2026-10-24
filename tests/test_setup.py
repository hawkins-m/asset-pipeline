"""Setup (ap setup, the Setup screen): first run, detection, plain-words tests, saving. No test here
starts ComfyUI, a GPU job or a paid service: probes and GPU tools are faked."""
import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import cli, config, gpu, setup
from asset_pipeline.jobs import JobQueue
from asset_pipeline.ui import app as ui_app

GPUS = [{"index": 0, "name": "Card A", "vram_gb": 24.0}, {"index": 1, "name": "Card B", "vram_gb": 16.0}]


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A fresh user: no config, storage under tmp, two fake GPUs, no ComfyUI answering."""
    monkeypatch.delenv("AP_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr(gpu, "list_gpus", lambda: GPUS)
    monkeypatch.setattr(setup, "_probe_comfy", lambda url, timeout=1.0: None)
    monkeypatch.setattr(setup, "_unreal_candidates", lambda: [])
    monkeypatch.setattr(setup, "_comfy_models_dir", lambda url: "")
    for k in config.SECRET_KEYS:
        monkeypatch.delenv(k, raising=False)
    config.backends.cache_clear()
    return tmp_path


def test_first_run_until_saved(machine):
    assert setup.first_run()
    setup.save({"storage": str(machine / "store"), "gpu_vision": "0", "gpu_comfy": "1"})
    assert not setup.first_run()
    assert config.ap_root() == machine / "store" and (machine / "store" / "projects").is_dir()
    assert config.get("jobs.comfy_gpu") == 1 and config.get("comfyui.service.gpu") == "1"


def test_state_prefills_found_values_and_never_returns_keys(machine, monkeypatch):
    monkeypatch.setattr(setup, "_blender_candidates", lambda: ["/opt/blender-5/blender"])
    config.save_secret("GEMINI_API_KEY", "a-secret-key-0123456789")
    st = setup.state()
    items = {i["id"]: i for i in st["items"]}
    assert st["first_run"] and st["gpus"] == GPUS
    assert items["blender"]["value"] == "" and items["blender"]["detected"] == "/opt/blender-5/blender"
    assert "a-secret-key" not in json.dumps(st)
    assert not items["gemini_key"]["shown"]                      # the defaults are open weights: no key asked
    assert items["sdk_url"]["shown"] and items["comfy_backend"]["value"] == "sdk"


def test_paid_keys_are_asked_only_when_a_default_needs_them(machine):
    config.save_user({"defaults": {"llm": "claude", "backends": {"frames": "gemini"}}})
    items = {i["id"]: i for i in setup.state()["items"]}
    assert items["anthropic_key"]["shown"] and items["gemini_key"]["shown"]


def test_tests_answer_in_plain_words(machine, monkeypatch):
    r = setup.test("storage", str(machine / "new"))
    assert r["ok"] and "will be created" in r["message"]
    assert not setup.test("storage", "relative/path")["ok"]
    assert not setup.test("comfy_url", "not a url")["ok"]
    r = setup.test("comfy_url", "http://127.0.0.1:1")
    assert not r["ok"] and "Start ComfyUI" in r["message"]
    r = setup.test("unreal", "")
    assert r["ok"] and r["warn"] and "Unreal tab" in r["message"]
    assert "doesn't exist" in setup.test("unreal", str(machine / "nope"))["message"]
    assert setup.test("gpu_vision", "1", {"gpu_comfy": "0"})["message"] == "Works: GPU 1 is Card B (16 GB)."
    r = setup.test("gpu_vision", "1", {"gpu_comfy": "1"})
    assert r["ok"] and r["warn"] and "share" in r["message"]
    assert "no GPU 5" in setup.test("gpu_comfy", "5")["message"]
    r = setup.test("comfy_api_key", "")
    assert r["ok"] and r["warn"]                                 # optional: a local ComfyUI needs none
    r = setup.test("gemini_key", "x" * 30)
    assert r["ok"] and "Not checked online" in r["message"]     # never calls the paid service


def test_unreal_version_from_the_install(machine):
    eng = machine / "UE" / "Engine"
    exe = eng / "Binaries" / "Linux" / "UnrealEditor-Cmd"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (eng / "Build").mkdir()
    (eng / "Build" / "Build.version").write_text('{"MajorVersion": 5, "MinorVersion": 8, "PatchVersion": 3}')
    r = setup.test("unreal", str(machine / "UE"))                # the engine folder works too
    assert r["ok"] and not r["warn"] and "5.8.3" in r["message"]


def test_blender_test_runs_the_version_only(machine, tmp_path):
    fake = tmp_path / "blender"
    fake.write_text("#!/bin/sh\necho 'Blender 4.1.0'\n")
    fake.chmod(0o755)
    assert "too old" in setup.test("blender", str(fake))["message"]
    fake.write_text("#!/bin/sh\necho 'Blender 5.2.2 LTS'\n")
    assert setup.test("blender", str(fake))["message"] == f"Works: Blender 5.2.2 LTS ({fake})."


def test_sdk_test_says_what_is_missing(machine):
    r = setup.test("sdk_url", "http://127.0.0.1:8190")
    assert not r["ok"] and "comfy_api_proxy.sh install" in r["message"]


def test_launch_command_needs_its_script(machine, tmp_path):
    (tmp_path / "comfy").mkdir()
    r = setup._check_launch("python3 main.py --port {port}", {"comfy_cwd": str(tmp_path / "comfy")})
    assert not r["ok"] and "main.py isn't in" in r["message"]
    (tmp_path / "comfy" / "main.py").write_text("")
    assert setup._check_launch("python3 main.py --port {port}", {"comfy_cwd": str(tmp_path / "comfy")})["ok"]


def test_save_removes_an_emptied_optional_item(machine):
    setup.save({"unreal": "/ue/UnrealEditor-Cmd"})
    assert config.get("unreal.editor_cmd") == "/ue/UnrealEditor-Cmd"
    setup.save({"unreal": ""})
    assert config.get("unreal.editor_cmd") == ""                 # the repo default again


def test_routes(machine, monkeypatch):
    c = TestClient(ui_app.create_app(JobQueue()))
    assert c.get("/api/setup/first-run").json() == {"first_run": True}
    assert len(c.get("/api/setup").json()["items"]) == len(setup.ITEMS)
    r = c.post("/api/setup/test", json={"id": "gpu_comfy", "value": "1", "values": {"gpu_vision": "0"}}).json()
    assert r["ok"] and r["id"] == "gpu_comfy"
    assert c.post("/api/setup/test", json={"id": "nope"}).status_code == 400
    assert c.post("/api/setup/save", json={"values": {"bogus": "1"}}).status_code == 400
    r = c.post("/api/setup/save", json={"values": {"storage": str(machine / "s"), "gpu_comfy": "0"},
                                        "secrets": {"COMFY_API_KEY": "cloud-key-0123456789"}})
    assert r.status_code == 200 and c.get("/api/setup/first-run").json() == {"first_run": False}
    assert stat.S_IMODE(config.secrets_toml().stat().st_mode) == 0o600
    assert "cloud-key" not in config.user_toml().read_text()


def test_ap_setup_yes_writes_the_found_values(machine, monkeypatch, capsys):
    monkeypatch.setattr(setup, "_blender_candidates", lambda: [])
    cli.main(["setup", "--check"])
    assert not config.user_toml().exists() and "Nothing written" in capsys.readouterr().out
    cli.main(["setup", "--yes", "--set", f"storage={machine / 'cli-store'}", "--set", "gpu_comfy=0"])
    out = capsys.readouterr().out
    assert "Wrote" in out and config.ap_root() == machine / "cli-store" and config.get("jobs.comfy_gpu") == 0
    with pytest.raises(SystemExit):
        cli.main(["setup", "--yes", "--set", "nope=1"])


def test_models_item_names_the_exact_download_command(machine, monkeypatch):
    from asset_pipeline import models
    monkeypatch.setattr(models, "missing", lambda feature=None, required_only=True: [models.get("flux1-redux-dev"), models.get("vlm-32b")])
    r = setup.test("models")
    assert not r["ok"] and "ap models download flux1-redux-dev vlm-32b" in r["message"] and "Gated: flux1-redux-dev" in r["message"]
    monkeypatch.setattr(models, "missing", lambda feature=None, required_only=True: [])
    assert setup.test("models")["ok"]


def test_comfy_models_folder_counts_what_is_there(machine, tmp_path):
    d = tmp_path / "comfy-models"
    (d / "checkpoints").mkdir(parents=True)
    r = setup.test("comfy_models_dir", str(d))
    assert r["ok"] and r["warn"] and "0 of 5" in r["message"] and "ap models download flux1-dev-fp8" in r["message"]
    assert not setup.test("comfy_models_dir", str(tmp_path / "nope"))["ok"]
