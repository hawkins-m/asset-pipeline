#!/usr/bin/env python3
"""ap_config.py KEY [KEY ...]  -- print effective config values for shell scripts, one per line.

Runs with the system python3 (>= 3.11, stdlib only): it reads asset_pipeline/config.py straight
from the checkout, so the order is the app's (environment > user config > backends.local.toml >
repo defaults). Keys: paths.ap_root, paths.venv (the orchestrator venv), comfyui.ports (the
ComfyUI ports this machine runs: url's port plus other_ports), or any "section.key"."""
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asset_pipeline import config  # noqa: E402


def venv() -> str:
    v = config.get("paths.venv") or ""
    if v:
        return str(Path(v).expanduser())
    repo_venv = config.REPO_ROOT / ".venv"
    return str(repo_venv if (repo_venv / "bin" / "ap").exists() else config.ap_root() / "envs" / "orchestrator")


def ports() -> str:
    url = urlparse(config.get("comfyui.url") or "http://127.0.0.1:8188")
    return " ".join(str(p) for p in [url.port or 80, *(config.get("comfyui.other_ports") or [])])


def value(key: str) -> str:
    if key == "paths.venv":
        return venv()
    if key == "comfyui.ports":
        return ports()
    v = config.get(key, "")
    return " ".join(map(str, v)) if isinstance(v, list) else str(v)


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(1)
    for k in sys.argv[1:]:
        print(value(k))
