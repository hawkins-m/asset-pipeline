"""Paths and backend configuration.

One user config outside the repo, `~/.config/terraformer/config.toml` (`$XDG_CONFIG_HOME`;
`AP_CONFIG_DIR` moves the folder), holds everything about this machine: storage folder, Blender,
ComfyUI, Unreal, GPU assignment. API keys go in `secrets.toml` beside it (mode 0600), loaded
into the environment at startup. Order, first wins:
    environment variables (ENV_OVERRIDES, AP_ROOT) > user config.toml
    > $AP_ROOT/backends.local.toml (older installs; still read) > the repo's config/backends.toml
The repo file holds only defaults that fit any machine. Stdlib only: scripts/ap_config.py
imports this module with the system python3.
"""
import os
import re
import tomllib
from functools import cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / "workflows"
BACKENDS_TOML = REPO_ROOT / "config" / "backends.toml"

# environment variable -> the config key it overrides
ENV_OVERRIDES = {
    "AP_BLENDER": ("tools", "blender"),
    "AP_COMFY_URL": ("comfyui", "url"),
    "AP_COMFY_BACKEND": ("comfyui", "backend"),
    "AP_COMFY_SDK_URL": ("comfyui", "sdk_url"),
    "AP_COMFY_MODELS": ("comfyui", "models_dir"),
    "AP_DINOV3_REPO": ("models", "dinov3_repo"),
    "AP_UE_EDITOR_CMD": ("unreal", "editor_cmd"),
    "AP_UE_PROJECTS": ("unreal", "projects_dir"),
    "AP_UE_BACKUPS": ("unreal", "backup_dir"),
    "AP_GPU_VISION": ("jobs", "gpu0_gpu"),
    "AP_GPU_COMFY": ("jobs", "comfy_gpu"),
}
# Every other environment variable the code or scripts read, with what it does. Settings belong in
# the user config; these are run-time switches, build knobs and standard variables.
# tests/test_registry.py fails on a read of an AP_/COMFY/HF_/TRELLIS_/PROXY_ variable not listed here
# (or in ENV_OVERRIDES / SECRET_KEYS).
ENV_VARS = {
    "AP_ROOT": "storage folder for this shell (wins over [paths] ap_root)",
    "AP_CONFIG_DIR": "the user config folder (default ~/.config/terraformer)",
    "AP_VENV": "the orchestrator venv scripts/ap runs (default [paths] venv)",
    "AP_ALLOW_PAID_APIS": "1 lets paid backends run for one command",
    "AP_UE_ARGS": "extra UnrealEditor-Cmd arguments (debugging)",
    "AP_UE_BACKUP_KEEP": "UE snapshots kept ([unreal] backup_keep)",
    "AP_TORCH": "PyTorch build for model venvs: rocm7.2 or cu128 (scripts/torch_index.sh; the hardware profile)",
    "AP_PROFILE": "hardware profile for this shell ([hardware] profile)",
    "COMFYUI_URL": "ComfyUI address for scripts/comfy_api_proxy.sh (ap passes [comfyui] url)",
    "COMFYUI_MODELS": "scripts/fetch_models.sh: ComfyUI models folder (same as AP_COMFY_MODELS)",
    "COMFY_BASE_URL": "set by sdk_client for comfy-sdk while it is constructed",
    "HF_ENDPOINT": "Hugging Face mirror for downloads (default https://huggingface.co)",
    "HF_HOME": "Hugging Face cache for model venvs (<storage>/hf)",
    "TRELLIS_GPU": "GPU for one scripts/trellis run ([jobs] gpu0_gpu)",
    "PROXY_PORT": "comfy-api-proxy port (ap passes [comfyui] sdk_url's port)",
    "PROXY_VERSION": "comfy-api-proxy version scripts/comfy_api_proxy.sh installs",
    "GFX1201_GUARD": "scripts/gfx1201_guard.py switch (ROCm gfx1201 workarounds)",
}

