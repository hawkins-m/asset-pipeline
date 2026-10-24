"""`ap doctor`: everything this machine needs, what is missing, and how to fix each piece.

The same results feed the app's Status box and switch features off: each check names the
features it enables (FEATURES), and a feature whose check fails is disabled in the UI with the
check's sentence as the reason (no Unreal -> the Unreal tab explains; the rest works). ComfyUI
merely not running is not "missing" while Start ComfyUI can launch it.

Checks are read-only: they probe HTTP endpoints, list files, run `blender --version` and the GPU
tools; they never start a job, a server or a paid call.
"""
import os
import time
from dataclasses import asdict, dataclass, field

import httpx

from . import config, gpu, preflight, setup

# feature -> what it is in plain words (the UI says "<what> needs <check>")
FEATURES = {
    "comfy": "Image generation (style, sheets, frames, segmentation)",
    "blender": "Greybox, procedural builds, passes and renders",
    "unreal": "The Unreal tab (import, renders, look scenes)",
    "vlm": "Scene analysis and Brief & build (the vision model)",
    "segdepth": "Frame segmentation and depth (terrain from frames)",
    "trellis": "Make 3D with TRELLIS.2",
}


@dataclass
class Check:
    id: str
    name: str
    level: str                     # ok | warn | fail
    detail: str
    fix: str = ""
    features: list[str] = field(default_factory=list)   # disabled when this check fails
    optional: bool = False         # its failure turns its features off but isn't "missing"


def _c(id_, name, ok, detail, fix="", features=(), warn=False, optional=False) -> Check:
    return Check(id_, name, "ok" if ok and not warn else ("warn" if ok else "fail"), detail, "" if ok and not warn else fix,
                 list(features), optional)


def check_config() -> Check:
    p = config.user_toml()
    if p.exists():
        return _c("config", "Settings", True, f"{p}")
    return _c("config", "Settings", True, f"no user config yet ({p}); defaults in use",
              "Run `ap setup` (or Tools › Setup… in the app) to check and save your settings.", warn=True)


def check_storage() -> Check:
    root = config.ap_root()
    if not root.is_dir():
        return _c("storage", "Storage folder", False, f"{root} doesn't exist",
                  f"`ap setup` creates it, or mkdir -p {root}", ["comfy", "blender", "unreal", "vlm", "segdepth", "trellis"])
    if not os.access(root, os.W_OK):
        return _c("storage", "Storage folder", False, f"{root} isn't writable",
                  "Pick a folder you own in `ap setup`.", ["comfy", "blender", "unreal", "vlm", "segdepth", "trellis"])
    return _c("storage", "Storage folder", True, str(root))


def check_gpus() -> Check:
    gpus = gpu.list_gpus()
    if not gpus:
        return _c("gpus", "GPUs", False, "no GPU found (neither rocm-smi nor nvidia-smi answered)",
                  "Install the ROCm or NVIDIA driver tools; ComfyUI, TRELLIS and the vision model need a GPU.",
                  ["vlm", "trellis"])
    have = {g["index"] for g in gpus}
    roles = {"TRELLIS and vision": int(config.get("jobs.gpu0_gpu", 0)), "ComfyUI": int(config.get("jobs.comfy_gpu", 1))}
    bad = [f"{k} is set to GPU {v}" for k, v in roles.items() if v not in have]
    names = ", ".join(f"{g['index']} = {g['name']} ({g['vram_gb']:.0f} GB)" for g in gpus)
    if bad:
        return _c("gpus", "GPUs", False, f"{'; '.join(bad)}, which doesn't exist ({names})",
                  "Choose existing GPUs in `ap setup` (one GPU: the same index for both roles).", ["vlm", "trellis"])
    return _c("gpus", "GPUs", True, f"{names}; TRELLIS/vision on {roles['TRELLIS and vision']}, ComfyUI on {roles['ComfyUI']}")


