"""Blender path, AP_ROOT and GPU vendor settings. The NVIDIA branches run against mocks only."""
import os
import subprocess
from pathlib import Path

import pytest

from asset_pipeline import blender, config, gpu

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Config with an empty AP_ROOT (no local toml) and a clean cache."""
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    monkeypatch.delenv("AP_BLENDER", raising=False)
    config.backends.cache_clear()
    yield tmp_path
    config.backends.cache_clear()


def test_blender_precedence(cfg, monkeypatch):
    assert blender.blender_bin() == config.backends()["tools"]["blender"] == "blender"
    (cfg / "backends.local.toml").write_text('[tools]\nblender = "/opt/bl/blender"\n')
    config.backends.cache_clear()
    assert blender.blender_bin() == "/opt/bl/blender"
    monkeypatch.setenv("AP_BLENDER", "/env/blender")
    assert blender.blender_bin() == "/env/blender"


def test_blender_default_without_tools_section(cfg, monkeypatch):
    bare = cfg / "bare.toml"
    bare.write_text("[comfyui]\n")
    monkeypatch.setattr(config, "BACKENDS_TOML", bare)
    config.backends.cache_clear()
    assert blender.blender_bin() == "blender"


def test_blender_run_uses_resolved_binary(cfg, monkeypatch, tmp_path):
    monkeypatch.setenv("AP_BLENDER", "/env/blender")
    seen = []
    monkeypatch.setattr(blender, "run_logged", lambda cmd, log, what, cwd=None: seen.append(cmd))
    blender.run(Path("s.py"), ["a"], tmp_path / "l.log", "x")
    blender.run(Path("s.py"), [], tmp_path / "l.log", "x", exe=Path("/fake"))
    assert seen[0][0] == "/env/blender" and seen[1][0] == "/fake"


def test_ap_root_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("AP_ROOT", raising=False)
    assert config.ap_root() == Path(config.DEFAULT_AP_ROOT)       # the repo config's value
    other = tmp_path / "b.toml"
    other.write_text('[paths]\nap_root = "/from/config"\n')
    monkeypatch.setattr(config, "BACKENDS_TOML", other)
    assert config.ap_root() == Path("/from/config")
    monkeypatch.setenv("AP_ROOT", "/from/env")
    assert config.ap_root() == Path("/from/env")
    monkeypatch.setenv("AP_ROOT", "")      # empty counts as unset
    assert config.ap_root() == Path("/from/config")
    other.write_text("[comfyui]\n")
    assert config.ap_root() == Path(config.DEFAULT_AP_ROOT)


def _which(found):
    return lambda name: (f"/usr/bin/{Path(name).name}" if Path(name).name in found else None)


def test_vendor(monkeypatch):
    for found, want in [({"rocm-smi"}, "amd"), ({"nvidia-smi"}, "nvidia"), ({"rocm-smi", "nvidia-smi"}, "amd"), (set(), None)]:
        monkeypatch.setattr(gpu.shutil, "which", _which(found))
        assert gpu.vendor() == want


def test_visible_devices_env(monkeypatch):
    for found, want in [({"rocm-smi"}, {"HIP_VISIBLE_DEVICES": "1"}), ({"nvidia-smi"}, {"CUDA_VISIBLE_DEVICES": "1"}),
                        (set(), {"HIP_VISIBLE_DEVICES": "1", "CUDA_VISIBLE_DEVICES": "1"})]:
        monkeypatch.setattr(gpu.shutil, "which", _which(found))
        assert gpu.visible_devices_env(1) == want


def _run(stdout, calls):
    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


def test_vram_amd(monkeypatch):
    calls = []
    out = '{"card0": {"VRAM Total Used Memory (B)": "2000000000"}, "card1": {"VRAM Total Used Memory (B)": "500000000"}, "system": {}}'
    monkeypatch.setattr(gpu.shutil, "which", _which({"rocm-smi"}))
    monkeypatch.setattr(gpu.subprocess, "run", _run(out, calls))
    assert gpu.vram_used_gb() == {0: 2.0, 1: 0.5}
    assert calls[0][1:] == ["--showmeminfo", "vram", "--json"] and calls[0][0].endswith("rocm-smi")


def test_vram_nvidia(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu.shutil, "which", _which({"nvidia-smi"}))
    monkeypatch.setattr(gpu.subprocess, "run", _run("0, 1907\n1, 0\n", calls))
    v = gpu.vram_used_gb()
    assert v[0] == pytest.approx(1907 * 1048576 / 1e9) and v[1] == 0
    assert calls[0] == ["nvidia-smi", "--query-gpu=index,memory.used", "--format=csv,noheader,nounits"]


def test_ue_gpu_vram_delegates(monkeypatch):
    from asset_pipeline import ue
    monkeypatch.setattr(gpu, "vram_used_gb", lambda: {0: 1.5})
    assert ue.gpu_vram() == {0: 1.5}


def test_shell_scripts_parse():
    changed = ["ap", "ap_root.sh", "build_env_common.sh", "torch_index.sh", "rocm_build_env.sh", "trellis",
               "install_qwen_vl.sh", "install_segdepth.sh", "install_trellis2.sh", "install_llamacpp_vlm.sh",
               "llamacpp_vlm_server.sh", "comfy_api_proxy.sh", "setup_rocm_toolchain.sh", "git-hooks/pre-commit"]
    for name in changed:
        r = subprocess.run(["bash", "-n", str(REPO / "scripts" / name)], capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def _sh(snippet, env=None):
    e = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"], **(env or {})}
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=e)


def test_ap_root_sh(tmp_path):
    src = f'source {REPO}/scripts/ap_root.sh; echo "$AP_ROOT"'
    assert _sh(src, {"AP_ROOT": str(tmp_path)}).stdout.strip() == str(tmp_path)
    assert _sh(src).stdout.strip() == config.DEFAULT_AP_ROOT
    assert _sh(src, {"AP_ROOT": ""}).stdout.strip() == config.DEFAULT_AP_ROOT


def test_torch_index_sh():
    src = f'source {REPO}/scripts/torch_index.sh; echo "$TORCH_KIND $TORCH_INDEX"'
    assert _sh(src).stdout.strip() == "rocm https://download.pytorch.org/whl/rocm7.2"
    r = _sh(src, {"AP_TORCH": "cu128"})
    assert r.stdout.strip() == "cuda https://download.pytorch.org/whl/cu128" and "untested" in r.stderr
    for bad in ["cu118", "rocm6.4", "cpu", "cu12"]:
        r = _sh(src, {"AP_TORCH": bad})
        assert r.returncode != 0 and "AP_TORCH must be" in r.stderr


def test_ap_gpu_vendor_sh(tmp_path):
    """With only a fake nvidia-smi on PATH the helper says nvidia (unless this machine has ROCm)."""
    (tmp_path / "nvidia-smi").write_text("#!/bin/sh\n")
    (tmp_path / "nvidia-smi").chmod(0o755)
    path = f"{tmp_path}:{os.environ['PATH']}"
    r = _sh(f'AP_ROOT=/x; source {REPO}/scripts/build_env_common.sh; ap_gpu_vendor', {"PATH": path})
    expect = "amd" if Path("/opt/rocm/bin/rocm-smi").exists() else "nvidia"
    assert r.stdout.strip() == expect


def test_pre_commit_lets_only_approved_docs_images_through(tmp_path):
    """Images pass only directly in docs/images/ and only when the committed APPROVED.txt names them."""
    repo, root = tmp_path / "r", tmp_path / "root"
    repo.mkdir(), root.mkdir()
    env = {"AP_ROOT": str(root), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True,
                              env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **env})
    git("init", "-q")
    git("config", "core.hooksPath", str(REPO / "scripts" / "git-hooks"))
    img = repo / "docs" / "images"
    (img / "sub").mkdir(parents=True)
    for name in ("ok.png", "other.png", "sub/ok.png"):
        (img / name).write_bytes(b"\x89PNG")
    (img / "APPROVED.txt").write_text("# list\nok.png   # fine\n")

    def commit(*paths):
        git("reset", "-q")
        git("add", *paths)
        return git("commit", "-qm", "x")
    assert commit("docs/images/ok.png").returncode != 0                     # the list isn't committed yet
    r = commit("docs/images/APPROVED.txt", "docs/images/ok.png")
    assert r.returncode == 0, r.stderr
    assert commit("docs/images/other.png").returncode != 0                  # not listed
    assert commit("docs/images/sub/ok.png").returncode != 0                 # a subfolder never passes
    (repo / "shot.png").write_bytes(b"\x89PNG")
    assert "shot.png" in commit("shot.png").stderr                          # anywhere else: refused as before
    (img / "APPROVED.txt").write_text("ok.png\nother.png\n")                # listed but the list not staged
    assert commit("docs/images/other.png").returncode != 0
