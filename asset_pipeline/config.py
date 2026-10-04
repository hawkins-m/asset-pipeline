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


@cache
def backends() -> dict:
    with BACKENDS_TOML.open("rb") as f:
        return tomllib.load(f)