def check_comfy() -> list[Check]:
    """ComfyUI through the configured backend (the proxy too for sdk), then its model files."""
    from .comfy.client import ComfyError, backend_name, connect
    c = config.backends()["comfyui"]
    out = []
    try:
        name = backend_name()
    except ValueError as e:
        return [_c("comfy", "ComfyUI", False, str(e), "Set the backend to sdk or httpx in `ap setup`.", ["comfy"])]
    if name == "sdk" and not setup.proxy_installed():
        out.append(_c("comfy_proxy", "Comfy API proxy (sdk backend)", False, "comfy-api-proxy isn't installed",
                      "scripts/comfy_api_proxy.sh install   (or switch the backend to httpx in `ap setup`)", ["comfy"]))
    up = setup._probe_comfy(c["url"], timeout=2.0)
    if not up:
        launch = setup._check_launch(str(c.get("service", {}).get("launch") or ""), {})
        if launch["ok"]:
            out.append(_c("comfy", f"ComfyUI ({name})", True, f"not running at {c['url']}",
                          "Start ComfyUI in the Status box (or `ap comfy start`); jobs that need it offer to start it.",
                          warn=True))
            out.append(check_comfy_models_offline())
        else:
            out.append(_c("comfy", f"ComfyUI ({name})", False, f"not running at {c['url']}, and Start ComfyUI can't "
                          f"launch it: {launch['message']}",
                          "Install ComfyUI (README step 2) and set its launch command in `ap setup`, or start it "
                          "yourself.", ["comfy"]))
        return out
    ver = (up.get("system") or {}).get("comfyui_version", "?")
    if out:                                           # no proxy: ComfyUI itself answers; the proxy is the fix above
        out.append(_c("comfy", f"ComfyUI ({name})", True, f"ComfyUI {ver} answers at {c['url']}"))
        out.append(check_comfy_models(c["url"]))
        return out
    try:
        connect(timeout_s=5).health()
        via = f" through the proxy at {c.get('sdk_url')}" if name == "sdk" else ""
        out.append(_c("comfy", f"ComfyUI ({name})", True, f"ComfyUI {ver} at {c['url']}{via}"))
    except (ComfyError, ValueError, httpx.HTTPError) as e:
        msg = str(e).split(". Start it")[0]
        if name == "sdk" and setup.proxy_installed():    # like ComfyUI not running: Start ComfyUI brings it up
            out.append(_c("comfy", f"ComfyUI ({name})", True, f"ComfyUI answers; {msg[0].lower() + msg[1:]}",
                          "Start ComfyUI in the Status box (or `ap comfy start`) starts the proxy too.", warn=True))
            out.append(check_comfy_models(c["url"]))      # read from ComfyUI itself: no proxy needed
        else:
            out.append(_c("comfy", f"ComfyUI ({name})", False, msg, "Check the address in `ap setup`.", ["comfy"]))
        return out
    out.append(check_comfy_models(c["url"]))
    return out


def _comfy_fix(files: list[str]) -> str:
    from . import models
    ids = [m.id for m in models.load() if m.root == "comfyui" and any(f.dest.split("/")[-1] in files for f in m.files)]
    cmd = models.download_command(ids) if ids else "add the model to config/models.toml"
    if not config.get("comfyui.models_dir"):
        cmd = "ap config set comfyui.models_dir <your ComfyUI>/models; " + cmd
    return cmd + "   (then restart ComfyUI so it lists them)"


def check_comfy_models_offline() -> Check:
    """ComfyUI isn't answering: the manifest's view of its models folder."""
    from . import models
    if not config.get("comfyui.models_dir"):
        return _c("comfy_models", "ComfyUI models", True, "not checked: ComfyUI isn't running and its models folder isn't set",
                  "ap setup (ComfyUI models folder)", warn=True)
    miss = models.missing("comfy")
    if miss:
        return _c("comfy_models", "ComfyUI models", False, f"missing {', '.join(m.id for m in miss)}",
                  models.download_command([m.id for m in miss]), ["comfy"])
    return _c("comfy_models", "ComfyUI models", True, f"all required files in {config.get('comfyui.models_dir')}")


def check_comfy_models(url: str) -> Check:
    missing, found = [], 0
    try:
        with httpx.Client(base_url=url, timeout=10) as http:
            for (node, fld), files in sorted(preflight.workflow_models().items()):
                r = http.get(f"/object_info/{node}")
                spec = r.json().get(node, {}).get("input", {}).get("required", {}).get(fld, [[]]) if r.status_code == 200 else [[]]
                options = spec[0] if isinstance(spec[0], list) else \
                    (spec[1].get("options", []) if len(spec) > 1 and isinstance(spec[1], dict) else [])
                for f in sorted(files):
                    if f in options:
                        found += 1
                    else:
                        missing.append(f)
    except httpx.HTTPError as e:
        return _c("comfy_models", "ComfyUI models", False, f"couldn't list them: {e}", "", ["comfy"])
    if missing:
        return _c("comfy_models", "ComfyUI models", False, f"{found} found, missing: {', '.join(missing)}",
                  _comfy_fix(missing), ["comfy"])
    return _c("comfy_models", "ComfyUI models", True, f"all {found} model files the workflows load")


def check_blender() -> Check:
    from . import blender
    r = setup._test_blender(str(blender.blender_bin()), {})
    return _c("blender", "Blender", r["ok"], r["message"].removeprefix("Works: "),
              "Install Blender 4.2+ (blender.org or snap install blender --classic) and set its path in `ap setup`.",
              ["blender"])


