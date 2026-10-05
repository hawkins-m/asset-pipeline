import json

import pytest

from asset_pipeline.project import ProjectStore


@pytest.fixture(autouse=True)
def ap_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    return tmp_path


def test_create_open_roundtrip(ap_root):
    store = ProjectStore.create("demo", brief="mossy ruins")
    assert store.root == ap_root / "projects" / "demo"
    p = ProjectStore.open("demo").load()
    assert p.brief == "mossy ruins"
    assert p.backends["style"] == "comfyui"  # defaults from config/backends.toml


def test_duplicate_and_bad_slug_rejected():
    ProjectStore.create("demo")
    with pytest.raises(FileExistsError):
        ProjectStore.create("demo")
    with pytest.raises(ValueError):
        ProjectStore.create("Bad Slug!")


def test_save_is_atomic_and_leaves_no_temp_files():
    store = ProjectStore.create("demo")
    p = store.load()
    p.backends["references"] = "gemini"
    store.save(p)
    assert ProjectStore.open("demo").load().backends["references"] == "gemini"
    assert [f.name for f in store.root.iterdir()] == ["project.json"]


def test_log_run_appends_jsonl():
    store = ProjectStore.create("demo")
    store.log_run({"stage": "gen", "outputs": ["a.png"]})
    store.log_run({"stage": "gen", "outputs": ["b.png"]})
    lines = [json.loads(l) for l in (store.root / "runs.jsonl").read_text().splitlines()]
    assert [l["outputs"] for l in lines] == [["a.png"], ["b.png"]]
    assert all("time" in l for l in lines)


def test_json_files_get_normal_permissions():
    import os, stat
    store = ProjectStore.create("perms")
    mode = stat.S_IMODE(os.stat(store.project_file).st_mode)
    umask = os.umask(0); os.umask(umask)
    assert mode == 0o666 & ~umask
