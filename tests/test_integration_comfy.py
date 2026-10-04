"""Live tests against the configured ComfyUI (pytest -m comfy). Skipped if it's down."""
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
