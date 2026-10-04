import json

import httpx
import pytest

from asset_pipeline.comfy.client import ComfyClient, ComfyError, ComfyUnavailable
from asset_pipeline.imagegen.base import BackendCapabilityError, GenRequest
from asset_pipeline.imagegen.comfyui import ComfyUIBackend
from asset_pipeline.schema import StyleAnchor


class FakeComfy:
    """Minimal ComfyUI HTTP API: /prompt, /history, /view, /system_stats."""

    def __init__(self, fail_validation=False, fail_execution=False):
        self.fail_validation = fail_validation
        self.fail_execution = fail_execution
        self.queued = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {"comfyui_version": "0.24.0"}, "devices": []})
        if path == "/prompt":
            if self.fail_validation:
                return httpx.Response(400, json={"error": "bad", "node_errors": {"3": "x"}})
            self.queued.append(json.loads(request.content)["prompt"])
            return httpx.Response(200, json={"prompt_id": "p1"})
        if path == "/history/p1":
            self.polls += 1
            if self.polls < 2:  # first poll: still running
                return httpx.Response(200, json={})
            status = {"status_str": "error" if self.fail_execution else "success",
                      "completed": not self.fail_execution, "messages": []}
            n = self.queued[-1]["5"]["inputs"]["batch_size"]
            images = [{"filename": f"r_{i}.png", "subfolder": "", "type": "temp"} for i in range(n)]
            return httpx.Response(200, json={"p1": {"status": status,
                                                    "outputs": {"9": {"images": images}}}})
        if path == "/view":
            return httpx.Response(200, content=b"PNG:" + request.url.params["filename"].encode())
        return httpx.Response(404)


def make_client(fake):
    return ComfyClient("http://fake", timeout_s=5, transport=httpx.MockTransport(fake.handler))


def test_backend_generates_batch_and_binds_request(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeComfy()
    backend = ComfyUIBackend(make_client(fake), {"t2i": "flux_t2i"})
    res = backend.generate(GenRequest(prompt="a red cube", n=3, seed=5, width=768, height=512),
                           tmp_path, prefix="t")
    assert [r.path.name for r in res] == ["t_000.png", "t_001.png", "t_002.png"]
    assert res[1].path.read_bytes() == b"PNG:r_1.png"
    g = fake.queued[0]
    assert g["6"]["inputs"]["text"] == "a red cube"
    assert g["3"]["inputs"]["seed"] == 5
    assert (g["5"]["inputs"]["width"], g["5"]["inputs"]["height"]) == (768, 512)


def test_validation_error_surfaces(tmp_path):
    backend = ComfyUIBackend(make_client(FakeComfy(fail_validation=True)), {"t2i": "flux_t2i"})
    with pytest.raises(ComfyError, match="rejected"):
        backend.generate(GenRequest(prompt="x"), tmp_path)


def test_execution_error_surfaces(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    backend = ComfyUIBackend(make_client(FakeComfy(fail_execution=True)), {"t2i": "flux_t2i"})
    with pytest.raises(ComfyError, match="execution failed"):
        backend.generate(GenRequest(prompt="x"), tmp_path)


def test_anchor_not_silently_ignored(tmp_path):
    backend = ComfyUIBackend(make_client(FakeComfy()), {"t2i": "flux_t2i"})
    with pytest.raises(BackendCapabilityError):
        backend.generate(GenRequest(prompt="x", anchor=StyleAnchor(images=[tmp_path / "a.png"])),
                         tmp_path)


def test_unreachable_server_gives_launch_hint():
    def down(request):
        raise httpx.ConnectError("refused")
    c = ComfyClient("http://127.0.0.1:1", transport=httpx.MockTransport(down))
    with pytest.raises(ComfyUnavailable, match="run_comfy.sh"):
        c.health()
