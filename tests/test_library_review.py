"""Build reviews (library_review): statuses with history per round, edits reset approval,
validation, the locked-frame check and the task list."""
import pytest

from asset_pipeline import kit_proc, library_review as lr
from asset_pipeline.project import ProjectStore, write_json

BRIEF = {"kind": "building", "form": "cube", "dims": [40.0, 40.0, 36.0], "params": {"storey_h": 9.0},
         "features": {}, "sheet": "refs/x.png", "position": 1, "view": "views/x.png", "measured": {}}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    s = ProjectStore.create("demo")
    write_json(kit_proc.spec_path(s, "cube-a"), BRIEF)
    monkeypatch.setattr(lr, "entry_of", lambda store, asset: None)
    monkeypatch.setattr(lr.sw_site, "load_layout", lambda store: type("L", (), {"city": None, "shots": []})())
    return s


def test_status_history_and_task_list(store):
    r = lr.set_status(store, "cube-a", "needs_changes", "Niche wider.\nJoints larger.")
    assert r["status"] == "needs_changes" and r["round"] == 1 and len(r["history"]) == 1
    r = lr.set_status(store, "cube-a", "needs_feature", "Needs a screen.")
    assert len(r["history"]) == 1 and r["history"][0]["status"] == "needs_feature"     # one call per round
    txt = lr.task_list(store)
    assert "cube-a [needs new feature]" in txt and "Needs a screen." in txt and "--done cube-a" in txt
    assert lr.done(store, ["cube-a"]) == 2
    r = lr.get(store, "cube-a")
    assert r["status"] == "ready" and lr.current_round(store) == 2 and not lr.tasks(store)
    r = lr.set_status(store, "cube-a", "approved", "")
    assert [h["round"] for h in r["history"]] == [1, 1, 2]


def test_editing_an_approved_brief_sets_needs_review(store):
    lr.set_status(store, "cube-a", "approved", "ok")
    lr.save_brief(store, "cube-a", {"features": {"joints": {"cols": 5}}})
    assert lr.get(store, "cube-a")["status"] == "needs_review"
    lr.set_status(store, "cube-a", "needs_changes", "x")
    lr.save_brief(store, "cube-a", {"features": {"joints": {"cols": 6}}})
    assert lr.get(store, "cube-a")["status"] == "needs_changes"           # only Approved resets


def test_validation_and_feature_diff(store):
    bad = {**BRIEF, "features": {"niche": {"w": 1.4}}, "dims": [40.0, 40.0, 36.0]}
    v = lr.validate(store, "cube-a", bad)
    assert any("niche.w" in e for e in v["errors"])
    assert lr.validate(store, "cube-a", {**BRIEF, "dims": [40, -1, 36]})["errors"]
    with pytest.raises(ValueError):
        lr.save_brief(store, "cube-a", {"features": {"niche": {"w": 1.4}}})
    out = lr.save_brief(store, "cube-a", {"features": {"niche": {"w": 0.55}}})
    assert out["brief"]["features"] == {"niche": {"w": 0.55}}                # stored as the difference from the defaults


def test_locked_impact_only_for_size_changes_in_locked_frames(store, monkeypatch):
    monkeypatch.setattr(lr, "usage", lambda store, asset: {"shots": [
        {"shot": "med-a", "lock": "frame", "frac": 0.01, "frame": "frames/med-a/batch_000/frame_000.png"},
        {"shot": "wide-a", "lock": "greybox", "frac": 0.02}]})
    assert lr.locked_impact(store, "cube-a", BRIEF, {**BRIEF, "features": {"x": 1}}) == []
    imp = lr.locked_impact(store, "cube-a", BRIEF, {**BRIEF, "dims": [40.0, 40.0, 44.0]})
    assert [r["shot"] for r in imp] == ["med-a"] and imp[0]["changed"] == ["h"]


def test_validate_copes_with_a_landmark_type_without_size_ranges(store, monkeypatch):
    """A landmark typology (a forum) has no width / depth / storey ranges; validation (Asset
    review's form, the guidance edit check) must not crash on it."""
    from types import SimpleNamespace
    from asset_pipeline.stages import sw_site
    typ = SimpleNamespace(id="forum", width=None, depth=None, storeys=None, storey_m=None)
    monkeypatch.setattr(sw_site, "load_layout", lambda s: SimpleNamespace(city=SimpleNamespace(typologies=[typ], storey_m=3.4)))
    monkeypatch.setattr(lr, "entry_of", lambda s, a: {"id": a, "typology": "forum", "size_m": None})
    v = lr.validate(store, "cube-a", BRIEF)
    assert not v["errors"] and any("no size ranges" in w for w in v["warnings"])
