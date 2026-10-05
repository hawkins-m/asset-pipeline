"""Gemini image backend with a mocked SDK. No network, no paid calls."""
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from asset_pipeline.imagegen.base import GenRequest
from asset_pipeline.imagegen.gemini import STYLE_REF, GeminiImageBackend, nearest_aspect
from asset_pipeline.paid import PaidAPIBlocked
from asset_pipeline.schema import LoraRef, StyleAnchor


def png_bytes(size=(64, 32)):
    buf = io.BytesIO()
    Image.new("RGB", size, "orange").save(buf, "PNG")
    return buf.getvalue()


class FakeGenai:
    def __init__(self, image=True):
        self.calls = []
        self.image = image
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        parts = [SimpleNamespace(inline_data=SimpleNamespace(data=png_bytes()))] if self.image else []
        return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=parts))],
                               text=None if self.image else "I can't draw that", prompt_feedback=None)


@pytest.fixture
def paid_on(monkeypatch):
    monkeypatch.setenv("AP_ALLOW_PAID_APIS", "1")


def test_nearest_aspect():
    assert nearest_aspect(1344, 768) == "16:9"
    assert nearest_aspect(1024, 1024) == "1:1"
    assert nearest_aspect(768, 1024) == "3:4"


def test_generates_n_images_with_seeds_and_aspect(tmp_path, paid_on):
    fake = FakeGenai()
    res = GeminiImageBackend(client=fake).generate(
        GenRequest(prompt="a barrel", n=2, seed=7, width=1344, height=768, negative="text"), tmp_path)
    assert [r.path.name for r in res] == ["img_000.png", "img_001.png"]
    assert [c["config"].seed for c in fake.calls] == [7, 8]
    assert fake.calls[0]["config"].image_config.aspect_ratio == "16:9"
    assert fake.calls[0]["config"].response_modalities == ["IMAGE"]
    assert fake.calls[0]["contents"][-1] == "a barrel. Avoid: text"
    assert res[0].meta["size"] == [64, 32] and res[0].backend == "gemini"


def test_anchor_images_sent_as_style_refs_and_lora_reported_ignored(tmp_path, paid_on):
    a = tmp_path / "a.png"
    Image.new("RGB", (32, 32), "red").save(a)
    anchor = StyleAnchor(images=[a], style_text="painterly", lora=LoraRef(file="s.safetensors"))
    fake = FakeGenai()
    res = GeminiImageBackend(client=fake).generate(GenRequest(prompt="a cart", anchor=anchor), tmp_path)
    contents = fake.calls[0]["contents"]
    assert len(contents) == 2 and not isinstance(contents[0], str)   # image part, then text
    assert contents[1].startswith(STYLE_REF) and contents[1].endswith("a cart, painterly")
    assert res[0].meta["ignored"] == ["lora"]


def test_no_image_is_an_error(tmp_path, paid_on):
    with pytest.raises(RuntimeError, match="can't draw"):
        GeminiImageBackend(client=FakeGenai(image=False)).generate(GenRequest(prompt="x"), tmp_path)


def test_blocked_without_paid_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("AP_ALLOW_PAID_APIS", raising=False)
    fake = FakeGenai()
    with pytest.raises(PaidAPIBlocked):
        GeminiImageBackend(client=fake).generate(GenRequest(prompt="x"), tmp_path)
    assert fake.calls == []
