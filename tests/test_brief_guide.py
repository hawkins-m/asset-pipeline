"""Build guidance (brief edits from the local VLM) and Make 3D with a new seed. No VLM, no GPU."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from asset_pipeline import brief_guide, kit_proc, library_review as lr, review
from asset_pipeline.jobs import JobQueue
from asset_pipeline.project import read_json, write_json
from asset_pipeline.stages import s5_3d, sw_library, sw_site
from asset_pipeline.ui import app as ui_app

from tests.test_library_entries import _populate, proj  # noqa: F401  (fixture)

SHEET = "refs/library/hall-a/sheet_000.png"


class FakeLLM:
    name = "fake"

    def __init__(self, edits, unsupported=()):
        self.edits, self.unsupported, self.prompts, self.images = edits, list(unsupported), [], []

    def json_text(self, system, prompt, images, schema):
        self.prompts.append(prompt)
        self.images.append(images)
        return json.dumps({"edits": self.edits, "unsupported": self.unsupported})


def _wait(c, job):
    for _ in range(400):
        j = c.get(f"/api/jobs/{job['id']}").json()
        if j["status"] in ("done", "failed", "error"):
            return j
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def _setup(proj):
    sw_site.save_layout(proj, sw_site.starter_layout())
    _populate(proj, "hall-a")
    sw_library.set_route(proj, "hall-a", "procedural")
    sp = kit_proc.spec_path(proj, "hall-a")
    b = read_json(sp)
    b["dims"], b["form"] = [40, 40, 20], "perimeter"
    write_json(sp, b)
    return sp


def test_propose_keeps_valid_edits_and_drops_invalid_ones_with_reasons(proj):
    sp = _setup(proj)
    vals = brief_guide.values(read_json(sp))
    win = "features.street.window.w"
    kind = "features.street.window.kind"
    llm = FakeLLM([
        {"path": win, "after": 0.5, "why": "wider openings"},
        {"path": kind, "after": "gothic", "why": "pointed heads"},          # not a choice
        {"path": win.replace("window.w", "window.nonsense"), "after": 1, "why": "x"},   # no such field
        {"path": win.replace("window.w", "window.h"), "after": 1.7, "why": "taller"},   # share above 1
        {"path": "dims", "after": [20, 12], "why": "size"},               # needs three numbers
    ])
    r = brief_guide.propose(proj, "hall-a", SHEET, "wider windows please", llm=llm)
    assert [e["path"] for e in r["edits"]] == [win]
    e = r["edits"][0]
    assert e["after"] == 0.5 and e["before"] == vals[win] and e["why"] == "wider openings"
    reasons = {d["path"]: d["reason"] for d in r["dropped"]}
    assert "one of" in reasons[kind] and "not a field" in reasons[win.replace("window.w", "window.nonsense")]
    assert reasons[win.replace("window.w", "window.h")] and reasons["dims"]
    assert "wider windows please" in llm.prompts[0] and win in llm.prompts[0]
    assert str(llm.images[0][0]).endswith("sheet_000_v1.png")           # the brief's view crop
    assert not r["brief_made"]


def test_propose_makes_the_brief_first_when_there_is_none(proj, monkeypatch):
    _populate(proj, "hall-a")
    sw_site.save_layout(proj, sw_site.starter_layout())
    kit_proc.spec_path(proj, "hall-a").unlink()
    from asset_pipeline import library_entries as le

    def fake_brief(store, asset, sheet, view="front", **kw):
        write_json(kit_proc.spec_path(store, asset), {"kind": "building", "form": "perimeter", "dims": [40, 40, 20], "params": {}})
    monkeypatch.setattr(le, "brief_from_sheet", fake_brief)
    r = brief_guide.propose(proj, "hall-a", SHEET, "taller", llm=FakeLLM([]))
    assert r["brief_made"] and r["edits"] == []


def test_guide_endpoint_runs_a_gpu0_job_with_the_proposal(proj, monkeypatch):
    _setup(proj)
    monkeypatch.setattr(brief_guide, "propose", lambda store, a, sheet, g, view="front", **k:
                        {"edits": [{"path": "dims", "before": [40, 40, 20], "after": [40, 40, 22], "why": "w"}],
                         "dropped": [], "brief_made": False})
    c = TestClient(ui_app.create_app(JobQueue()))
    base = "/api/projects/p/library/asset/hall-a/guide"
    assert c.post(base, json={"sheet": SHEET, "guidance": "  "}).status_code == 400
    assert c.post("/api/projects/p/library/asset/canopy-table/guide", json={"sheet": SHEET, "guidance": "x"}).status_code == 400
    j = c.post(base, json={"sheet": SHEET, "guidance": "a taller hall"}).json()
    assert j["kind"] == "library.guide" and j["lane"] == "gpu0"
    done = _wait(c, j)
    assert done["status"] == "done" and done["result"]["edits"][0]["after"] == [40, 40, 22]


def test_accept_applies_only_accepted_edits_writes_guidance_and_rebuilds(proj, monkeypatch):
    sp = _setup(proj)
    lr.set_status(proj, "hall-a", "approved", "ok")
    vals = brief_guide.values(read_json(sp))
    win = "features.street.window.w"
    rebuilt = []
    monkeypatch.setattr(lr, "rebuild", lambda store, asset, log=None: rebuilt.append(asset) or {"round": 1})
    c = TestClient(ui_app.create_app(JobQueue()))
    base = "/api/projects/p/library/asset/hall-a/guide/apply"
    # an invalid row is refused and the brief is untouched
    before = sp.read_text()
    r = c.post(base, json={"guidance": "g", "edits": [{"path": win, "after": 7}]})
    assert r.status_code == 400 and sp.read_text() == before and not rebuilt
    # only the rows sent (the accepted ones) are applied
    r = c.post(base, json={"guidance": "wider windows", "edits": [{"path": win, "after": 0.5, "why": "w"}]})
    assert r.status_code == 200 and r.json()["kind"] == "library.rebuild"
    _wait(c, r.json())
    b = read_json(sp)
    assert b["guidance"] == "wider windows" and b["dims"] == [40, 40, 20]
    assert brief_guide.values(b)[win] == 0.5
    assert rebuilt == ["hall-a"] and lr.get(proj, "hall-a")["status"] == "needs_review"
    # no edits accepted: only the guidance is recorded, then the build
    r = c.post(base, json={"guidance": "just note it", "edits": []})
    _wait(c, r.json())
    assert read_json(sp)["guidance"] == "just note it" and brief_guide.values(read_json(sp))[win] == 0.5
    # empty guidance leaves no key
    r = c.post(base, json={"guidance": "", "edits": []})
    _wait(c, r.json())
    assert "guidance" not in read_json(sp)


def test_make_3d_new_seed_passes_a_non_default_seed_and_records_it(proj, monkeypatch):
    _populate(proj)
    out = s5_3d.out_dir(proj, "library", "canopy-table")
    cmds = []

    def fake_popen(cmd, **kw):
        cmds.append(cmd)
        seed = int(cmd[cmd.index("--seed") + 1])
        stem, to = f"sheet_000_v1_1024_cascade_s{seed}", Path(cmd[cmd.index("--out-dir") + 1])
        (to / f"{stem}.glb").write_bytes(b"glb")
        write_json(to / f"{stem}.json", {"glb_faces": 9})          # a result without a seed
        return SimpleNamespace(pid=0, returncode=0, poll=lambda: 0)
    monkeypatch.setattr(s5_3d.subprocess, "Popen", fake_popen)
    rec = s5_3d.run(proj, "library", "canopy-table", seed=None)
    seed = int(cmds[0][cmds[0].index("--seed") + 1])
    assert seed != 42 and rec["seed"] == seed
    assert read_json(out / f"sheet_000_v1_1024_cascade_s{seed}.json")["seed"] == seed
    assert s5_3d.results(proj, "library", "canopy-table")[0]["seed"] == seed
    # the default stays 42
    s5_3d.run(proj, "library", "canopy-table")
    assert int(cmds[1][cmds[1].index("--seed") + 1]) == 42
    # the endpoint's flag
    seen = {}
    monkeypatch.setattr(s5_3d, "run", lambda store, plan, asset, mode, seed: seen.update(seed=seed) or {})
    c = TestClient(ui_app.create_app(JobQueue()))
    j = c.post("/api/projects/p/3d", json={"plan": "library", "asset": "canopy-table", "new_seed": True}).json()
    _wait(c, j)
    assert seen["seed"] is None



def test_shade_smooth_guidance_becomes_the_shading_edit_and_the_rest_is_unsupported(proj):
    sp = _setup(proj)
    assert brief_guide.values(read_json(sp))["shading"] == "auto"
    llm = FakeLLM([], unsupported=["smooth shading", "rounded corners"])        # the VLM missed the field
    r = brief_guide.propose(proj, "hall-a", SHEET, "shade smooth, avoid faceted look, rounded corners", llm=llm)
    assert [(e["path"], e["after"]) for e in r["edits"]] == [("shading", "smooth")]
    assert r["unsupported"] == ["rounded corners"]
    assert "shading" in llm.prompts[0] and "[auto, smooth, flat]" in llm.prompts[0]
    brief_guide.apply(proj, "hall-a", r["edits"], "shade smooth")
    assert read_json(sp)["shading"] == "smooth"


def test_shading_must_be_a_choice(proj):
    _setup(proj)
    r = brief_guide.propose(proj, "hall-a", SHEET, "glossy", llm=FakeLLM([{"path": "shading", "after": "glossy"}]))
    assert r["edits"] == [] and "auto, smooth or flat" in r["dropped"][0]["reason"]


def test_curved_forms_shade_smooth_by_angle_by_default():
    from asset_pipeline import building_proc as bp
    box = [{"kind": "box"}]
    assert bp.shading_of({"form": "hall_vault"}, box) == {"smooth": True, "angle": 30.0}
    assert bp.shading_of({"form": "basilica"}, box + [{"kind": "shell"}]) == {"smooth": True, "angle": 30.0}  # apse, drum, dome
    assert bp.shading_of({"form": "cube"}, box) is None                        # blender_kit's default
    assert bp.shading_of({"form": "cube", "shading": "smooth"}, box) == {"smooth": True, "angle": 30.0}
    assert bp.shading_of({"form": "hall_vault", "shading": "flat"}, box) == {"smooth": False}


def _hall(proj):
    sp = _setup(proj)
    b = read_json(sp)
    b["dims"], b["form"], b["features"] = [43, 28, 19.5], "hall_vault", {}
    write_json(sp, b)
    return sp


def test_a_hall_vault_offers_only_the_settings_its_builder_uses(proj):
    sp = _hall(proj)
    st = brief_guide.settings(read_json(sp))
    assert st["labels"] == ["shading (smooth/flat)", "size (width, depth, height)", "materials (base, wall, top)",
                            "texture scale", "texture source", "plinth height", "vault thickness", "ribs", "end rims",
                            "skylight", "ridge cap", "end screens", "market stalls", "apron"]
    assert "features.ribs" in st["paths"] and "features.planter" not in st["paths"]
    assert not any(p.startswith("params.") for p in st["paths"])           # the hall has no storey or bay rhythm
    assert st["examples"] == ["shading", "size", "plinth height", "vault thickness", "ribs"]


def test_guidance_edits_to_settings_the_builder_ignores_are_dropped(proj):
    _hall(proj)
    llm = FakeLLM([{"path": "features.planter", "after": "none", "why": "monolithic"},
                   {"path": "features.vault_t", "after": 0.9, "why": "heavier"}])
    r = brief_guide.propose(proj, "hall-a", SHEET, "more monolithic and brutalist", llm=llm)
    assert [e["path"] for e in r["edits"]] == ["features.vault_t"]
    assert {d["path"]: d["reason"] for d in r["dropped"]} == {"features.planter": "this builder doesn't use it"}
    assert "This builder's settings: shading (smooth/flat)" in llm.prompts[0] and "brutalist" in llm.prompts[0]
    assert "features.planter" not in llm.prompts[0].split("Current brief fields")[1]


def test_build_sha_follows_what_is_built_not_where_or_the_texture_path(tmp_path):
    tex_a, tex_b = tmp_path / "a.jpg", tmp_path / "b.jpg"
    tex_a.write_bytes(b"concrete")
    tex_b.write_bytes(b"concrete")
    job = {"name": "x", "out": "/one/procedural.glb", "parts": [{"kind": "box"}], "materials": {"wall": {"base": str(tex_a)}}}
    same = job | {"name": "y", "out": "/two/procedural.glb", "materials": {"wall": {"base": str(tex_b)}}}
    assert kit_proc.build_sha(job) == kit_proc.build_sha(same)
    tex_b.write_bytes(b"brick")
    assert kit_proc.build_sha(job) != kit_proc.build_sha(same)
    assert kit_proc.build_sha(job) != kit_proc.build_sha(job | {"shading": {"smooth": False}})


def test_guidance_never_flattens_a_curved_form_unless_it_says_flat_shading(proj):
    _hall(proj)
    edits = [{"path": "shading", "after": "flat", "why": "brutalist"}, {"path": "features.vault_t", "after": 1.0, "why": "heavier"}]
    llm = FakeLLM(edits)
    r = brief_guide.propose(proj, "hall-a", SHEET, "more brutalist", llm=llm)
    assert [e["path"] for e in r["edits"]] == ["features.vault_t"]
    assert any(d["path"] == "shading" and "curved form" in d["reason"] for d in r["dropped"])
    assert "never set shading to flat" in llm.prompts[0]
    r = brief_guide.propose(proj, "hall-a", SHEET, "use flat shading", llm=FakeLLM(edits[:1]))
    assert [(e["path"], e["after"]) for e in r["edits"]] == [("shading", "flat")]


def test_a_box_form_may_go_flat_from_style_words(proj):
    _setup(proj)                                                  # perimeter: no curves
    r = brief_guide.propose(proj, "hall-a", SHEET, "brutalist", llm=FakeLLM([{"path": "shading", "after": "flat"}]))
    assert [(e["path"], e["after"]) for e in r["edits"]] == [("shading", "flat")]
