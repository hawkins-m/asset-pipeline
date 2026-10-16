"""Stale vs hand-moved slot actors, and the selective --reset-layout --only-stale."""
import importlib.util
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest import mock

import numpy as np
import pytest

from asset_pipeline import cli, config, layout_history, ue, ue_coords


def mat(x, y, yaw_deg=0.0, z=0.0):
    c, s = np.cos(np.radians(yaw_deg)), np.sin(np.radians(yaw_deg))
    m = np.eye(4)
    m[:2, :2] = [[c, -s], [s, c]]
    m[:3, 3] = [x, y, z]
    return m.tolist()


def test_same_place_tolerances():
    a = np.asarray(mat(10, 20, 30))
    assert layout_history.same_place(a, mat(10.009, 20, 30))          # 9 mm
    assert not layout_history.same_place(a, mat(10.02, 20, 30))       # 2 cm
    assert not layout_history.same_place(a, mat(10, 20, 30.5))        # half a degree
    b = np.asarray(mat(10, 20, 30))
    b[:3, :3] *= 1.01                                                 # 1% scale
    assert not layout_history.same_place(a, b)


def test_classify_current_stale_hand_and_orphans():
    current = {"a": mat(0, 0), "b": mat(50, 0), "c": mat(100, 0, 90), "d": mat(5, 5)}
    versions = {"new": current, "old": {"b": mat(40, 0), "c": mat(90, 0, 45), "gone": mat(1, 1)},
                "older": {"b": mat(40, 0)}}
    ue_actors = {"a": ue_coords.to_ue(mat(0, 0)),             # where the current greybox puts it
                 "b": ue_coords.to_ue(mat(40, 0)),            # an earlier greybox's place
                 "c": ue_coords.to_ue(mat(93, 0, 45)),        # nobody's place: moved by hand
                 "d": ue_coords.to_ue(mat(5.004, 5)),         # 4 mm off: still current
                 "gone": ue_coords.to_ue(mat(1, 1))}          # not in the layout any more: prune's job
    res = layout_history.classify(ue_actors, current, versions)
    assert res == {"current": ["a", "d"], "stale": {"b": ["old", "older"]}, "hand": ["c"]}


@pytest.fixture(scope="module")
def ap_import():
    with mock.patch.dict(sys.modules, {"unreal": mock.MagicMock()}):
        spec = importlib.util.spec_from_file_location("ap_import_reset", config.REPO_ROOT / "unreal/ap_import.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def test_importer_resets_only_the_listed_slots(ap_import):
    assert not ap_import.resets("b", {})                                       # never without reset_layout
    assert ap_import.resets("b", {"reset_layout": True})                       # all
    assert ap_import.resets("b", {"reset_layout": True, "reset_only": ["b"]})
    assert not ap_import.resets("c", {"reset_layout": True, "reset_only": ["b"]})
    assert not ap_import.resets("b", {"reset_layout": True, "reset_only": []})  # nothing stale


def test_only_stale_resets_just_the_stale_ids(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "ProjectStore", SimpleNamespace(open=lambda slug: "store"))
    monkeypatch.setattr(layout_history, "check", lambda store, log=print:
                        {"current": ["a"], "stale": {"s2": ["x"], "s1": ["y"]}, "hand": ["h1"]})
    monkeypatch.setattr(ue, "import_", lambda store, **kw: calls.append(kw) or {})
    monkeypatch.setattr(cli, "_print_ue_report", lambda rep: None)
    cli.main(["ue", "import", "p", "--reset-layout", "--only-stale"])
    assert calls[0]["reset_layout"] and calls[0]["reset_only"] == ["s1", "s2"]
    out = capsys.readouterr().out
    assert "1 slots where the current greybox puts them; 2 stale" in out and "moved by hand (kept): h1" in out
    with pytest.raises(SystemExit, match="--reset-layout"):
        cli.main(["ue", "import", "p", "--only-stale"])


def test_greybox_versions_reads_each_committed_blend_once(tmp_path, monkeypatch):
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t"), git("config", "user.name", "t")
    (tmp_path / "site").mkdir()
    for i in range(2):
        (tmp_path / "site/greybox.blend").write_bytes(f"blend {i}".encode())
        git("add", "site/greybox.blend")
        git("commit", "-q", "--no-verify", "-m", f"v{i}")
    runs = []

    def fake_run(script, args, log, what, blend=None):
        content = blend.read_bytes().decode()
        runs.append(content)
        args[1].write_text(json.dumps({"slots": [{"id": "s", "matrix": mat(float(content[-1]), 0)}]}))
    monkeypatch.setattr(layout_history.blender, "run", fake_run)
    store = SimpleNamespace(root=tmp_path)
    v = layout_history.greybox_versions(store, log=lambda s: None)
    assert sorted(runs) == ["blend 0", "blend 1"] and len(v) == 2
    assert sorted(m["s"][0][3] for m in v.values()) == [0.0, 1.0]
    layout_history.greybox_versions(store, log=lambda s: None)
    assert len(runs) == 2                                   # cached in site/build/history
    assert not list((tmp_path / "site/build/history").glob("*.blend"))