# API keys secrets.toml may hold ([keys] NAME = "value"); the environment wins over the file
SECRET_KEYS = ("COMFY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "HF_TOKEN")


def config_dir() -> Path:
    if os.environ.get("AP_CONFIG_DIR"):
        return Path(os.environ["AP_CONFIG_DIR"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "terraformer"


def user_toml() -> Path:
    return config_dir() / "config.toml"


def secrets_toml() -> Path:
    return config_dir() / "secrets.toml"


def default_ap_root() -> Path:
    """Where storage goes when nothing sets it: $XDG_DATA_HOME/terraformer (~/.local/share/terraformer)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "terraformer"


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def user() -> dict:
    """The user config as written (no defaults)."""
    return _read(user_toml())


def ap_root() -> Path:
    """Storage root for everything that must not live in the repo (venvs, models, projects,
    outputs): env AP_ROOT > [paths] ap_root in the user config > in the repo's backends.toml
    (older checkouts) > default_ap_root(). Not settable from backends.local.toml, which lives
    inside it."""
    if os.environ.get("AP_ROOT"):
        return Path(os.environ["AP_ROOT"]).expanduser()
    for src in (user(), _read(BACKENDS_TOML)):
        if (src.get("paths") or {}).get("ap_root"):
            return Path(src["paths"]["ap_root"]).expanduser()
    return default_ap_root()


def projects_dir() -> Path:
    return ap_root() / "projects"


def local_toml() -> Path:
    """Machine overrides from before the user config (older installs), inside AP_ROOT."""
    return ap_root() / "backends.local.toml"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _env_value(raw: str):
    return int(raw) if re.fullmatch(r"-?\d+", raw) else raw


def env_overrides() -> dict:
    out: dict = {}
    for env, (sec, key) in ENV_OVERRIDES.items():
        if os.environ.get(env):
            out.setdefault(sec, {})[key] = _env_value(os.environ[env])
    return out


@cache
def backends() -> dict:
    """The effective config: repo defaults < backends.local.toml < user config < environment."""
    cfg = _read(BACKENDS_TOML)
    if local_toml().exists():
        cfg = _merge(cfg, _read(local_toml()))
    return _merge(_merge(cfg, user()), env_overrides())


def get(dotted: str, default=None):
    """One effective value by "section.key" (paths.ap_root is ap_root())."""
    if dotted == "paths.ap_root":
        return str(ap_root())
    node = backends()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


# --- writing the user config -----------------------------------------------------------------

def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def dumps(data: dict, header: str = "") -> str:
    """A small TOML writer for nested tables of strings, numbers, bools and lists."""
    lines = [header.rstrip() + "\n"] if header else []

    def table(d: dict, path: list[str]):
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        if path and (scalars or not any(isinstance(v, dict) for v in d.values())):
            lines.append(f"[{'.'.join(path)}]")
        for k, v in scalars.items():
            lines.append(f"{k} = {_toml_value(v)}")
        if path and scalars:
            lines.append("")
        for k, v in d.items():
            if isinstance(v, dict):
                table(v, path + [k])
    table({k: v for k, v in data.items() if not isinstance(v, dict)} | {k: v for k, v in data.items() if isinstance(v, dict)}, [])
    return "\n".join(lines).rstrip() + "\n"


USER_HEADER = ("# Terraformer user config (ap setup writes it; edit by hand too). Environment variables\n"
               "# win over it; API keys go in secrets.toml beside it, never here. Defaults: the repo's\n"
               "# config/backends.toml.")


def save_user(updates: dict, remove: list[str] = ()) -> Path:
    """Merge `updates` into the user config (dotted `remove` keys are deleted) and write it."""
    cfg = _merge(user(), updates)
    for dotted in remove:
        *head, last = dotted.split(".")
        node = cfg
        for p in head:
            node = node.get(p) if isinstance(node, dict) else None
        if isinstance(node, dict):
            node.pop(last, None)
    path = user_toml()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps(cfg, USER_HEADER))
    tmp.replace(path)
    backends.cache_clear()
    return path


def secrets() -> dict:
    return {k: str(v) for k, v in (_read(secrets_toml()).get("keys") or {}).items() if v}


def save_secret(name: str, value: str) -> Path:
    """Set (or with an empty value remove) one API key in secrets.toml, kept at mode 0600."""
    if name not in SECRET_KEYS:
        raise ValueError(f"unknown key {name!r}: one of {', '.join(SECRET_KEYS)}")
    keys = secrets()
    if value:
        keys[name] = value
    else:
        keys.pop(name, None)
    path = secrets_toml()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(dumps({"keys": keys}, "# Terraformer API keys. Mode 0600; never commit or share this file."))
    os.replace(path.with_suffix(".tmp"), path)
    os.chmod(path, 0o600)
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
    return path


def load_secrets() -> list[str]:
    """Put secrets.toml's keys into the environment where the environment has none. Returns the
    names it set. Refuses (no keys loaded) a file other users can read."""
    path = secrets_toml()
    if not path.is_file():
        return []
    if path.stat().st_mode & 0o077:
        import sys
        print(f"terraformer: {path} is readable by other users: chmod 600 it; its keys were not loaded",
              file=sys.stderr)
        return []
    done = []
    for k, v in secrets().items():
        if k in SECRET_KEYS and not os.environ.get(k):
            os.environ[k] = v
            done.append(k)
    return done


# --- moving an older install over ------------------------------------------------------------

# keys that used to have this machine's values in the repo file; a migration keeps the values
# the old install ran with
MOVED_FROM_REPO = (("unreal", "editor_cmd"), ("unreal", "projects_dir"), ("unreal", "backup_dir"))


def migrate(root: Path, old_repo_values: dict | None = None) -> Path | None:
    """Write the user config for an install that ran from `root` (its AP_ROOT) with
    $AP_ROOT/backends.local.toml: the storage folder, everything in that local toml, and the
    machine values the repo file used to carry (`old_repo_values`, {section: {key: value}}).
    Does nothing when a user config exists already."""
    if user_toml().exists():
        return None
    cfg: dict = {"paths": {"ap_root": str(root)}}
    for sec, key in MOVED_FROM_REPO:
        v = ((old_repo_values or {}).get(sec) or {}).get(key)
        if v:
            cfg.setdefault(sec, {})[key] = v
    cfg = _merge(cfg, _read(root / "backends.local.toml"))
    return save_user(cfg)
