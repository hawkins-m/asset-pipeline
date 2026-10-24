"""Fields with an automatic value show it as real, editable text (not a placeholder that vanishes
on click), marked "automatic" / "edited" with Reset to auto; Manage entry's Save says what a saved
description affects."""
import pytest

from test_ui_layout import BROWSER_PY, _browse

SCRIPT = r'''
import json, sys
from playwright.sync_api import sync_playwright
url = sys.argv[1]
out = {}
E = "harbour-prop-00"
STATE = """() => { const el = document.querySelector('#en-desc'), m = el.nextElementSibling;
  return [el.value, el.getAttribute('placeholder'), m.querySelector('.auto-state').textContent, m.querySelector('.auto-reset').hidden]; }"""
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1400, "height": 900})
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto(url); pg.wait_for_selector("#assets-list .asset-card", state="attached", timeout=20000)
    win = ".window:has(#entry-form)"
    pg.evaluate(f"manageEntry('{E}')"); pg.wait_for_selector("#en-desc")
    out["open"] = pg.evaluate(STATE)
    out["name"] = pg.evaluate("() => [document.querySelector('#en-name').value, document.querySelector('#en-name').nextElementSibling.querySelector('.auto-state').textContent]")
    pg.click("#en-desc"); out["clicked"] = pg.evaluate(STATE)[0]           # the wording stays to edit
    pg.locator("#en-desc").press("End"); pg.keyboard.type(" Weathered oak.")
    out["typed"] = pg.evaluate(STATE)
    pg.click(f"{win} .auto-mark:has(.auto-state) >> nth=1 >> .auto-reset")
    out["reset"] = pg.evaluate(STATE)
    pg.locator("#en-desc").press("End"); pg.keyboard.type(" Weathered oak.")
    pg.locator(f"{win} .wbuttons button", has_text="Save").click()
    pg.wait_for_selector("#entry-saved-dialog", timeout=10000)
    out["notice"] = pg.locator("#entry-saved-body").inner_text()
    pg.click("#entry-saved-dialog .wbuttons button")
    lib = pg.evaluate("async () => (await (await fetch(`/api/projects/${slug}/library`)).json())")
    out["saved"] = (lib.get("decisions") or {}).get(E, {}).get("description")
    pg.evaluate(f"manageEntry('{E}')"); pg.wait_for_selector("#en-desc")
    out["reopened"] = pg.evaluate(STATE)
    pg.click(f"{win} .auto-mark:has(.auto-state) >> nth=1 >> .auto-reset")
    pg.locator(f"{win} .wbuttons button", has_text="Save").click()
    pg.wait_for_selector("#entry-saved-dialog", timeout=10000)
    out["notice2"] = pg.locator("#entry-saved-body").inner_text()
    lib = pg.evaluate("async () => (await (await fetch(`/api/projects/${slug}/library`)).json())")
    out["saved2"] = (lib.get("decisions") or {}).get(E, {}).get("description")
    # a number field (the brief form's storey): 3.40 is the automatic 3.4, empty counts as automatic
    out["num"] = pg.evaluate("""() => {
      const d = document.createElement('div'); d.innerHTML = autoFieldHtml('input', 'type="number" id="nf"', null, 3.4);
      document.body.append(d); autoFieldsWire(d); const el = d.querySelector('#nf'), st = () => d.querySelector('.auto-state').textContent;
      const r = [el.value, st()]; el.value = '3.40'; el.dispatchEvent(new Event('input')); r.push(st(), autoFieldValue(el));
      el.value = '4'; el.dispatchEvent(new Event('input')); r.push(st(), autoFieldValue(el));
      el.value = ''; el.dispatchEvent(new Event('input')); r.push(st(), autoFieldValue(el)); return r; }""")
    out["errors"] = errs
    b.close()
print("RESULT " + json.dumps(out))
'''


@pytest.mark.skipif(not BROWSER_PY.exists(), reason="needs the browser-test env (Playwright + system Chrome)")
def test_automatic_description_is_editable_text_marked_and_resettable(tmp_path, monkeypatch):
    out = _browse(tmp_path, monkeypatch, SCRIPT)
    auto = "A harbour-prop-00."
    assert out["open"] == [auto, None, "automatic", True]                 # real text, no placeholder
    assert out["name"] == ["harbour prop 00", "automatic"]
    assert out["clicked"] == auto
    assert out["typed"] == [auto + " Weathered oak.", None, "edited", False]
    assert out["reset"] == [auto, None, "automatic", True]
    assert out["saved"] == auto + " Weathered oak."
    assert "Description saved" in out["notice"] and "Reference sheets" in out["notice"] and "don't read it" in out["notice"]
    assert out["reopened"][0] == auto + " Weathered oak." and out["reopened"][2] == "edited"
    assert out["saved2"] is None and "back to the automatic one" in out["notice2"]
    assert out["num"] == ["3.4", "automatic", "automatic", None, "edited", "4", "automatic", None]
    assert out["errors"] == []
