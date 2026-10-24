"""The user config (~/.config/terraformer): order, writing, secrets, migration of an older install."""
import os
import stat
import tomllib

import pytest

from asset_pipeline import blender, cli, config


def test_order_env_over_user_over_local_over_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    (tmp_path / "backends.local.toml").write_text('[tools]\nblender = "/local/blender"\n[comfyui]\nurl = "http://h:1"\n')
    config.backends.cache_clear()
    assert config.get("tools.blender") == "/local/blender" and config.get("comfyui.timeout_s") == 900
    config.save_user({"tools": {"blender": "/user/blender"}})
    assert blender.blender_bin() == "/user/blender" and config.get("comfyui.url") == "http://h:1"
    monkeypatch.setenv("AP_BLENDER", "/env/blender")
    monkeypatch.setenv("AP_GPU_COMFY", "0")
    config.backends.cache_clear()
    assert config.get("tools.blender") == "/env/blender" and config.get("jobs.comfy_gpu") == 0


def test_save_user_round_trips_and_removes(tmp_path):
    config.save_user({"comfyui": {"url": 'http://a "b"', "other_ports": [8189], "service": {"gpu": "1"}},
                      "jobs": {"gpu0_gpu": 0}, "unreal": {"backup_keep": 20}})
    config.save_user({"tools": {"blender": "/b"}}, remove=["comfyui.service.gpu"])
    data = tomllib.loads(config.user_toml().read_text())
    assert data["comfyui"]["url"] == 'http://a "b"' and data["comfyui"]["other_ports"] == [8189]
    assert data["comfyui"]["service"] == {} or "gpu" not in data["comfyui"].get("service", {})
    assert data["jobs"]["gpu0_gpu"] == 0 and data["tools"]["blender"] == "/b" and data["unreal"]["backup_keep"] == 20


def test_secrets_file_is_private_and_env_wins(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("COMFY_API_KEY", "from-env")
    config.save_secret("GEMINI_API_KEY", "g-key")
    config.save_secret("COMFY_API_KEY", "c-file")
    monkeypatch.setenv("COMFY_API_KEY", "from-env")
    monkeypatch.delenv("GEMINI_API_KEY")
    assert stat.S_IMODE(config.secrets_toml().stat().st_mode) == 0o600
    assert stat.S_IMODE(config.config_dir().stat().st_mode) == 0o700
    assert config.load_secrets() == ["GEMINI_API_KEY"]
    assert os.environ["GEMINI_API_KEY"] == "g-key" and os.environ["COMFY_API_KEY"] == "from-env"
    assert "g-key" not in config.user_toml().read_text() if config.user_toml().exists() else True
    with pytest.raises(ValueError):
        config.save_secret("PATH", "x")
    config.save_secret("GEMINI_API_KEY", "")
    assert "GEMINI_API_KEY" not in config.secrets()


def test_a_readable_secrets_file_is_not_loaded(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config.save_secret("GEMINI_API_KEY", "g")
    monkeypatch.delenv("GEMINI_API_KEY")
    config.secrets_toml().chmod(0o644)
    assert config.load_secrets() == [] and "GEMINI_API_KEY" not in os.environ
    assert "chmod 600" in capsys.readouterr().err


def test_migrate_keeps_an_older_install_working(tmp_path, monkeypatch):
    root = tmp_path / "old-root"
    root.mkdir()
    (root / "backends.local.toml").write_text('[comfyui]\nbackend = "httpx"\n[comfyui.service]\ngpu = "1"\n'
                                              '[tools]\nblender = "/snap/bin/blender"\n')
    monkeypatch.delenv("AP_ROOT", raising=False)
    path = config.migrate(root, {"unreal": {"editor_cmd": "/ue/UnrealEditor-Cmd", "projects_dir": "~/UE"}})
    assert path == config.user_toml()
    assert config.ap_root() == root and config.get("comfyui.backend") == "httpx"
    assert config.get("unreal.editor_cmd") == "/ue/UnrealEditor-Cmd" and config.get("tools.blender") == "/snap/bin/blender"
    assert config.migrate(root) is None                                   # never overwrites


def test_ap_config_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AP_ROOT", str(tmp_path))
    cli.main(["config", "set", "jobs.comfy_gpu", "0"])
    cli.main(["config", "set", "comfyui.service.launch", "~/ComfyUI/run.sh --port {port}"])
    config.backends.cache_clear()
    assert config.get("jobs.comfy_gpu") == 0 and config.get("comfyui.service.launch") == "~/ComfyUI/run.sh --port {port}"
    capsys.readouterr()
    cli.main(["config", "get", "jobs.comfy_gpu"])
    assert capsys.readouterr().out.strip() == "0"
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    cli.main(["config", "show"])
    out = capsys.readouterr().out
    assert "secret-value" not in out and "GEMINI_API_KEY" in out and f'ap_root = "{tmp_path}"' in out
