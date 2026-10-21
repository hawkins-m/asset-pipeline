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
vm.runInContext(fs.readFileSync(process.argv[1], "utf8") + "\n;globalThis.__wf = " + (S.expr || "workflow().map(s => [s.id, s.status, s.next.ok])") + ";", ctx);
console.log(JSON.stringify(ctx.__wf));
"""


def _wf(**over):
    state = {"data": {"project": {"anchor": None}, "explore": [], "moodboard": {}, "jobs": [], "stars": {}},
             "siteData": {"layout": False, "summary": None, "shots": []}, "framesData": {"shots": []},
             "libraryData": {"plan": None, "decisions": {}}, "reviewData": {"assets": [], "pending_sheets": [], "starred_sheets": 0},
             "unrealData": {"greybox": False, "export": None, "project": False}}
    state.update(over)
    out = subprocess.run([NODE, "-e", HARNESS, str(STATIC / "workflow.js"), json.dumps(state)], capture_output=True, text=True, check=True)
    got = json.loads(out.stdout)
    return got if "expr" in over else {k: (st, ok) for k, st, ok in got}


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


def test_left_panel_is_the_same_on_every_tab_and_frames_has_its_shot_list_inside():
    html = _html()
    assert 'id="side-frames"' not in html and html.count('<aside class="side"') == 1
    frames = re.search(r'<section class="tabpanel" data-tab="frames".*?</section>', html, re.S).group(0)
    assert 'id="frames-shots"' in frames
    assert "side-default" not in (STATIC / "shell.js").read_text()      # showTab no longer swaps panels


def test_stage_list_uses_the_workflow_states_in_tab_order():
    shell = (STATIC / "shell.js").read_text()
    assert "function stageStates" not in shell and "workflow()" in shell[shell.index("function renderStages"):]
    wf = (STATIC / "workflow.js").read_text()
    ids = re.findall(r'\{id: "(\w+)", tabs: \[([^\]]*)\]', wf)
    tabs = [t.strip().strip('"') for _, ts in ids for t in ts.split(",")]
    assert [i for i, _ in ids] == ["style", "layout", "shots", "frames", "library", "views", "unreal"]
    assert sorted(tabs) == sorted(re.findall(r'data-tab="(\w+)">', _html())) and '"city", "site"' in wf
    assert "City = procedural layout, Site = your own Blender layout" in _html()


def test_frames_lock_button_star_note_and_discover_assets():
    frames = (STATIC / "frames.js").read_text()
    assert "Use this frame for this shot" in frames and "Starring only marks frames you like" in frames
    assert "frames/lock" in frames and "Discover assets →" in frames and "discoverAssets()" in frames
    lib = (STATIC / "library.js").read_text()
    assert "Discover assets from locked shots" in lib and "Discover assets from locked shots" in _html()
    assert ">Re-plan<" not in _html()


def test_library_opens_on_areas_as_its_first_sub_tab():
    html = _html()
    subs = re.findall(r'<button role="tab" data-sub="(\w+)"', html)
    assert subs[0] == "areas" and 'id="areas-tab"' in html
    shell = (STATIC / "shell.js").read_text()
    assert ': "areas");' in shell and 'localStorage.getItem("ap.sub")' not in shell     # every load opens on Areas
    areas = (STATIC / "areas.js").read_text()
    assert "<th>Next</th>" in areas and 'showSub("areas")' in areas


def test_unreal_tab_is_numbered_steps_each_with_a_reason():
    html = _html()
    tab = re.search(r'<section class="tabpanel" data-tab="unreal".*?</section>', html, re.S).group(0)
    steps = re.findall(r'<li data-step="(\w+)"><span class="n">(\d)</span><b>([^<]+)</b>', tab)
    assert steps == [("init", "1", "Create UE project"), ("export", "2", "Export"), ("import", "3", "Import"),
                     ("render", "4", "Render shots"), ("look", "5", "Match look")]
    js = (STATIC / "unreal.js").read_text()
    assert "Create look preset…" in js and "ue/looks/new" in js and "ue-why" in js


@pytest.mark.skipif(not NODE, reason="needs node")
def test_library_empty_states_name_the_real_prerequisite():
    def pre(views=False, **kw):
        p = _wf(expr=f"libraryPrereq({str(views).lower()})", **kw)
        return p and (p["text"], p["go"])
    assert "Scene plans" in pre()[0]                                               # no site layout: scene mode
    built = {"layout": True, "summary": None, "shots": []}
    assert pre(siteData=built)[1] == ["site", None] and "greybox" in pre(siteData=built)[0]
    assert pre(siteData=built, city=True)[1] == ["city", None]
    built["summary"] = {"slots": 1}
    assert pre(siteData=built)[1] == ["frames", None]
    frames = {"shots": [{"id": "a", "mood": False, "locked": "frames/a/b/f.png", "batches": []}]}
    assert pre(siteData=built, framesData=frames)[1] == ["library", "assets"] and "Discover assets" in pre(siteData=built, framesData=frames)[0]
    lib = {"plan": {"entries": [{"id": "e1"}], "unseen": []}, "decisions": {}}
    assert "decide each asset" in pre(siteData=built, framesData=frames, libraryData=lib)[0]
    lib["decisions"] = {"e1": {"decision": "description"}}
    assert pre(siteData=built, framesData=frames, libraryData=lib) is None
    assert pre(True, siteData=built, framesData=frames, libraryData=lib)[1] == ["library", "refs"]
    for f in ("refs.js", "review.js"):
        assert "analyse a scene in Library › Scene plans" not in (STATIC / f).read_text()


def test_generated_go_buttons_never_carry_data_tab_or_data_sub():
    """showSub hides every [data-sub] element in the Library tab: an empty-state button carrying
    data-sub="" vanished as soon as a sub-tab was shown."""
    wf = (STATIC / "workflow.js").read_text()
    assert 'data-sub="${' not in wf and 'data-tab="${nextTab}"' not in wf and "data-go-tab" in wf


@pytest.mark.skipif(not NODE, reason="needs node")
def test_approve_says_which_version_export_and_unreal_use():
    js = (STATIC / "versions.js").read_text()
    fn = js[js.index("function versionUseLine"):js.index("async function versionJob")]
    run = lambda cur, ok: subprocess.run([NODE, "-e", fn + f";console.log(versionUseLine({json.dumps(cur)}, {str(ok).lower()}))"],
                                         capture_output=True, text=True, check=True).stdout
    assert "v3 (procedural)" in run({"n": 3, "kind": "procedural"}, False) and "only once it is Approved" in run({"n": 3, "kind": "procedural"}, False)
    assert "only once" not in run({"n": 3, "kind": "procedural"}, True)
    assert "attached to an Approved building" in run({"n": 2, "kind": "trellis"}, False)
    assert "No version in use" in run(None, False)
    assert all("greybox stand-ins" in run(c, False) for c in (None, {"n": 1, "kind": "procedural"}, {"n": 1, "kind": "trellis"}))
    for f in ("review.js", "library_review.js"):
        assert "versionUseLine(" in (STATIC / f).read_text()


def test_confusing_labels_are_renamed():
    ui = "".join((STATIC / f).read_text() for f in ("index.html", "library.js", "review.js", "refs.js", "areas.js", "versions.js", "shell.js"))
    for old in ('"Good source"', '"Use description"', '"Description + reference frame"', "<th>Route</th>", '"Cut views"',
                '`Cut views (', '"Unpin" : "Pin"', '"3D route:"'):
        assert old not in ui, old
    for new in ('"Frame crop"', '"Description only"', '"Description + chosen image"', "<th>Build method</th>",
                "Extract reference views", '"Stop keeping" : "Keep"', '"Build method:"'):
        assert new in ui, new


def test_base_text_is_12px_and_secondary_text_11px():
    css = (STATIC / "style.css").read_text()
    assert "body { margin: 0; font: 12px/1.35 var(--ui);" in css
    assert re.search(r"\.hint \{[^}]*font-size: 11px;", css)
    sizes = [int(x) for x in re.findall(r"font-size: (\d+)px", css)]
    assert min(sizes) >= 10 and sizes.count(10) <= 1          # only the dialog close glyph stays at 10px
