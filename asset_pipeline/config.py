"""Paths and backend configuration."""
import os
import tomllib
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / "workflows"
BACKENDS_TOML = REPO_ROOT / "config" / "backends.toml"


def ap_root() -> Path:
    """Storage root for everything that must not live in the repo (see CLAUDE.md)."""
    return Path(os.environ.get("AP_ROOT", "/mnt/storage/asset-pipeline"))


def projects_dir() -> Path:
    return ap_root() / "projects"


def local_toml() -> Path:
    """Machine-specific overrides of backends.toml, outside the repo."""
    return ap_root() / "backends.local.toml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


@cache
def backends() -> dict:
    with BACKENDS_TOML.open("rb") as f:
        cfg = tomllib.load(f)
    if local_toml().exists():
        with local_toml().open("rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    return cfg
