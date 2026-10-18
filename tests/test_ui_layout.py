"""Library / Views and 3D layout: fixed headers, a scroll position per list, Jump to, name links
between Asset review and Reference sheets, and the Show filter defaulting to the current area."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.stages import sw_library, sw_site
from asset_pipeline.ui import app as ui_app

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "asset_pipeline" / "ui" / "static"
BROWSER_PY = Path(os.environ.get("AP_ROOT_REAL", "/mnt/storage/asset-pipeline")) / "envs" / "browser-test" / "bin" / "python"


def _entry(eid, district):
    return {"id": eid, "tier": "D", "kind": "prop", "typology": eid, "part": eid, "material": None, "instances": 1,
            "districts": {}, "district": district, "mood": [], "flags": [], "best": None, "share": 0.0,
            "description_auto": f"A {eid}.", "description": None, "size_m": [2, 2, 2], "route_auto": "trellis", "slot_ids": []}


def _project(store: ProjectStore, n_per_district=24):
    """Two districts of props, every one decided, each with a sheet; an area over one district."""
    ents = [_entry(f"{d}-prop-{i:02d}", d) for d in ("academy", "harbour") for i in range(n_per_district)]
    write_json(sw_site.site_dir(store) / "library.json", {"min_share": 0.005, "sources": {}, "mood": {}, "candidates": [],
                                                          "checked": True, "entries": [], "unseen": ents, "surfaces": {}})
    write_json(sw_site.site_dir(store) / "areas.json", {"harbour-area": {"shots": [], "districts": ["harbour"]}})
    for e in ents:
        sw_library.decide(store, e["id"], "description")
        d = store.root / "refs/library" / e["id"]
        d.mkdir(parents=True)
        (d / "sheet_000.png").write_bytes(b"")
        write_json(d / "meta.json", {"plan": "library", "unit": e["id"], "kind": "object", "assets": [e["id"]], "sheets": []})
    sw_library.asset_plan(store)
    return ents


def test_library_data_carries_each_areas_members(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    store = ProjectStore.create("p")
    _project(store, 3)
    d = TestClient(ui_app.create_app(JobQueue())).get("/api/projects/p/library").json()
    assert d["areas"] == {"harbour-area": ["harbour-prop-00", "harbour-prop-01", "harbour-prop-02"]}


def test_lists_are_their_own_scroll_containers_and_jump_js_loads_after_what_it_wraps():
    html = (STATIC / "index.html").read_text()
    for k, lst in (("assets", "assets-list"), ("refs", "refs-list"), ("views", "review-list")):
        assert f'id="{lst}" class="scroll-list"' in html and f'id="{k}-jump"' in html
    order = [html.index(f"/static/{f}.js") for f in ("shell", "review", "refs", "library", "jump")]
    assert order == sorted(order)
    css = (STATIC / "style.css").read_text()
    assert "#library-tab, #review-tab { display: flex; flex-direction: column; overflow: hidden; }" in css


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BROWSER_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright
url = sys.argv[1]
out = {}
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1400, "height": 800})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url); pg.wait_for_selector("#assets-list .asset-card", state="attached", timeout=20000)
    pg.click("button[data-tab=library]"); pg.click("button[data-sub=assets]"); pg.wait_for_timeout(500)
    out["filter"] = pg.locator("#lib-filter").input_value()
    out["cards_in_area"] = pg.locator("#assets-list .asset-card").count()
    pg.locator("#lib-filter").select_option("all"); pg.wait_for_timeout(300)
    out["first_section"] = pg.locator("#assets-list .asset-district h3").first.inner_text().split()[0]
    al, rl = pg.locator("#assets-list"), pg.locator("#refs-list")
    bar = pg.locator("#assets-tab > .row").bounding_box()["y"]
    al.evaluate("e => e.scrollTop = 600"); pg.wait_for_timeout(300)
    out["bar_fixed"] = pg.locator("#assets-tab > .row").bounding_box()["y"] == bar
    out["panel_scroll"] = pg.locator("#library-tab").evaluate("e => e.scrollTop")
    pg.click("button[data-sub=refs]"); pg.wait_for_timeout(300)
    rl.evaluate("e => e.scrollTop = 450"); pg.wait_for_timeout(300)
    pg.click("button[data-sub=assets]"); pg.wait_for_timeout(300)
    out["assets_back"] = al.evaluate("e => e.scrollTop")
    pg.click("button[data-sub=refs]"); pg.wait_for_timeout(300)
    out["refs_back"] = rl.evaluate("e => e.scrollTop")
    pg.reload(); pg.wait_for_timeout(2500)
    out["after_reload"] = [pg.locator("#refs-list").evaluate("e => e.scrollTop"), pg.locator("#lib-filter").input_value()]
    j = pg.locator("#refs-jump"); j.fill("academy-prop-12"); j.press("Enter"); pg.wait_for_timeout(300)
    u = pg.locator('#refs-list .unit[data-unit="academy-prop-12"]')
    out["jump_offset"] = round(u.bounding_box()["y"] - rl.bounding_box()["y"])
    u.locator(".jump-link").click(); pg.wait_for_timeout(400)
    c = pg.locator('#assets-list .asset-card[data-entry="academy-prop-12"]')
    out["link_sub"] = pg.locator("#lib-tabs [aria-selected=true]").inner_text()
    out["link_offset"] = round(c.bounding_box()["y"] - pg.locator("#assets-list").bounding_box()["y"])
    out["errors"] = errs
    b.close()
print("RESULT " + json.dumps(out))
'''


def _browse(tmp_path, monkeypatch, script: str) -> dict:
    """A real `ap ui` on a scratch project, driven by `script` in the browser-test env."""
    root = tmp_path / "root"
    monkeypatch.setenv("AP_ROOT", str(root))
    _project(ProjectStore.create("p"))
    env = dict(os.environ, AP_ROOT=str(root))
    port = _free_port()
    srv = subprocess.Popen([sys.executable, "-m", "asset_pipeline.cli", "ui", "--port", str(port)], env=env, cwd=REPO,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        drive = tmp_path / "drive.py"
        drive.write_text(script)
        r = subprocess.run([str(BROWSER_PY), str(drive), f"http://127.0.0.1:{port}/?project=p"],
                           capture_output=True, text=True, timeout=120)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
        assert line, r.stdout + r.stderr
        return json.loads(line[7:])
    finally:
        srv.terminate()
        srv.wait(10)


@pytest.mark.skipif(not BROWSER_PY.exists(), reason="needs the browser-test env (Playwright + system Chrome)")
def test_fixed_headers_scroll_memory_jump_and_links_in_a_browser(tmp_path, monkeypatch):
    out = _browse(tmp_path, monkeypatch, BROWSER_SCRIPT)
    assert out["filter"] == "area:harbour-area" and out["cards_in_area"] == 24     # the current area by default
    assert out["first_section"] == "academy"
    assert out["bar_fixed"] and out["panel_scroll"] == 0                           # only the list scrolls
    assert out["assets_back"] == 600 and out["refs_back"] == 450                   # one position per sub-tab
    assert out["after_reload"] == [450, "all"]                                     # remembered, with the filter
    assert 0 <= out["jump_offset"] <= 8
    assert out["link_sub"] == "Asset review" and 0 <= out["link_offset"] <= 8
    assert out["errors"] == []


POLL_SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright
url = sys.argv[1]
out = {}
MARK = """sel => { document.querySelectorAll(sel).forEach(e => { e.__m = 1; e.querySelectorAll('img, model-viewer').forEach(m => m.__m = 1); }); }"""
KEPT = """sel => { const r = [...document.querySelectorAll(sel)]; return [r.length, r.filter(e => e.__m).length,
  r.flatMap(e => [...e.querySelectorAll('img')]).filter(m => !m.__m).length]; }"""
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1400, "height": 800})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url); pg.wait_for_selector("#assets-list .asset-card", state="attached", timeout=20000)
    pg.click("button[data-tab=library]"); pg.click("button[data-sub=assets]"); pg.wait_for_timeout(400)
    pg.locator("#lib-filter").select_option("all"); pg.wait_for_timeout(300)
    for k, lst, row in (("assets", "#assets-list", ".asset-card"), ("refs", "#refs-list", ".unit"), ("views", "#review-list", ".asset-review")):
        if k == "refs": pg.click("button[data-sub=refs]")
        if k == "views": pg.click("button[data-tab=views]")
        pg.wait_for_timeout(300)
        pg.locator(lst).evaluate("e => e.scrollTop = 500")
        pg.evaluate(MARK, f"{lst} {row}")
        ys = pg.evaluate(f"() => [...document.querySelectorAll('{lst} {row}')].slice(0, 30).map(e => Math.round(e.getBoundingClientRect().top))")
        for _ in range(3):                                  # three polls with nothing changed
            pg.evaluate("load()"); pg.wait_for_timeout(250)
        out[k] = {"kept": pg.evaluate(KEPT, f"{lst} {row}"), "scroll": pg.locator(lst).evaluate("e => e.scrollTop"),
                  "moved": ys != pg.evaluate(f"() => [...document.querySelectorAll('{lst} {row}')].slice(0, 30).map(e => Math.round(e.getBoundingClientRect().top))")}
    # one row's data changes: only it is rebuilt, and it keeps its loaded image
    pg.click("button[data-tab=library]"); pg.click("button[data-sub=refs]"); pg.wait_for_timeout(300)
    pg.evaluate(MARK, "#refs-list .unit")
    out["changed"] = pg.evaluate("""() => {
      const u = refs[0]; u.guidance = "a new batch's words";
      renderRefs();
      const rows = [...document.querySelectorAll('#refs-list .unit')];
      const r = rows.find(e => e.dataset.unit === u.key);
      return [!r.__m, !!r.querySelector('img').__m, rows.filter(e => e !== r && !e.__m).length];
    }""")
    out["errors"] = errs
    b.close()
print("RESULT " + json.dumps(out))
'''


@pytest.mark.skipif(not BROWSER_PY.exists(), reason="needs the browser-test env (Playwright + system Chrome)")
def test_polling_redraws_keep_rows_images_and_scroll(tmp_path, monkeypatch):
    """The bug: every poll rebuilt every row, so images went blank and the page shook."""
    out = _browse(tmp_path, monkeypatch, POLL_SCRIPT)
    for k in ("assets", "refs", "views"):
        n, kept, new_imgs = out[k]["kept"]
        assert n > 0 and kept == n and new_imgs == 0, (k, out[k])     # same rows, same image elements
        assert out[k]["scroll"] == 500 and not out[k]["moved"], (k, out[k])
    assert out["changed"] == [True, True, 0]                          # rebuilt, image kept, others untouched
    assert out["errors"] == []
