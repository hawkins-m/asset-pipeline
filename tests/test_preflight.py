"""ap preflight: what it requires of ComfyUI, and how it reads /object_info."""
import httpx

from asset_pipeline import preflight


def test_demo_workflows_need_the_open_weights_models():
    files = set().union(*preflight.workflow_models().values())
    assert files == {"flux1-dev-fp8.safetensors", "flux1-redux-dev.safetensors",
                     "sigclip_vision_patch14_384.safetensors",
                     "flux1-dev-controlnet-union-pro-2.0.safetensors", "sam3.1_multiplex_fp16.safetensors"}


def test_missing_model_fails_with_a_hint(monkeypatch):
    have = {"flux1-dev-fp8.safetensors", "sam3.1_multiplex_fp16.safetensors"}

    def handler(request):
        node = request.url.path.rsplit("/", 1)[-1]
        field = next(f for f, n in preflight.LOADERS.items() if n == node)
        combo = node == "StyleModelLoader"          # the newer COMBO form for one loader
        spec = ["COMBO", {"options": sorted(have)}] if combo else [sorted(have)]
        return httpx.Response(200, json={node: {"input": {"required": {field: spec}}}})
    real = httpx.Client
    monkeypatch.setattr(preflight.httpx, "Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr("asset_pipeline.comfy.client.connect",
                        lambda timeout_s=None: type("C", (), {"health": lambda self: {"system": {}}})())
    models = preflight.comfy_checks()[1]
    assert not models.ok and "2 found" in models.detail
    assert "flux1-redux-dev.safetensors (StyleModelLoader)" in models.detail
    assert "fetch_models.sh" in models.detail
