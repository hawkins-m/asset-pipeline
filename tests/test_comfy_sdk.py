"""SdkComfyClient against a fake comfy_sdk client: same behaviour as the httpx ComfyClient."""
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from asset_pipeline import config, jobs, segment
from asset_pipeline.comfy import client as comfy_client
from asset_pipeline.comfy.client import ComfyClient, ComfyError, ComfyUnavailable
from asset_pipeline.comfy.sdk_client import SdkComfyClient


class FakeOutput:
    def __init__(self, node, name, kind="image"):
        self.node_id, self.name, self.type = node, name, kind

    def to_bytes(self):
        return b"PNG:" + self.name.encode()


class FakeJob:
    def __init__(self, sdk, jid, graph):
        self.sdk, self.id, self.graph = sdk, jid, graph
        self.status, self.error, self.polls, self.canceled = "queued", None, 0, False

    def refresh(self):
        self.polls += 1
        if self.polls >= 2 and self.status in ("queued", "running"):
            self.status = self.sdk.final
            if self.status == "failed":
                self.error = SimpleNamespace(node_id="9", class_type="PreviewImage",
                                             message=self.sdk.message)
        return self

    def get_outputs(self, node):
        return [o for o in self.sdk.outputs if o.node_id == node]

    def cancel(self):
        self.canceled = True
        return self


class FakeSdk:
    def __init__(self, final="succeeded", message="", outputs=None):
        self.final, self.message = final, message
        self.outputs = outputs if outputs is not None else [FakeOutput("9", "r_0.png"),
                                                            FakeOutput("9", "r_1.png")]
        self.submitted, self.uploaded = [], []
        self.workflows = SimpleNamespace(from_json=lambda g: SimpleNamespace(json=g))
        self.assets = SimpleNamespace(from_file=self._asset)
        self.jobs = SimpleNamespace(get=lambda jid: self.job)

    def _asset(self, path):
        self.uploaded.append(path.name)
        return SimpleNamespace(as_reference=lambda: {"__type": "core/ASSET", "info": {"id": path.name}})

    def submit(self, wf):
        self.submitted.append(wf.json)
        self.job = FakeJob(self, f"p{len(self.submitted)}", wf.json)
        return self.job


def make(sdk, handler=None, sdk_url="http://proxy"):
    handler = handler or (lambda r: httpx.Response(200, json={"system": {}, "devices": []}))
    return SdkComfyClient("http://comfy", sdk_url, timeout_s=5,
                          transport=httpx.MockTransport(handler), sdk=sdk)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)


def test_run_returns_images_of_the_output_nodes():
    sdk = FakeSdk(outputs=[FakeOutput("9", "a.png"), FakeOutput("9", "a.json", "file"),
                           FakeOutput("5", "other.png")])
    out = make(sdk).run({"9": {"class_type": "PreviewImage", "inputs": {}}}, ["9"])
    assert [(o.node, o.filename, o.data) for o in out] == [("9", "a.png", b"PNG:a.png")]
    assert sdk.submitted == [{"9": {"class_type": "PreviewImage", "inputs": {}}}]


def test_run_files_returns_every_kind():
    sdk = FakeSdk(outputs=[FakeOutput("9", "a.png"), FakeOutput("9", "a.txt", "text")])
    assert [o.filename for o in make(sdk).run_files({}, ["9"])] == ["a.png", "a.txt"]


def test_no_outputs_is_an_error():
    with pytest.raises(ComfyError, match="no images"):
        make(FakeSdk(outputs=[])).run({}, ["9"])
    with pytest.raises(ComfyError, match="3D outputs"):
        make(FakeSdk(outputs=[])).run_files({}, ["9"])


def test_upload_returns_an_asset_reference(tmp_path):
    p = tmp_path / "frame.png"
    p.write_bytes(b"png")
    sdk = FakeSdk()
    ref = make(sdk).upload_image(p)
    assert ref["__type"] == "core/ASSET" and sdk.uploaded == ["frame.png"]


