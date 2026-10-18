"""ComfyUI service control (ap comfy start|stop|status, the app's Start/Stop/Restart) and the
gate that holds ComfyUI jobs until it answers. A tiny stand-in server plays ComfyUI; no GPU."""
import socket
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

from asset_pipeline import gpu as gpu_mod
from asset_pipeline.comfy import service
from asset_pipeline.jobs import JobQueue

FAKE = '''
import http.server, json, sys, time
print("Total VRAM 32000 MB", flush=True)
print("Import times for custom nodes:", flush=True)
if "--fail" in sys.argv:
    print("Traceback (most recent call last):", flush=True)
    print("RuntimeError: boom while loading", flush=True)
    sys.exit(3)
time.sleep(0.6)
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200 if self.path == "/system_stats" else 404)
        self.end_headers(); self.wfile.write(json.dumps({"devices": []}).encode())
    def log_message(self, *a): pass
port = int(sys.argv[sys.argv.index("--port") + 1])
print("Starting server", flush=True)
http.server.HTTPServer(("127.0.0.1", port), H).serve_forever()
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _settings(tmp_path, launch: str, port: int | None = None) -> dict:
    port = port or _free_port()
    return {"launch": launch, "cwd": str(tmp_path), "gpu": "", "start_timeout_s": 20, "stop_timeout_s": 5,
            "url": f"http://127.0.0.1:{port}", "port": port, "host": "127.0.0.1", "backend": "httpx",
            "sdk_url": "http://127.0.0.1:1", "log": tmp_path / "logs" / "comfyui.log",
            "pidfile": tmp_path / "logs" / "comfyui.json"}


@pytest.fixture
def fake(tmp_path):
    (tmp_path / "fake_comfy.py").write_text(FAKE)
    return f"{sys.executable} fake_comfy.py --port {{port}}"


def test_settings_take_the_port_from_the_url_and_fill_the_launch_command(monkeypatch):
    monkeypatch.setattr(service.config, "backends", lambda: {"comfyui": {
        "url": "http://127.0.0.1:8123", "backend": "httpx",
        "service": {"launch": "~/bin/run.sh --port {port} --api {url}", "gpu": "1"}}})
    s = service.settings()
    assert s["port"] == 8123 and s["gpu"] == "1" and s["start_timeout_s"] == 300
    cmd = service.command(s)
    assert cmd[0].endswith("/bin/run.sh") and not cmd[0].startswith("~")
    assert cmd[1:] == ["--port", "8123", "--api", "http://127.0.0.1:8123"]


def test_launch_env_pins_the_gpu_per_vendor(monkeypatch):
    monkeypatch.setattr(gpu_mod, "vendor", lambda: "amd")
    assert service.launch_env({"gpu": "1"})["HIP_VISIBLE_DEVICES"] == "1"
    monkeypatch.setattr(gpu_mod, "vendor", lambda: "nvidia")      # untested on hardware
    assert service.launch_env({"gpu": 0})["CUDA_VISIBLE_DEVICES"] == "0"
    monkeypatch.delenv("HIP_VISIBLE_DEVICES", raising=False)
    assert "HIP_VISIBLE_DEVICES" not in service.launch_env({"gpu": ""})


def test_start_streams_the_log_waits_for_health_and_stop_ends_it(tmp_path, fake):
    s = _settings(tmp_path, fake)
    lines = []
    st = service.start(log=lambda m: None, on_line=lines.append, s=s)
    try:
        assert st["state"] == "running" and st["managed"] and service.healthy(s)
        assert "Total VRAM 32000 MB" in lines and s["pidfile"].is_file()
        again = service.start(log=lambda m: None, s=s)              # already up: no second process
        assert again["pid"] == st["pid"]
    finally:
        after = service.stop(log=lambda m: None, s=s)
    assert after["state"] == "stopped" and not service.healthy(s) and not s["pidfile"].exists()


def test_start_reports_the_log_errors_when_comfyui_exits(tmp_path, fake):
    s = _settings(tmp_path, fake + " --fail")
    with pytest.raises(RuntimeError) as e:
        service.start(log=lambda m: None, s=s)
    assert "code 3" in str(e.value) and "boom while loading" in str(e.value)


def test_start_explains_a_bad_launch_command(tmp_path):
    s = _settings(tmp_path, "/nonexistent/run_comfy.sh --port {port}")
    with pytest.raises(RuntimeError, match=r"\[comfyui.service\] launch"):
        service.start(log=lambda m: None, s=s)


def test_stop_never_kills_something_else_on_the_port(tmp_path):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    try:
        s = _settings(tmp_path, "unused", port=srv.getsockname()[1])
        if service.listener_pid(s["port"]) is None:
            pytest.skip("ss can't see listening processes here")
        with pytest.raises(RuntimeError, match="not ComfyUI"):
            service.stop(log=lambda m: None, s=s)
    finally:
        srv.close()


def test_gate_refuses_when_down_holds_while_starting_and_fails_with_the_start():
    up = {"v": False}
    gate = service.Gate(ready_fn=lambda: up["v"], poll_s=0.01)
    with pytest.raises(service.ComfyDown):
        gate.check_submit()
    gate.starting()
    gate.check_submit()                               # accepted while a start is under way
    threading.Timer(0.1, lambda: up.update(v=True)).start()
    gate.wait()                                       # returns once ComfyUI answers
    up["v"] = False
    gate.finished("ComfyUI exited with code 1")
    with pytest.raises(RuntimeError, match="did not start: ComfyUI exited"):
        gate.wait()


def test_gated_lane_queues_jobs_until_comfyui_answers():
    up = {"v": False}
    gate = service.Gate(ready_fn=lambda: up["v"], poll_s=0.01)
    q = JobQueue(gates={"comfy": gate})
    with pytest.raises(service.ComfyDown):
        q.submit("frames.generate", "p", lambda: "ran")
    assert q.submit("refs.generate", "p", lambda: "hosted", gated=False)   # e.g. a Gemini image stage
    gate.starting()
    job = q.submit("frames.generate", "p", lambda: "ran")
    time.sleep(0.1)
    assert q.get(job.id).status == "running" and "waiting for ComfyUI" in q.get(job.id).note
    up["v"] = True
    for _ in range(200):
        if q.get(job.id).status == "done":
            break
        time.sleep(0.01)
    assert q.get(job.id).result == "ran" and q.get(job.id).note == ""
    assert q.submit("x", "p", lambda: 1, lane="cpu")                    # other lanes never wait


def test_app_answers_409_comfy_down_and_runs_start_as_a_service_job(monkeypatch):
    from asset_pipeline.ui import app as ui_app
    up = {"v": False}
    gate = service.Gate(ready_fn=lambda: up["v"], poll_s=0.01)
    q = JobQueue(gates={"comfy": gate})
    client = TestClient(ui_app.create_app(q, comfy_gate=gate))

    def fake_start(log, on_line, cancel):
        on_line("Total VRAM 32000 MB")
        on_line("Cannot import custom_nodes/broken: ModuleNotFoundError")
        up["v"] = True
        return {"state": "running", "url": "http://x", "pid": 1}
    monkeypatch.setattr(service, "start", fake_start)

    @client.app.post("/api/test/comfy-job")       # a route that submits to the comfy lane
    def comfy_job():
        return q.submit("frames.generate", "", lambda: "ran").public()

    r = client.post("/api/test/comfy-job")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "comfy_down"
    job = client.post("/api/comfy/start").json()
    assert job["kind"] == "comfy.start" and job["lane"] == "service" and job["project"] == ""
    for _ in range(200):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.01)
    assert j["status"] == "done" and j["result"]["warnings"] == ["Cannot import custom_nodes/broken: ModuleNotFoundError"]
    assert client.post("/api/test/comfy-job").status_code == 200


def test_a_failed_start_is_a_job_error_with_the_log(monkeypatch):
    from asset_pipeline.ui import app as ui_app
    gate = service.Gate(ready_fn=lambda: False, poll_s=0.01)
    client = TestClient(ui_app.create_app(JobQueue(gates={"comfy": gate}), comfy_gate=gate))

    def boom(log, on_line, cancel):
        raise RuntimeError("ComfyUI exited with code 1 during startup:\nRuntimeError: no GPU")
    monkeypatch.setattr(service, "start", boom)
    job = client.post("/api/comfy/start").json()
    for _ in range(200):
        j = client.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "error"):
            break
        time.sleep(0.01)
    assert j["status"] == "error" and "no GPU" in j["error"]
    assert gate.state == "failed" and "code 1" in gate.error


def test_only_a_comfyui_command_line_counts_as_comfyui():
    assert service.looks_like_comfy(["python", "/x/ComfyUI-app/main.py", "--listen", "0.0.0.0"])
    assert service.looks_like_comfy(["/home/u/.local/bin/comfyui", "--port", "8188"])
    # a test run or an editor with "comfy" in a path is not ComfyUI (this test file is one)
    assert not service.looks_like_comfy(["python", "-m", "pytest", "tests/test_comfy_service.py"])
    assert not service.looks_like_comfy(["code", "/home/u/comfy-notes/main.txt"])


def test_log_lines_reach_the_jobs_panel_without_colour_codes(tmp_path):
    log = tmp_path / "comfyui.log"
    log.write_bytes(b"\x1b[32m[INFO]\x1b[0m Enabled pinned memory\n\x1b[32m[INFO]\x1b[0m\nhalf a li")
    got = []
    pos = service._stream(log, 0, got.append)
    assert got == ["[INFO] Enabled pinned memory"] and pos < log.stat().st_size     # the partial line waits