def check_unreal() -> Check:
    path = config.get("unreal.editor_cmd") or ""
    if not path:
        return _c("unreal", "Unreal Engine (optional)", False, "not set up",
                  "Set UnrealEditor-Cmd of a UE 5.8 install in `ap setup`; everything else works without it.", ["unreal"],
                  optional=True)
    r = setup._test_unreal(path, {})
    if r["ok"]:
        from pathlib import Path
        full = Path(path).expanduser().parent / "UnrealEditor"
        if not full.exists():
            return _c("unreal", "Unreal Engine (optional)", False, f"{full} not found (renders need the full editor)",
                      "Point `ap setup` at a full UE install (built or from Epic).", ["unreal"], optional=True)
    return _c("unreal", "Unreal Engine (optional)", r["ok"], r["message"].removeprefix("Found ").replace(" Not started by this test.", ""),
              "Fix the path in `ap setup` (…/Engine/Binaries/Linux/UnrealEditor-Cmd).", ["unreal"], optional=True)


def _model_check(id_, name, feature, required_only, env_paths, env_fix, optional=False) -> Check:
    """A feature's models (the manifest) and the venv that runs them."""
    from . import models
    miss = models.missing(feature, required_only=required_only)
    root = config.ap_root()
    no_env = [p for p in env_paths if not (root / p).exists()]
    if miss:
        size = sum(models.status(m)["missing_bytes"] for m in miss)
        return _c(id_, name, False, f"missing {', '.join(m.id for m in miss)} ({models.gb(size)})"
                  + (f"; and its environment ({', '.join(no_env)})" if no_env else ""),
                  models.download_command([m.id for m in miss]) + (f"   then: {env_fix}" if no_env else ""), [feature],
                  optional=optional)
    if no_env:
        return _c(id_, name, False, f"models installed; its environment is missing ({', '.join(no_env)})", env_fix,
                  [feature], optional=optional)
    ids = [m.id for m in models.load() if m.feature == feature and (models.required(m) if required_only else m.need != "experimental")]
    return _c(id_, name, True, ", ".join(ids) + " installed", optional=optional)


def check_local() -> list[Check]:
    local = config.backends().get("local", {})
    backend = (local.get("models", {}).get(str(local.get("model", "32b"))) or {}).get("backend")
    vlm_env = ["envs/llamacpp/llama-server"] if backend == "llamacpp" else ["envs/qwen-vl/bin/python"]
    vlm_fix = "scripts/install_llamacpp_vlm.sh llamacpp" if backend == "llamacpp" else "scripts/install_qwen_vl.sh venv"
    return [
        _model_check("vlm", f"Vision LLM (Qwen3-VL {local.get('model', '32b')})", "vlm", True, vlm_env, vlm_fix),
        _model_check("segdepth", "Segmentation and depth", "segdepth", True, ["envs/segdepth/bin/python"],
                     "scripts/install_segdepth.sh venv"),
        _model_check("trellis", "TRELLIS.2 (optional)", "trellis", False, ["envs/trellis2/bin/python", "src/TRELLIS.2"],
                     "scripts/install_trellis2.sh (README step 6)", optional=True),
    ]


def check_paid_keys() -> list[Check]:
    out = []
    for backend, key in (("gemini", "GEMINI_API_KEY"), ("claude", "ANTHROPIC_API_KEY")):
        if backend in setup.paid_needs():
            have = bool(os.environ.get(key) or config.secrets().get(key))
            out.append(_c(f"key_{backend}", f"{key} (a default backend is paid)", have,
                          "set" if have else "not set", "Add it in `ap setup` (secrets.toml), or switch the default to local."))
    return out


def run() -> list[Check]:
    checks = [check_config(), check_storage(), check_gpus(), *check_comfy(), check_blender(), check_unreal(),
              *check_local(), *check_paid_keys()]
    return checks


def features(checks: list[Check]) -> dict[str, dict]:
    """{feature: {ok, why, what}}: off when any check it depends on failed."""
    out = {f: {"ok": True, "why": "", "what": w} for f, w in FEATURES.items()}
    for c in checks:
        if c.level != "fail":
            continue
        for f in c.features:
            if out[f]["ok"]:
                out[f] = {"ok": False, "why": f"{c.name}: {c.detail}." + (f" Fix: {c.fix}" if c.fix else ""), "what": FEATURES[f]}
    return out


def report() -> dict:
    t0 = time.time()
    checks = run()
    return {"checks": [asdict(c) for c in checks], "features": features(checks),
            "missing": sum(c.level == "fail" and not c.optional for c in checks),
            "optional_missing": sum(c.level == "fail" and c.optional for c in checks),
            "notes": sum(c.level == "warn" for c in checks),
            "gpus": {"vision": int(config.get("jobs.gpu0_gpu", 0)), "comfy": int(config.get("jobs.comfy_gpu", 1))},
            "at": time.time(), "seconds": round(time.time() - t0, 1)}