def test_failed_job_maps_to_execution_error_and_sam_no_detections():
    """segment._no_detections must still read "SAM found nothing" from an SDK failure."""
    sdk = FakeSdk(final="failed", message="index 0 is out of bounds for dimension 0 with size 0")
    with pytest.raises(ComfyError, match="execution failed") as e:
        make(sdk).run({}, ["9"])
    err = e.value.execution_error()
    assert err["node_type"] == "PreviewImage" and err["exception_type"] is None
    assert segment._no_detections(e.value)
    other = ComfyError("x", messages=[["execution_error", {"exception_type": None,
                                                           "exception_message": "out of memory"}]])
    assert not segment._no_detections(other)


def _comfy_queue(queue_state, posts):
    def handler(request):
        if request.method == "GET" and request.url.path == "/queue":
            return httpx.Response(200, json=queue_state)
        posts.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})
    return handler


def test_cancel_asks_the_sdk_then_deletes_through_comfyui():
    """The proxy's cancel doesn't reach ComfyUI <= 0.24, so the /queue delete still runs;
    another client's running prompt is untouched."""
    posts, sdk = [], FakeSdk()
    c = make(sdk, _comfy_queue({"queue_pending": [[1, "p1", {}, {}, []]],
                                "queue_running": [[0, "other", {}, {}, []]]}, posts))
    pid = c.queue({})
    assert c.cancel_prompt(pid) == "deleted"
    assert sdk.job.canceled and posts == [("/queue", {"delete": ["p1"]})]


def test_prompt_queued_under_a_job_is_canceled_with_it(monkeypatch):
    monkeypatch.undo()                                    # real sleeps for the worker thread
    posts = []
    c = make(FakeSdk(), _comfy_queue({"queue_pending": [], "queue_running": [[0, "p1", {}, {}, []]]},
                                     posts))
    q = jobs.JobQueue()
    started = threading.Event()

    def work():
        c.queue({"1": {}})
        started.set()
        while True:
            jobs.check_canceled()
            time.sleep(0.01)
    j = q.submit("k", "p", work)
    started.wait(5)
    q.cancel(j.id)
    for _ in range(500):
        if j.status == "canceled":
            break
        time.sleep(0.01)
    assert j.status == "canceled"
    assert posts == [("/interrupt", {"prompt_id": "p1"})]


def test_health_checks_the_proxy_too():
    def handler(request):
        if request.url.path == "/api/v2/health":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"system": {}, "devices": []})
    with pytest.raises(ComfyUnavailable, match="comfy_api_proxy.sh"):
        make(FakeSdk(), handler).health()


def test_backend_selection(monkeypatch):
    monkeypatch.setenv("AP_COMFY_BACKEND", "httpx")
    c = comfy_client.connect()
    assert type(c) is ComfyClient
    monkeypatch.setenv("AP_COMFY_BACKEND", "sdk")
    monkeypatch.setattr("asset_pipeline.comfy.sdk_client._sdk_client", lambda url, timeout: FakeSdk())
    monkeypatch.setenv("AP_COMFY_SDK_URL", "http://127.0.0.1:8190")
    c = comfy_client.connect(timeout_s=3)
    assert isinstance(c, SdkComfyClient) and c.sdk_url == "http://127.0.0.1:8190" and c.timeout_s == 3
    monkeypatch.setenv("AP_COMFY_BACKEND", "grpc")
    with pytest.raises(ValueError, match="httpx or sdk"):
        comfy_client.connect()


def test_local_toml_overrides_the_repo_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    (tmp_path / "backends.local.toml").write_text('[comfyui]\nbackend = "httpx"\n')
    config.backends.cache_clear()
    try:
        cfg = config.backends()
        assert cfg["comfyui"]["backend"] == "httpx"
        assert cfg["comfyui"]["url"]                       # the rest of [comfyui] kept
        assert cfg["comfyui"]["workflows"]["t2i"] == "flux_t2i"
    finally:
        config.backends.cache_clear()
