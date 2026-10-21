"""The tabs as a linear workflow: a global toolbar, stage buttons in their own tabs, a header strip
per tab with an honest status and a Next button, and the guide panel."""
import re
from pathlib import Path

from asset_pipeline.ui import app as ui_app

STATIC = Path(ui_app.STATIC)


def _html():
    return (STATIC / "index.html").read_text()


def test_toolbar_keeps_only_global_controls_and_stage_buttons_live_in_their_tabs():
    html = _html()
    bar = re.search(r'<div class="toolbar".*?\n  </div>', html, re.S).group(0)
    ids = re.findall(r'id="([^"]+)"', bar)
    assert ids == ["project", "tb-new", "tb-disk", "tb-stop"], ids

    def tab(name):
        return re.search(rf'<section class="tabpanel" data-tab="{name}".*?</section>', html, re.S).group(0)
    assert all(f'id="{i}"' in tab("city") for i in ("city-apply", "city-replan", "city-build"))
    assert "Update city plan" in tab("city") and "Update plan, greybox &amp; passes" in tab("city")
    assert 'id="site-apply"' in tab("site") and 'id="site-build"' in tab("site")
    assert 'id="shots-render"' in tab("shots")
    assert 'id="frames-missing"' in tab("frames")
    shell = (STATIC / "shell.js").read_text()
    for act in ("applyAll", "replan", "build", "render", "frames"):     # still in the Pipeline menu
        assert re.search(rf'\["[^"]+", "{act}"', shell), act


def test_apply_shows_its_steps_before_running():
    shell = (STATIC / "shell.js").read_text()
    body = shell[shell.index("async function applyChanges"):shell.index("function applyPreview")]
    assert body.index("applyPreview(steps") < body.index("applyState = {steps")
