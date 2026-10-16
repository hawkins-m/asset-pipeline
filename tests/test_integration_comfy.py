"""Live tests against the configured ComfyUI (pytest -m comfy). Skipped if it's down."""
import os

import pytest
from PIL import Image

from asset_pipeline import config
from asset_pipeline.comfy.client import ComfyClient, ComfyUnavailable
from asset_pipeline.imagegen.base import GenRequest
from asset_pipeline.imagegen.registry import make_backend

pytestmark = pytest.mark.comfy


@pytest.fixture(scope="module", autouse=True)
def comfy_up():
    try:
        ComfyClient(config.backends()["comfyui"]["url"]).health()
    except ComfyUnavailable as e:
        pytest.skip(str(e))


def test_flux_t2i_returns_real_images(tmp_path):
    res = make_backend("comfyui").generate(
        GenRequest(prompt="a single red cube on a plain white background, studio lighting",
                   width=512, height=512, n=2, seed=1, steps=8), tmp_path)
    assert len(res) == 2
    for r in res:
        im = Image.open(r.path)
        assert im.size == (512, 512)
        lo, hi = im.convert("L").getextrema()
        assert hi - lo > 50, "image is blank/flat"


def _sdk_client():
    """The Comfy SDK client through comfy-api-proxy; skips if the proxy isn't running."""
    from asset_pipeline.comfy.sdk_client import SdkComfyClient
    c = config.backends()["comfyui"]
    url = os.environ.get("AP_COMFY_SDK_URL") or c.get("sdk_url")
    client = SdkComfyClient(c["url"], url, timeout_s=c.get("timeout_s", 900))
    try:
        client.health()
    except ComfyUnavailable as e:
        pytest.skip(str(e))
    return client


def test_sdk_and_httpx_backends_agree(tmp_path):
    """One small workflow (SAM 3.1) through both backends: upload, PreviewImage (temp)
    outputs, and the "found nothing" failure that segment.detect turns into []."""
    from asset_pipeline import segment
    img = Image.new("RGB", (512, 512), "white")
    img.paste((220, 30, 30), (160, 160, 352, 352))
    path = tmp_path / "red_square.png"
    img.save(path)
    results = {}
    for name, client in (("httpx", ComfyClient(config.backends()["comfyui"]["url"])),
                         ("sdk", _sdk_client())):
        found = segment.detect(client, path, "red square")
        none = segment.detect(client, path, "giraffe")
        results[name] = ([(d.bbox, round(d.area_frac, 4)) for d in found], none)
    assert results["httpx"][0], "SAM found no red square"
    assert results["sdk"] == results["httpx"]
    assert results["sdk"][1] == []
