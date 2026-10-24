"""`ap preflight`: can this machine run the demo path? One line per check, then a verdict.

Checks ComfyUI (through the configured backend, and the Comfy API proxy for `sdk`), the
ComfyUI model files the demo's workflows load (read from ComfyUI's /object_info, so they're
checked where ComfyUI actually looks), the local models and envs ($AP_ROOT), Blender and
Unreal. Required checks fail the run; optional ones (TRELLIS, Unreal unless --ue) only warn.
"""
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import config
from .comfy import workflow

# Workflows on the demo path (frames, sheets, SAM) and the optional ones.
DEMO_WORKFLOWS = ("flux_control_union", "flux_t2i", "flux_t2i_anchor", "flux_img2img", "sam3_detect", "sam3_box")
LOADERS = {"ckpt_name": "CheckpointLoaderSimple", "style_model_name": "StyleModelLoader",
           "clip_name": "CLIPVisionLoader", "control_net_name": "ControlNetLoader",
           "lora_name": "LoraLoaderModelOnly"}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def workflow_models(names=DEMO_WORKFLOWS) -> dict[tuple[str, str], set[str]]:
    """(loader node, input) -> model files the given workflows load."""
    out: dict[tuple[str, str], set[str]] = {}
    for name in names:
        graph, _ = workflow.load_template(name)
        for node in graph.values():
            for field, value in node.get("inputs", {}).items():
                if field in LOADERS and isinstance(value, str) and value and node.get("class_type") == LOADERS[field]:
                    out.setdefault((node["class_type"], field), set()).add(value)
    return out


def comfy_checks() -> list[Check]:
    from .comfy.client import ComfyError, backend_name, connect
    c = config.backends()["comfyui"]
    name = backend_name()
    try:
        stats = connect(timeout_s=5).health()
    except (ComfyError, ValueError) as e:
        return [Check(f"ComfyUI ({name} backend)", False, str(e))]
    version = stats.get("system", {}).get("comfyui_version", "?")
    via = f" via proxy {c.get('sdk_url')}" if name == "sdk" else ""
    checks = [Check(f"ComfyUI ({name} backend)", True, f"{version} at {c['url']}{via}")]
    missing, found = [], 0
    with httpx.Client(base_url=c["url"], timeout=10) as http:
        for (node, field), files in sorted(workflow_models().items()):
            r = http.get(f"/object_info/{node}")
            spec = r.json().get(node, {}).get("input", {}).get("required", {}).get(field, [[]]) \
                if r.status_code == 200 else [[]]
            options = spec[0] if isinstance(spec[0], list) else \
                (spec[1].get("options", []) if len(spec) > 1 and isinstance(spec[1], dict) else [])  # COMBO form
            for f in sorted(files):
                if f in options:
                    found += 1
                else:
                    missing.append(f"{f} ({node})")
    checks.append(Check("ComfyUI models", not missing,
                        f"{found} found" + (f"; missing: {', '.join(missing)} (scripts/fetch_models.sh)"
                                            if missing else "")))
    return checks


def _exists(label: str, paths: list[Path], hint: str, required: bool = True) -> Check:
    missing = [p for p in paths if not p.exists()]
    return Check(label, not missing, ("ok" if not missing else
                                      "missing " + ", ".join(str(p) for p in missing) + f" ({hint})"), required)


def local_checks() -> list[Check]:
    root, local = config.ap_root(), config.backends().get("local", {})
    models = local.get("models", {})
    vlm = []
    for key in (local.get("model"), local.get("fallback")):
        m = models.get(key or "", {})
        if m.get("backend") == "llamacpp":
            files = [root / "models" / m["gguf"], root / "models" / m["mmproj"], root / "envs/llamacpp/llama-server"]
        elif m.get("backend") == "transformers":
            files = [root / "models/Qwen3-VL-8B-Instruct", root / "envs/qwen-vl/bin/python"]
        else:
            continue
        vlm.append((key, [p for p in files if not p.exists()]))
    usable = [k for k, miss in vlm if not miss]
    out = [Check("Vision LLM (Qwen3-VL)", bool(usable),
                 f"usable: {', '.join(usable)}" if usable else
                 "none installed (scripts/install_llamacpp_vlm.sh llamacpp 8b, or scripts/install_qwen_vl.sh)")]
    out.append(_exists("Segmentation/depth (OneFormer, Depth Anything V2 Small)",
                       [root / "envs/segdepth/bin/python", root / "models/oneformer_ade20k_swin_large",
                        root / "models/Depth-Anything-V2-Small-hf"], "scripts/install_segdepth.sh"))
    out.append(_exists("TRELLIS.2 (irregular assets; not on the demo path)",
                       [root / "envs/trellis2/bin/python", root / "models/TRELLIS.2-4B"],
                       "scripts/install_trellis2.sh", required=False))
    return out


def blender_check() -> Check:
    from . import blender
    exe = str(blender.blender_bin())
    path = shutil.which(exe)
    if not path:
        return Check("Blender", False, f"{exe} not found (set [tools] blender or AP_BLENDER)")
    try:
        r = subprocess.run([path, "-b", "--factory-startup", "--version"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=120)
        ver = next((ln.strip() for ln in r.stdout.splitlines() if ln.startswith("Blender ")), "version unknown")
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check("Blender", False, f"{path}: {e}")
    return Check("Blender", True, f"{ver} ({path})")


def ue_check(required: bool) -> Check:
    from . import ue
    try:
        exe = ue.editor_cmd()
    except FileNotFoundError as e:
        return Check("Unreal Engine 5.8", False, str(e), required)
    full = exe.parent / "UnrealEditor"
    return Check("Unreal Engine 5.8", full.exists(),
                 str(exe.parent) if full.exists() else f"{full} not found (renders need the full editor)", required)


def run(ue: bool = False) -> list[Check]:
    return comfy_checks() + local_checks() + [blender_check(), ue_check(required=ue)]
