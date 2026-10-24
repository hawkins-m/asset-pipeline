"""Nothing machine-specific or model-specific is hard-coded: every model the code loads is in the
manifest (config/models.toml), every setting it reads is in the config (config/backends.toml
defaults, config.ENV_OVERRIDES / ENV_VARS / SECRET_KEYS). Setup, Doctor and the installer only
know what is registered there, so these fail when something bypasses them (CLAUDE.md)."""
import json
import re
import tomllib
from pathlib import Path

from asset_pipeline import config, models

REPO = Path(__file__).resolve().parents[1]
CODE = [p for p in [*(REPO / "asset_pipeline").rglob("*.py"), *(REPO / "scripts").rglob("*.py"),
                    *(REPO / "scripts").glob("*.sh"), *(REPO / "unreal").rglob("*.py"),
                    REPO / "scripts" / "ap", REPO / "scripts" / "trellis", REPO / "scripts" / "git-hooks" / "pre-commit"]
        if "__pycache__" not in p.parts]
MANIFEST = models.load()
REPOS = {m.repo for m in MANIFEST} | {s.repo for m in MANIFEST for s in m.mirrors}


def _text(p: Path) -> str:
    return p.read_text(errors="replace")


def test_every_comfyui_model_a_workflow_loads_is_in_the_manifest():
    listed = {f.dest.split("/")[-1] for m in MANIFEST if m.root == "comfyui" for f in m.files}
    loaded = {}
    for wf in (REPO / "workflows").glob("*.json"):
        if wf.name.endswith(".bindings.json"):
            continue
        for node in json.loads(wf.read_text()).values():
            for v in (node.get("inputs") or {}).values():
                if isinstance(v, str) and re.search(r"\.(safetensors|ckpt|pt|pth|gguf|bin)$", v):
                    loaded.setdefault(v, wf.name)
    missing = {f: wf for f, wf in loaded.items() if f not in listed}
    assert not missing, f"workflow models not in config/models.toml: {missing}"


def test_local_model_paths_are_manifest_destinations():
    dests = [f.dest for m in MANIFEST if m.root == "models" for f in m.files]
    ok = lambda p: any(d == p or d.startswith(p.rstrip("/") + "/") for d in dests)   # noqa: E731
    bad = []
    for p in CODE:
        t = _text(p)
        refs = re.findall(r'"models"\s*/\s*"([^"]+)"', t) + [   # ComfyUI's own models/ folders: checked via the workflows
            m.group(1) for m in re.finditer(r'models/([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)', t)
            if "ComfyUI" not in t[max(0, m.start() - 12):m.start()]]
        bad += [f"{p.relative_to(REPO)}: models/{r}" for r in refs if not ok(r)]
    local = config._read(config.BACKENDS_TOML).get("local", {}).get("models", {})
    for name, m in local.items():
        bad += [f"backends.toml [local.models.{name}] {k} = {m[k]}" for k in ("gguf", "mmproj") if k in m and not ok(m[k])]
    assert not bad, "model paths not in config/models.toml:\n" + "\n".join(bad)


HUB_ID = r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*"


def test_hub_repos_the_code_loads_are_in_the_manifest():
    bad = []
    for p in CODE:
        for ln in _text(p).splitlines():
            if p.suffix == ".py" or p.name in ("ap", "trellis"):
                if re.search(r"from_pretrained|snapshot_download|hf_hub_download|_model\"|_REPO", ln):
                    bad += [f"{p.relative_to(REPO)}: {r}" for r in re.findall(rf'"({HUB_ID})"', ln)
                            if r not in REPOS and not r.startswith(("http", "models/"))]
            for r in re.findall(rf"\bhf download ({HUB_ID})", ln):
                if r not in REPOS:
                    bad.append(f"{p.relative_to(REPO)}: {r}")
    assert not bad, "hub repos not in config/models.toml:\n" + "\n".join(bad)


SETTING_ENV = re.compile(r"^(AP_|COMFY|HF_|TRELLIS_|PROXY_)|_API_KEY$|_TOKEN$")


def test_every_setting_env_var_read_is_registered():
    known = set(config.ENV_VARS) | set(config.ENV_OVERRIDES) | set(config.SECRET_KEYS)
    bad = []
    for p in CODE:
        t = _text(p)
        if p.suffix == ".py":
            names = re.findall(r'(?:os\.environ(?:\.get)?\s*[\[(]\s*|getenv\(\s*|_setting\(\s*)"([A-Z][A-Z0-9_]+)"', t)
        else:                                   # a variable the script assigns itself is not a setting
            own = set(re.findall(r"(?:^|[\s;(])(?:export |local )?([A-Z][A-Z0-9_]+)=", t, re.M))
            names = [n for n in re.findall(r"\$\{?([A-Z][A-Z0-9_]+)", t) if n not in own or f"${{{n}:-" in t]
        bad += [f"{p.relative_to(REPO)}: {n}" for n in set(names) if SETTING_ENV.search(n) and n not in known]
    assert not bad, "environment variables not registered in config.ENV_VARS / ENV_OVERRIDES / SECRET_KEYS:\n" + "\n".join(sorted(bad))


def test_config_keys_the_code_reads_have_defaults():
    defaults = tomllib.loads(config.BACKENDS_TOML.read_text())

    def has(dotted):
        node = defaults
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True
    bad = []
    for p in CODE:
        if p.suffix != ".py":
            continue
        for k in re.findall(r'config\.get\(\s*"([a-z0-9_.]+)"', _text(p)):
            if k != "paths.ap_root" and not has(k):
                bad.append(f"{p.relative_to(REPO)}: {k}")
    for m in MANIFEST:
        if m.repo_setting and not has(m.repo_setting):
            bad.append(f"config/models.toml {m.id}: {m.repo_setting}")
    assert not bad, "config keys without a default in config/backends.toml:\n" + "\n".join(bad)


def test_no_machine_paths_in_code_or_config():
    machine = re.compile(r"/mnt/(?!\*)|/media/(?!\*)|/home/[a-z]|~/Projects/|/Users/[A-Za-z]")
    bad = []
    for p in [*CODE, config.BACKENDS_TOML, models.MANIFEST]:
        for i, ln in enumerate(_text(p).splitlines(), 1):
            if machine.search(ln):
                bad.append(f"{p.relative_to(REPO)}:{i}: {ln.strip()[:100]}")
    assert not bad, "machine-specific paths (put them in the user config):\n" + "\n".join(bad)
