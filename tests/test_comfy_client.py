import json

import httpx
import pytest

from asset_pipeline.comfy.client import ComfyClient, ComfyError, ComfyUnavailable
from asset_pipeline.imagegen.base import BackendCapabilityError, GenRequest
from asset_pipeline.imagegen.comfyui import ComfyUIBackend
from asset_pipeline.schema import LoraRef, StyleAnchor


class FakeComfy:
    """Minimal ComfyUI HTTP API: /prompt, /history, /view, /system_stats."""

    def __init__(self, fail_validation=False, fail_execution=False):
        self.fail_validation = fail_validation
        self.fail_execution = fail_execution
        self.queued = []
        self.uploads = []
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
        if path == "/upload/image":
            name = request.content.split(b'filename="')[1].split(b'"')[0].decode()
            self.uploads.append(name)
            return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
        if path == "/view":
            return httpx.Response(200, content=b"PNG:" + request.url.params["filename"].encode())
        return httpx.Response(404)


WORKFLOWS = {"t2i": "flux_t2i", "t2i_anchor": "flux_t2i_anchor"}


def make_client(fake):
    return ComfyClient("http://fake", timeout_s=5, transport=httpx.MockTransport(fake.handler))


def test_backend_generates_batch_and_binds_request(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeComfy()
    backend = ComfyUIBackend(make_client(fake), WORKFLOWS)
    res = backend.generate(GenRequest(prompt="a red cube", n=3, seed=5, width=768, height=512),
                           tmp_path, prefix="t")
    assert [r.path.name for r in res] == ["t_000.png", "t_001.png", "t_002.png"]
    assert res[1].path.read_bytes() == b"PNG:r_1.png"
    g = fake.queued[0]
    assert g["6"]["inputs"]["text"] == "a red cube"
    assert g["3"]["inputs"]["seed"] == 5
    assert (g["5"]["inputs"]["width"], g["5"]["inputs"]["height"]) == (768, 512)


def test_validation_error_surfaces(tmp_path):
    backend = ComfyUIBackend(make_client(FakeComfy(fail_validation=True)), WORKFLOWS)
    with pytest.raises(ComfyError, match="rejected"):
        backend.generate(GenRequest(prompt="x"), tmp_path)


def test_execution_error_surfaces(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    backend = ComfyUIBackend(make_client(FakeComfy(fail_execution=True)), WORKFLOWS)
    with pytest.raises(ComfyError, match="execution failed"):
        backend.generate(GenRequest(prompt="x"), tmp_path)


def test_refs_not_silently_ignored(tmp_path):
    backend = ComfyUIBackend(make_client(FakeComfy()), WORKFLOWS)
    with pytest.raises(BackendCapabilityError):
        backend.generate(GenRequest(prompt="x", refs=[tmp_path / "a.png"]), tmp_path)


def _anchor_files(tmp_path, k):
    paths = []
    for i in range(k):
        p = tmp_path / f"anchor_{i}.png"
        p.write_bytes(b"png")
        paths.append(p)
    return paths


def test_anchor_uploads_and_chains_with_split_strength(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeComfy()
    backend = ComfyUIBackend(make_client(fake), WORKFLOWS)
    anchor = StyleAnchor(images=_anchor_files(tmp_path, 3), strength=0.6)
    res = backend.generate(GenRequest(prompt="barrel", anchor=anchor), tmp_path / "out")
    g = fake.queued[0]
    assert fake.uploads == ["anchor_0.png", "anchor_1.png", "anchor_2.png"]
    applies = sorted(k for k, n in g.items() if n["class_type"] == "StyleModelApply")
    assert applies == ["24_0", "24_1", "24_2"]
    assert g["24_0"]["inputs"]["conditioning"] == ["11", 0]          # chain starts at guidance
    assert g["24_1"]["inputs"]["conditioning"] == ["24_0", 0]
    assert g["3"]["inputs"]["positive"] == ["24_2", 0]               # sampler reads chain end
    assert all(abs(g[a]["inputs"]["strength"] - 0.2) < 1e-9 for a in applies)
    assert "12" not in g and g["3"]["inputs"]["model"] == ["4", 0]  # no LoRA -> bypassed
    assert res[0].meta["workflow"] == "flux_t2i_anchor"


def test_lora_without_anchor_uses_anchor_template_with_no_style_nodes(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    fake = FakeComfy()
    backend = ComfyUIBackend(make_client(fake), WORKFLOWS)
    backend.generate(GenRequest(prompt="x", lora=LoraRef(file="style.safetensors", strength=0.7)),
                     tmp_path)
    g = fake.queued[0]
    assert g["12"]["inputs"]["lora_name"] == "style.safetensors"
    assert not any(n["class_type"] == "StyleModelApply" for n in g.values())
    assert g["3"]["inputs"]["positive"] == ["11", 0]


def test_missing_anchor_image_is_an_error(tmp_path):
    backend = ComfyUIBackend(make_client(FakeComfy()), WORKFLOWS)
    with pytest.raises(FileNotFoundError):
        backend.generate(GenRequest(prompt="x", anchor=StyleAnchor(images=[tmp_path / "nope.png"])),
                         tmp_path)


def test_unreachable_server_gives_launch_hint():
    def down(request):
        raise httpx.ConnectError("refused")
    c = ComfyClient("http://127.0.0.1:1", transport=httpx.MockTransport(down))
    with pytest.raises(ComfyUnavailable, match="run_comfy.sh"):
        c.health()
