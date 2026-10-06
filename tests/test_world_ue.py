"""World mode phases 4-5: manifest building, import checks, the UE runner (no UE needed)."""
import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

from asset_pipeline import ue, ue_coords
from asset_pipeline.project import ProjectStore, write_json
from asset_pipeline.stages import s7_export, sw_site

REAL_UE = Path("/mnt/storage/UnrealEngine/5.8.3/Engine/Binaries/Linux/UnrealEditor-Cmd")


def mat(yaw=0.0, loc=(0, 0, 0)):
    a = math.radians(yaw)
    m = np.eye(4)
    m[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
    m[:3, 3] = loc
    return m.tolist()


GB = {"blend_sha256": "x", "untagged": [], "fixed": [],
      "slots": [{"id": "t-1", "type": "temple", "category": "building", "kit": "k", "district": "core",
                 "matrix": mat(30, (10, 20, 5)), "bbox": [[0, 0, 0], [1, 1, 1]],
                 "pieces": [{"name": "a", "piece": "k:col", "mesh": "k:col#1", "matrix_local": mat(0, (2, 0, 0))},
                            {"name": "b", "piece": "k:col", "mesh": "k:col#1", "matrix_local": mat(0, (-2, 0, 0))},
                            {"name": "c", "piece": "mass", "mesh": "mass#2", "matrix_local": mat()}]},
                {"id": "sea", "type": "sea", "category": "terrain", "kit": None, "district": None,
                 "matrix": mat(), "bbox": [[0, 0, 0], [1, 1, 1]],
                 "pieces": [{"name": "sea", "piece": "sea", "mesh": "sea", "matrix_local": mat()}]}],
      "shots": [{"id": "close", "tier": "tight", "matrix": mat(), "lens_mm": 50, "sensor_mm": 36,
                 "resolution": [1600, 900], "district": None, "notes": ""},
                {"id": "far", "tier": "wide", "matrix": mat(), "lens_mm": 24, "sensor_mm": 36,
                 "resolution": [1344, 768], "district": None, "notes": "x"}]}
INDEX = {"k:col#1": {"id": "k-col-1", "glb": "k-col-1/SM_k-col-1.glb", "faces": 10, "bounds": [[0] * 3, [1] * 3]},
         "mass#2": {"id": "mass-2", "glb": "mass-2/SM_mass-2.glb", "faces": 6, "bounds": [[0] * 3, [1] * 3]},
         "sea": {"id": "sea", "glb": "sea/SM_sea.glb", "faces": 1, "bounds": [[0] * 3, [1] * 3]}}


def test_manifest_groups_pieces_into_instanced_components_with_clean_names():
    m = s7_export.manifest(GB, INDEX, project="my-world")
    assert s7_export.counts(m) == {"assets": 3, "slots": 2, "instances": 4, "shots": 2}
    t = m["slots"][0]
    assert t["label"] == "AP_core_t-1" and t["folder"] == "AP/core/building"
    assert "ap:id=t-1" in t["tags"] and "ap:kit=k" in t["tags"] and "ap:source=pipeline" in t["tags"]
    assert [c["asset"] for c in t["components"]] == ["k-col-1", "mass-2"]
    assert [i["loc"] for i in t["components"][0]["instances"]] == [[200, 0, 0], [-200, 0, 0]]  # relative, cm
    assert t["transform"]["loc"] == pytest.approx([1000, -2000, 500]) and t["transform"]["rot"][2] == pytest.approx(-30)
    assert m["slots"][1]["label"] == "AP_site_sea" and m["assets"][2]["tint"] == list(s7_export.TINTS["sea"])
    assert [s["id"] for s in m["shots"]] == ["far", "close"]                     # wide before tight
    assert m["shots"][1]["sensor_height_mm"] == pytest.approx(36 * 900 / 1600)
    assert m["sequence"]["name"] == "LS_MyWorld" and m["map"] == "/Game/AP/Maps/MyWorld"
    with pytest.raises(ValueError, match="not exported"):
        s7_export.manifest(GB, {k: v for k, v in INDEX.items() if k != "sea"})


def test_landscape_values_cover_the_heightmap():
    m = s7_export.manifest(GB, INDEX, sw_site.starter_layout(), "w")
    t = sw_site.starter_layout().terrain
    ls = m["landscape"]
    assert ls["scale"][0] * (t.resolution - 1) == pytest.approx(t.extent_m * 100)
    lo, hi = t.z_range
    assert ls["location"][2] - 32768 / 128 * ls["scale"][2] == pytest.approx(lo * 100, abs=1)   # v = 0
    assert ls["location"][2] + 32767 / 128 * ls["scale"][2] == pytest.approx(hi * 100, abs=1)   # v = 65535


def test_verify_samples_follows_moved_actors_and_catches_misplacement():
    m = s7_export.manifest(GB, INDEX, project="w")
    actor = m["slots"][0]["transform"]
    want = ue_coords.C @ (np.asarray(GB["slots"][0]["matrix"]) @ np.asarray(mat(0, (2, 0, 0))))[:3, 3] * 100
    good = {"slot": "t-1", "asset": "k-col-1", "actor": actor, "world_cm": want.tolist()}
    assert ue.verify_samples(GB, INDEX, [good]) == ([], [])
    moved_actor = dict(actor, loc=[actor["loc"][0] + 500, actor["loc"][1], actor["loc"][2]])
    moved = dict(good, actor=moved_actor, world_cm=(want + [500, 0, 0]).tolist())
    assert ue.verify_samples(GB, INDEX, [moved]) == ([], ["t-1"])
    wrong = dict(good, world_cm=(want + [0, 0, 50]).tolist())     # e.g. a yaw sign error would do this
    errors, _ = ue.verify_samples(GB, INDEX, [wrong])
    assert errors and "50.0 cm" in errors[0]


FAKE_EDITOR = r"""#!/usr/bin/env python3
import json, sys
script = next(a for a in sys.argv if a.startswith("-script=")).split("=", 1)[1]
args = json.load(open(script.split()[-1]))
print("LogPython: Error: something noisy")
json.dump({"ok": True, "echo": sys.argv[1:4]}, open(args["report"], "w"))
sys.exit(1)        # the commandlet exits 1 whenever an error was logged
"""


def test_run_script_trusts_the_report_over_the_exit_code(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    fake = tmp_path / "UnrealEditor-Cmd"
    fake.write_text(FAKE_EDITOR)
    fake.chmod(0o755)
    monkeypatch.setenv("AP_UE_EDITOR_CMD", str(fake))
    monkeypatch.setenv("AP_UE_PROJECTS", str(tmp_path / "ue"))
    store = ProjectStore.create("demo-world")
    rep = ue.run_script(store, "ap_import.py", {"manifest": "m.json"}, "test")
    up = ue.uproject(store)
    assert up == tmp_path / "ue/demo-world/DemoWorld.uproject" and json.loads(up.read_text())["EngineAssociation"] == "5.8"
    assert "SF_VULKAN_SM6" in (up.parent / "Config/DefaultEngine.ini").read_text()
    assert rep["ok"] and rep["echo"][0] == str(up) and rep["echo"][1] == "-run=pythonscript"
    assert rep["exit"].startswith("UnrealEditor-Cmd exited with code 1")
    assert rep["log_errors"] == ["LogPython: Error: something noisy"]


def test_missing_editor_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_UE_EDITOR_CMD", str(tmp_path / "nope"))
    with pytest.raises(FileNotFoundError, match="UnrealEditor-Cmd not found"):
        ue.editor_cmd()


@pytest.fixture
def ue_project(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("AP_UE_PROJECTS", str(tmp_path / "ue"))
    monkeypatch.setenv("AP_UE_BACKUPS", str(tmp_path / "backups"))
    store = ProjectStore.create("demo-world")
    up = ue.init(store)
    for rel in ("Content/AP/Maps/Demo.umap", "Content/Lighting/Sun.uasset", "Intermediate/cache.bin",
                "Saved/Logs/editor.log", "DerivedDataCache/ddc.bin"):
        (up.parent / rel).parent.mkdir(parents=True, exist_ok=True)
        (up.parent / rel).write_bytes(b"x" * 1000)
    stamps = iter(f"20261006-0000{i:02d}" for i in range(60))
    monkeypatch.setattr(ue.time, "strftime", lambda fmt: next(stamps))
    return store, up


def test_backup_snapshots_exclude_caches_hardlink_and_rotate(ue_project):
    store, up = ue_project
    first = ue.backup(store, keep=3)
    snap = Path(first["snapshot"])
    assert (snap / "Content/Lighting/Sun.uasset").is_file() and (snap / up.name).is_file()
    assert not any((snap / d).exists() for d in ("Intermediate", "Saved", "DerivedDataCache"))
    (up.parent / "Content/Lighting/Sun.uasset").write_bytes(b"y" * 1000)       # your lighting work
    second = ue.backup(store, keep=3)
    s2 = Path(second["snapshot"])
    assert (s2 / "Content/Lighting/Sun.uasset").read_bytes() == b"y" * 1000
    assert (snap / "Content/Lighting/Sun.uasset").read_bytes() == b"x" * 1000   # old copy intact
    assert os.stat(s2 / "Content/AP/Maps/Demo.umap").st_ino == os.stat(snap / "Content/AP/Maps/Demo.umap").st_ino
    for _ in range(3):
        rep = ue.backup(store, keep=3)
    assert len(rep["kept"]) == 3 and [p.name for p in ue.backups(store)] == rep["kept"]
    assert (Path(rep["snapshot"]).parent / "latest").resolve() == Path(rep["snapshot"]).resolve()
    assert not snap.exists() and not list(Path(rep["snapshot"]).parent.glob("*.partial"))


def test_backup_refuses_while_the_editor_has_the_project_open(ue_project, monkeypatch):
    store, _ = ue_project
    monkeypatch.setattr(ue, "editor_running", lambda up: True)
    with pytest.raises(RuntimeError, match="open"):
        ue.backup(store)
    assert ue.backup(store, force=True)["files"] > 0
