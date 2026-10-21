"""The tabs as a linear workflow: a global toolbar, stage buttons in their own tabs, a header strip
per tab with an honest status and a Next button, and the guide panel."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

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


# --- workflow.js states, run in node against stubbed project data --------------------------------
NODE = shutil.which("node")
HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const S = JSON.parse(process.argv[2]);
const ctx = {console, JSON, Object, Set, Array, Math, String, Number,
  data: S.data, siteData: S.siteData, framesData: S.framesData, libraryData: S.libraryData, reviewData: S.reviewData,
  unrealData: S.unrealData, refs: S.refs || [], lr: {list: S.builds ? {assets: S.builds} : null}, slug: "p", currentTab: "city",
  TAB_NAMES: {style: "Style", site: "Site", city: "City", shots: "Shots", frames: "Frames", library: "Library", views: "Views and 3D", unreal: "Unreal"},
  activeJobs: k => (S.jobs || []).filter(j => j === k), isActive: () => false, applyRunning: () => false,
  outOfDate: () => S.ood || {plan: false, greybox: false, passes: 0, any: false}, isCity: () => !!S.city,
  ueExportStale: () => !!S.ueStale, lrAssetsOf: () => [],
  libEntries: () => S.libraryData && S.libraryData.plan ? S.libraryData.plan.entries : [],
  libDecision: e => (S.libraryData.decisions[e.id] || {}).decision || null};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\n;globalThis.__wf = workflow().map(s => [s.id, s.status, s.next.ok]);", ctx);
console.log(JSON.stringify(ctx.__wf));
"""


def _wf(**over):
    state = {"data": {"project": {"anchor": None}, "explore": [], "moodboard": {}, "jobs": [], "stars": {}},
             "siteData": {"layout": False, "summary": None, "shots": []}, "framesData": {"shots": []},
             "libraryData": {"plan": None, "decisions": {}}, "reviewData": {"assets": [], "pending_sheets": [], "starred_sheets": 0},
             "unrealData": {"greybox": False, "export": None, "project": False}}
    state.update(over)
    out = subprocess.run([NODE, "-e", HARNESS, str(STATIC / "workflow.js"), json.dumps(state)], capture_output=True, text=True, check=True)
    return {k: (st, ok) for k, st, ok in json.loads(out.stdout)}


@pytest.mark.skipif(not NODE, reason="needs node")
def test_workflow_states_come_from_project_state_not_single_files():
    w = _wf()
    assert w["style"] == ("none", True)                          # optional: Next always open
    assert w["layout"] == ("input", False) and w["shots"][0] == "none" and w["unreal"][0] == "none"

    built = {"layout": True, "summary": {"slots": 3, "stale": False}, "shots": [
        {"id": "a", "rendered": "2026-10-20T10:00", "stale": False}, {"id": "b", "rendered": None, "stale": False}]}
    w = _wf(siteData=built)
    assert w["layout"] == ("done", True)
    assert w["shots"] == ("stale", True)                         # one fresh pass is enough to go on

    w = _wf(siteData=built, ood={"plan": True, "greybox": False, "passes": 1, "any": True}, city=True)
    assert w["layout"][0] == "stale"

    # frames: generated but nothing locked = needs review, Next closed; all locked = complete
    frames = {"shots": [{"id": "a", "mood": False, "locked": None, "batches": [{"archived": False, "frames": [{"key": "frames/a/b/f.png", "role": None}]}]}]}
    assert _wf(siteData=built, framesData=frames)["frames"] == ("review", False)
    frames["shots"][0]["locked"] = "frames/a/b/f.png"
    assert _wf(siteData=built, framesData=frames)["frames"] == ("done", True)

    # library: a lock the plan never considered is out of date; one it considered and dropped is not
    plan = {"entries": [{"id": "e1"}], "unseen": [], "candidates": [], "sources": {}}
    lib = {"plan": plan, "decisions": {"e1": {"decision": "description"}}}
    assert _wf(siteData=built, framesData=frames, libraryData=lib)["library"][0] == "stale"
    plan["candidates"] = [{"frame": "frames/a/b/f.png", "ok": False}]
    assert _wf(siteData=built, framesData=frames, libraryData=lib)["library"][0] == "ready"     # e1 has no sheets
    assert _wf(siteData=built, framesData=frames, libraryData=lib,
               refs=[{"plan": "library", "key": "e1", "sheets": ["s"]}])["library"][0] == "done"
    lib["decisions"] = {}
    assert _wf(siteData=built, framesData=frames, libraryData=lib)["library"][0] == "input"

    # unreal: exported but stale export is out of date even with a project
    ue = {"greybox": True, "export": {"mtime": 1}, "project": True}
    assert _wf(siteData=built, unrealData=ue)["unreal"][0] == "done"
    assert _wf(siteData=built, unrealData=ue, ueStale=True)["unreal"][0] == "stale"
