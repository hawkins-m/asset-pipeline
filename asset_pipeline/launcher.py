"""Desktop launcher for the Terraformer UI: `ap launcher install|remove|status`.

Writes, under the user's home (never the repo):
  ~/.local/share/applications/terraformer.desktop   the menu entry (Name=Terraformer)
  ~/.local/share/icons/hicolor/scalable/apps/terraformer.svg   a simple neutral icon
  ~/.local/bin/terraformer-launch                   the script the entry runs

The script asks http://127.0.0.1:PORT/api/version; if nothing answers it starts `ap ui` detached
(setsid, nohup, log to $AP_ROOT/logs/ui.log) with the venv that was running when the launcher
was installed, waits up to 60 s, then opens the page with xdg-open.
"""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

APP_NAME = "terraformer"
DEFAULT_PORT = 8700

ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
  <rect width="128" height="128" rx="22" fill="#2f3b4a"/>
  <path d="M10 98 L44 50 L66 76 L84 56 L118 98 Z" fill="#8fb6a0"/>
  <path d="M10 98 L44 50 L58 68 L30 98 Z" fill="#6f9b86"/>
  <circle cx="92" cy="34" r="11" fill="#e9c46a"/>
  <path d="M10 98 H118 V106 H10 Z" fill="#1f2833"/>
</svg>
"""

SCRIPT = """#!/usr/bin/env bash
# Terraformer launcher (written by `ap launcher install`; rewrite it by installing again).
# Opens the UI, starting `ap ui` first if it is not running.
PORT={port}
URL="http://127.0.0.1:$PORT"
AP_BIN={ap_bin}
export AP_ROOT={ap_root}
up() {{ curl -fsS -m 2 "$URL/api/version" >/dev/null 2>&1; }}
if ! up; then
  mkdir -p "$AP_ROOT/logs"
  setsid nohup "$AP_BIN" ui --port "$PORT" >>"$AP_ROOT/logs/ui.log" 2>&1 </dev/null &
  for _ in $(seq 1 60); do
    up && break
    sleep 1
  done
fi
if up; then
  exec xdg-open "$URL/"
fi
echo "Terraformer did not start within 60 s; see $AP_ROOT/logs/ui.log" >&2
command -v notify-send >/dev/null 2>&1 && notify-send "Terraformer" "The UI did not start: see $AP_ROOT/logs/ui.log"
exit 1
"""

DESKTOP = """[Desktop Entry]
Type=Application
Version=1.0
Name=Terraformer
Comment=Terraformer Pipeline: concept images to Unreal Engine environments
Exec={exec}
Icon={icon}
Categories=Graphics;3DGraphics;
Terminal=false
StartupNotify=false
"""


def _home(home: Path | str | None) -> Path:
    return Path(home) if home else Path(os.environ.get("HOME") or Path.home())


def _data_home(home: Path) -> Path:
    x = os.environ.get("XDG_DATA_HOME")
    return Path(x) if x else home / ".local" / "share"


def paths(home: Path | str | None = None) -> dict[str, Path]:
    h = _home(home)
    d = _data_home(h)
    return {"desktop": d / "applications" / f"{APP_NAME}.desktop",
            "icon": d / "icons" / "hicolor" / "scalable" / "apps" / f"{APP_NAME}.svg",
            "script": h / ".local" / "bin" / f"{APP_NAME}-launch"}


def ap_binary() -> Path:
    """The `ap` of the venv running now (or AP_VENV)."""
    here = Path(sys.executable).parent / "ap"
    if here.is_file():
        return here
    venv = os.environ.get("AP_VENV")
    if venv and (Path(venv) / "bin" / "ap").is_file():
        return Path(venv) / "bin" / "ap"
    return here


def status(home: Path | str | None = None) -> dict:
    p = paths(home)
    return {k: {"path": str(v), "exists": v.is_file()} for k, v in p.items()} | {
        "installed": all(v.is_file() for v in p.values())}


def _update_db(desktop: Path) -> None:
    if shutil.which("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", str(desktop.parent)], capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass                                        # the entry works without the cache


def install(home: Path | str | None = None, port: int = DEFAULT_PORT, ap_bin: Path | str | None = None,
            update_db: bool = True) -> dict:
    p = paths(home)
    ap_bin = Path(ap_bin) if ap_bin else ap_binary()
    for f in p.values():
        f.parent.mkdir(parents=True, exist_ok=True)
    p["script"].write_text(SCRIPT.format(port=int(port), ap_bin=shlex.quote(str(ap_bin)),
                                         ap_root=shlex.quote(str(config.ap_root()))))
    p["script"].chmod(0o755)
    p["icon"].write_text(ICON_SVG)
    p["desktop"].write_text(DESKTOP.format(exec=str(p["script"]), icon=str(p["icon"])))
    p["desktop"].chmod(0o755)
    if update_db:
        _update_db(p["desktop"])
    return {k: str(v) for k, v in p.items()}


def remove(home: Path | str | None = None, update_db: bool = True) -> list[str]:
    p = paths(home)
    gone = []
    for f in p.values():
        if f.is_file():
            f.unlink()
            gone.append(str(f))
    if gone and update_db:
        _update_db(p["desktop"])
    return gone
