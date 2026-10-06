"""Local VLM lifecycle (asset_pipeline/vlm.py) with fake servers. No GPU, no models."""
import json
import os
import subprocess

import pytest

from asset_pipeline import vlm
from asset_pipeline.llm.registry import make_llm


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    return tmp_path


def _sleeper() -> subprocess.Popen:
    return subprocess.Popen(["sleep", "60"], start_new_session=True)


def _fake_start(started, fail=()):
    def start(model, gpu, idle):
        if model in fail:
            raise vlm.VLMError(f"{model}: missing weights")
        p = _sleeper()
        started.append(p)
        st = {"model": model, "backend": "llamacpp" if model == "32b" else "transformers",
              "url": "http://127.0.0.1:1", "pid": p.pid, "gpu": gpu}
        (vlm._logs() / "vlm.json").write_text(json.dumps(st))
        return st
    return start


def test_up_uses_default_then_falls_back(root, monkeypatch):
    started, logs = [], []
    monkeypatch.setattr(vlm, "_start", _fake_start(started, fail={"32b"}))
    st = vlm.up(log=logs.append)
    assert st["model"] == "8b" and "falling back" in logs[0]
    vlm.down(force=True)
    monkeypatch.setattr(vlm, "_start", _fake_start(started))
    assert vlm.up()["model"] == "32b"
    assert vlm.up()["pid"] == started[-1].pid          # reused, not restarted
    vlm.down(force=True)


def test_up_refuses_gpu_held_by_trellis(root, monkeypatch):
    monkeypatch.setattr(vlm, "_start", _fake_start([]))
    lock = vlm._logs() / f"trellis-gpu0-{os.getpid()}.lock"   # this test process is "trellis"
    lock.touch()
    with pytest.raises(vlm.VLMError, match="TRELLIS"):
        vlm.up(gpu=0)
    assert vlm.up(gpu=1)["gpu"] == 1                   # the other GPU is free
    vlm.down(force=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    stale = vlm._logs() / f"trellis-gpu1-{dead.pid}.lock"
    stale.touch()
    assert vlm.trellis_jobs(1) == [] and not stale.exists()


def test_down_waits_for_request_and_respects_gpu(root, monkeypatch):
    started = []
    monkeypatch.setattr(vlm, "_start", _fake_start(started))
    vlm.up(gpu=0)
    assert vlm.down(gpu=1) == "running on GPU 0, left alone"
    answers = iter([True, True, False])
    monkeypatch.setattr(vlm, "busy", lambda st: next(answers))
    monkeypatch.setattr(vlm.time, "sleep", lambda s: None)
    logs = []
    assert vlm.down(gpu=0, log=logs.append).startswith("stopped 32b")
    assert "waiting" in logs[0]
    started[0].wait(timeout=5)                          # really terminated
    assert vlm.state() is None and vlm.down() == "not running"


def test_stale_state_is_cleared(root):
    dead = subprocess.Popen(["true"])
    dead.wait()
    (vlm._logs() / "vlm.json").write_text(json.dumps({"model": "32b", "pid": dead.pid}))
    assert vlm.state() is None and not (vlm._logs() / "vlm.json").exists()


def test_local_provider_starts_on_first_use(root, monkeypatch):
    llm = make_llm("local")
    assert llm.name == "local"                         # constructing it starts nothing
    assert vlm.state() is None
    calls = []

    class FakeClient:
        name = "local-32b"

        def json_text(self, *a):
            calls.append(a)
            return "{}"
    monkeypatch.setattr(vlm, "_start", _fake_start([]))
    monkeypatch.setattr(vlm, "client", lambda st: FakeClient())
    assert llm.json_text("s", "p", [], {}) == "{}"
    assert llm.name == "local-32b" and vlm.state()["model"] == "32b"
    vlm.down(force=True)


def test_missing_weights_name_the_install_script(root):
    with pytest.raises(vlm.VLMError, match="install_llamacpp_vlm.sh"):
        vlm._command("32b", vlm._cfg()["models"]["32b"], 0, 600)
