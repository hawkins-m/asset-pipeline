"""--prune safety: the editor script deletes only an expected count, or at most 10 without
one; past that, a confirmation reruns it with the exact count, and headless runs never do."""
import importlib.util
import sys
from unittest import mock

import pytest

from asset_pipeline import config, ue
from asset_pipeline.cli import _print_ue_report


@pytest.fixture(scope="module")
def ap_import():
    with mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()}):
        spec = importlib.util.spec_from_file_location("ap_import_under_test", config.REPO_ROOT / "unreal/ap_import.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def test_prune_rule(ap_import):
    assert ap_import.PRUNE_MAX == ue.PRUNE_MAX == 10
    assert ap_import.prune_allowed(4, None) and ap_import.prune_allowed(10, None)
    assert not ap_import.prune_allowed(11, None) and not ap_import.prune_allowed(291, None)
    assert ap_import.prune_allowed(291, 291)
    assert not ap_import.prune_allowed(291, 4) and not ap_import.prune_allowed(3, 4)   # expect is exact


def _orphans(n):
    types = ["housing", "park", "street"]
    return [{"label": f"AP_x_{i}", "type": types[i % 3]} for i in range(n)]


@pytest.fixture
def fake_import(monkeypatch, ap_import):
    """ue._import standing in for the editor run, applying the script's own rule."""
    calls = []

    def run(store, reset_layout, prune, expect, n=0, vegetation=False):
        calls.append({"prune": prune, "expect": expect})
        orphans = _orphans(state["n"])
        rep = {"orphans": orphans, "pruned": []}
        if prune and orphans:
            if ap_import.prune_allowed(len(orphans), expect, ue.PRUNE_MAX):
                rep["pruned"] = [o["label"] for o in orphans]
            else:
                rep["prune_refused"] = len(orphans)
        return rep
    state = {"n": 0}
    monkeypatch.setattr(ue, "_import", run)
    return calls, state


def test_small_prune_needs_no_confirmation(fake_import):
    calls, state = fake_import
    state["n"] = 4
    rep = ue.import_(None, prune=True)
    assert len(rep["pruned"]) == 4 and calls == [{"prune": True, "expect": None}]


def test_large_prune_is_refused_headless(fake_import):
    calls, state = fake_import
    state["n"] = 291
    rep = ue.import_(None, prune=True)                     # confirm=None: a headless run
    assert rep["pruned"] == [] and rep["prune_refused"] == 291 and len(calls) == 1


def test_large_prune_asks_with_the_breakdown_then_reruns_with_the_exact_count(fake_import):
    calls, state = fake_import
    state["n"] = 291
    asked = []
    rep = ue.import_(None, prune=True, confirm=lambda n, b: asked.append((n, b)) or True)
    assert asked == [(291, {"housing": 97, "park": 97, "street": 97})]
    assert len(rep["pruned"]) == 291
    assert calls == [{"prune": True, "expect": None}, {"prune": True, "expect": 291}]


def test_declined_confirmation_deletes_nothing(fake_import):
    calls, state = fake_import
    state["n"] = 291
    rep = ue.import_(None, prune=True, confirm=lambda n, b: False)
    assert rep["pruned"] == [] and len(calls) == 1


def test_expect_must_match(fake_import):
    calls, state = fake_import
    state["n"] = 291
    assert ue.import_(None, prune=True, expect=4)["prune_refused"] == 291
    assert len(ue.import_(None, prune=True, expect=291)["pruned"]) == 291


def test_no_prune_never_deletes(fake_import):
    calls, state = fake_import
    state["n"] = 3
    rep = ue.import_(None, confirm=lambda n, b: True)
    assert rep["pruned"] == [] and calls == [{"prune": False, "expect": None}]


def test_report_counts_orphans_and_warnings(capsys):
    rep = {"ok": True, "assets": {"imported": [], "skipped": []},
           "actors": {"created": [], "kept_transform": [], "reset": []}, "sequence": {"path": "s", "sections": 1},
           "checks": {"instances_total": 0, "cameras": 0, "nanite_off": [], "missing_mi": [], "instance_mismatch": []},
           "seconds": 1, "sample_errors": [], "warnings": ["a", "b"], "orphans": _orphans(12), "pruned": [],
           "prune_refused": 12}
    _print_ue_report(rep)
    out = capsys.readouterr().out
    assert "12 pipeline actors not in the manifest (housing 4, park 4, street 4); NOT pruned: needs --expect 12" in out
    assert "2 warning(s)" in out
