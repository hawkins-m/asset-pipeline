"""Vision-LLM adapters with mocked SDKs / server. No network, no paid calls."""
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from pydantic import BaseModel

from asset_pipeline.llm import base
from asset_pipeline.llm.base import LLMError, LLMRefusal, structured
from asset_pipeline.llm.claude import ClaudeVision
from asset_pipeline.llm.gemini import GeminiVision
from asset_pipeline.llm.local import LocalVision
from asset_pipeline.llm.registry import make_llm
from asset_pipeline.paid import PaidAPIBlocked


class Scene(BaseModel):
    objects: list[str]
    count: int


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "scene.png"
    Image.new("RGB", (3000, 1500), "teal").save(p)
    return p


@pytest.fixture
def paid_on(monkeypatch):
    monkeypatch.setenv("AP_ALLOW_PAID_APIS", "1")


class ScriptedLLM:
    name = "scripted"

    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def json_text(self, system, prompt, images, schema):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def test_structured_retries_with_validation_errors_then_succeeds():
    llm = ScriptedLLM(['{"objects": ["boat"]}', '```json\n{"objects": ["boat"], "count": 1}\n```'])
    out = structured(llm, "find things", [], Scene)
    assert out == Scene(objects=["boat"], count=1)
    assert "did not match" in llm.prompts[1] and "count" in llm.prompts[1]


def test_structured_gives_up_with_raw_text():
    llm = ScriptedLLM(["nope"] * 3)
    with pytest.raises(LLMError) as e:
        structured(llm, "x", [], Scene, retries=2)
    assert e.value.raw == "nope" and len(llm.prompts) == 3


def test_encode_image_downscales(img):
    data, mime = base.encode_image(img)
    assert mime == "image/jpeg"
    import io
    assert max(Image.open(io.BytesIO(data)).size) == 1568


# --- Claude -------------------------------------------------------------------

class FakeAnthropic:
    def __init__(self, response):
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))
        self.response = response

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_claude_blocked_without_paid_flag(img, monkeypatch):
    monkeypatch.delenv("AP_ALLOW_PAID_APIS", raising=False)
    fake = FakeAnthropic(None)
    with pytest.raises(PaidAPIBlocked):
        structured(ClaudeVision(client=fake), "x", [img], Scene)
    assert fake.calls == []


def test_claude_request_shape_and_parsed_output(img, paid_on):
    parsed = Scene(objects=["barrel"], count=1)
    fake = FakeAnthropic(SimpleNamespace(stop_reason="end_turn", parsed_output=parsed))
    out = structured(ClaudeVision(client=fake), "list objects", [img], Scene, system="be terse")
    assert out is parsed
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_format"] is Scene
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["system"] == "be terse"
    blocks = call["messages"][0]["content"]
    assert blocks[0]["type"] == "image" and blocks[0]["source"]["type"] == "base64"
    assert blocks[-1] == {"type": "text", "text": "list objects"}


def test_claude_refusal_and_truncation(img, paid_on):
    refused = SimpleNamespace(stop_reason="refusal", parsed_output=None,
                              stop_details=SimpleNamespace(category="cyber"))
    with pytest.raises(LLMRefusal, match="cyber"):
        structured(ClaudeVision(client=FakeAnthropic(refused)), "x", [img], Scene)
    cut = SimpleNamespace(stop_reason="max_tokens", parsed_output=None)
    with pytest.raises(LLMError, match="max_tokens"):
        structured(ClaudeVision(client=FakeAnthropic(cut)), "x", [img], Scene)


# --- Gemini -------------------------------------------------------------------

class FakeGenai:
    def __init__(self, texts):
        self.texts, self.calls = list(texts), []
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(text=self.texts.pop(0), prompt_feedback=None)


def test_gemini_json_mode_with_schema_and_retry(img, paid_on):
    fake = FakeGenai(['{"objects": []}', json.dumps({"objects": ["well"], "count": 1})])
    out = structured(GeminiVision(client=fake), "list", [img], Scene, system="sys")
    assert out.objects == ["well"]
    cfg = fake.calls[0]["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == Scene.model_json_schema()
    assert cfg.system_instruction == "sys"
    assert fake.calls[0]["model"] == "gemini-3.1-pro-preview"
    assert fake.calls[0]["contents"][-1] == "list"
    assert len(fake.calls) == 2  # one retry


def test_gemini_empty_response_is_an_error(img, paid_on):
    with pytest.raises(LLMError, match="no text"):
        GeminiVision(client=FakeGenai([""])).json_text("", "x", [img], {})


def test_gemini_blocked_without_paid_flag(img, monkeypatch):
    monkeypatch.delenv("AP_ALLOW_PAID_APIS", raising=False)
    fake = FakeGenai(["{}"])
    with pytest.raises(PaidAPIBlocked):
        GeminiVision(client=fake).json_text("", "x", [img], {})
    assert fake.calls == []


# --- Local --------------------------------------------------------------------

def test_local_posts_images_and_schema(img):
    seen = {}

    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"loaded": False})
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"text": '{"objects": ["cart"], "count": 1}'})

    llm = LocalVision("http://vlm", transport=httpx.MockTransport(handler))
    out = structured(llm, "list", [img], Scene, system="sys")
    assert out.objects == ["cart"]
    assert seen["system"] == "sys" and seen["schema"] == Scene.model_json_schema()
    assert len(seen["images"]) == 1 and seen["images"][0]["mime"] == "image/jpeg"


def test_local_errors_give_hints(img):
    down = LocalVision("http://vlm", transport=httpx.MockTransport(
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))))
    with pytest.raises(LLMError, match="ap vlm up"):
        down.json_text("", "x", [img], {})
    broken = LocalVision("http://vlm", transport=httpx.MockTransport(
        lambda r: httpx.Response(500, text="CUDA OOM")))
    with pytest.raises(LLMError, match="OOM"):
        broken.json_text("", "x", [img], {})


def test_registry():
    assert make_llm("local").name == "local"
    assert make_llm("gemini").name == "gemini"
    assert make_llm("claude").name == "claude"
    with pytest.raises(ValueError):
        make_llm("gpt")


# --- llama.cpp ----------------------------------------------------------------

def test_llamacpp_sends_openai_chat_with_json_schema(img):
    from asset_pipeline.llm.llamacpp import LlamaCppVision
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"objects": ["well"], "count": 1}'}}], "usage": {"completion_tokens": 9}})

    llm = LlamaCppVision("http://llama", transport=httpx.MockTransport(handler))
    out = structured(llm, "list", [img], Scene, system="sys")
    assert out.objects == ["well"] and llm.last_usage["completion_tokens"] == 9
    assert seen["response_format"]["json_schema"]["schema"] == Scene.model_json_schema()
    assert seen["temperature"] == 0
    sys_msg, user = seen["messages"]
    assert sys_msg["content"].startswith("sys") and "JSON Schema" in sys_msg["content"]
    assert user["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert user["content"][-1] == {"type": "text", "text": "list"}


def test_llamacpp_truncation_is_an_error(img):
    from asset_pipeline.llm.llamacpp import LlamaCppVision
    llm = LlamaCppVision("http://llama", transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"choices": [{"finish_reason": "length", "message": {"content": '{"obj'}}]})))
    with pytest.raises(LLMError, match="max_tokens"):
        llm.json_text("", "x", [img], {})
