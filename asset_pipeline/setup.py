"""First-run setup: what `ap setup` and the app's Setup screen ask for, what they detect, and a
plain-words Test per item. Values are written to the user config (config.save_user) and API
keys to secrets.toml (config.save_secret); nothing here writes into the repo.

Items (ITEMS): storage folder, ComfyUI backend and address, the Comfy API proxy (sdk backend),
the ComfyUI launch command, API keys the enabled backends need, Blender, Unreal (optional),
GPU assignment. A Test never starts a GPU job and never calls a paid service: an API key test
checks only that a key is there.
"""
import glob
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from . import config, gpu


@dataclass
class Item:
    id: str
    label: str
    key: str                       # "section.key" in the config, or "secret:NAME"
    help: str
    kind: str = "text"             # text | path | choice | secret | gpu
    choices: list[str] = field(default_factory=list)
    optional: bool = False
    when: str = ""                 # shown only when this holds: "backend=sdk", "backend=httpx", "paid"


ITEMS = [
    Item("storage", "Storage folder", "paths.ap_root",
         "Where projects, models, model environments and outputs go. Needs room: tens of GB for models.", "path"),
    Item("comfy_backend", "ComfyUI backend", "comfyui.backend",
         "sdk: the Comfy SDK through the local comfy-api-proxy (the default). httpx: ComfyUI's own HTTP API, no proxy.",
         "choice", ["sdk", "httpx"]),
    Item("comfy_url", "ComfyUI address", "comfyui.url",
         "The ComfyUI server the pipeline uses, e.g. http://127.0.0.1:8188. Install ComfyUI yourself (README step 2)."),
    Item("sdk_url", "Comfy API proxy address", "comfyui.sdk_url",
         "The local comfy-api-proxy the sdk backend talks to (install once: scripts/comfy_api_proxy.sh install). "
         "`ap comfy start` starts it with ComfyUI.", when="backend=sdk"),
    Item("comfy_launch", "ComfyUI launch command", "comfyui.service.launch",
         "How Start ComfyUI launches your server; {port} is filled in. Leave as is if you start ComfyUI yourself.",
         optional=True),
    Item("comfy_cwd", "ComfyUI folder", "comfyui.service.cwd",
         "The folder the launch command runs in (your ComfyUI checkout).", "path", optional=True),
    Item("comfy_api_key", "Comfy account API key", "secret:COMFY_API_KEY",
         "Only for Comfy Cloud, a paid service (not the default, not needed for a local ComfyUI).", "secret",
         optional=True, when="backend=sdk"),
    Item("gemini_key", "Gemini API key", "secret:GEMINI_API_KEY",
         "Needed only because a default backend is Gemini (paid; runs need AP_ALLOW_PAID_APIS=1).", "secret",
         when="paid=gemini"),
    Item("anthropic_key", "Anthropic API key", "secret:ANTHROPIC_API_KEY",
         "Needed only because the default vision LLM is Claude (paid; runs need AP_ALLOW_PAID_APIS=1).", "secret",
         when="paid=claude"),
    Item("blender", "Blender", "tools.blender",
         "The Blender executable (greybox, procedural buildings, renders). Blender 4.2 or newer.", "path"),
    Item("unreal", "Unreal Engine", "unreal.editor_cmd",
         "UnrealEditor-Cmd of a UE 5.8 install (<UnrealEngine>/Engine/Binaries/Linux/UnrealEditor-Cmd). "
         "Without it the Unreal tab explains what is missing and everything else works.", "path", optional=True),
    Item("unreal_projects", "Unreal projects folder", "unreal.projects_dir",
         "Where the pipeline keeps its UE projects (one per project).", "path", optional=True),
    Item("gpu_vision", "GPU for TRELLIS and the vision model", "jobs.gpu0_gpu",
         "GPU index for TRELLIS.2 and the local vision LLM.", "gpu"),
    Item("gpu_comfy", "GPU for ComfyUI", "jobs.comfy_gpu",
         "GPU index ComfyUI runs on (Start ComfyUI pins it there). With one GPU use the same index for both: "
         "jobs then wait for each other.", "gpu"),
]
BY_ID = {i.id: i for i in ITEMS}


def first_run() -> bool:
    """No user config yet: the app opens Setup by itself."""
    return not config.user_toml().exists()


def paid_needs() -> set[str]:
    """Paid backends the defaults select ("gemini", "claude"): their keys are asked for."""
    d = config.backends().get("defaults", {})
    out = {d.get("llm", "local")} & {"gemini", "claude"}
    out |= {"gemini" for v in (d.get("backends") or {}).values() if v == "gemini"}
    return out


def _shown(item: Item, values: dict) -> bool:
    if not item.when:
        return True
    k, _, v = item.when.partition("=")
    if k == "backend":
        return (values.get("comfy_backend") or "sdk") == v
    if k == "paid":
        return v in paid_needs()
    return True


def current(item: Item) -> str:
    if item.key.startswith("secret:"):
        return ""                                   # never sent back; `has_value` says whether one is set
    v = config.get(item.key, "")
    return "" if v is None else str(v)


def _has_secret(item: Item) -> bool:
    name = item.key.removeprefix("secret:")
    return bool(os.environ.get(name) or config.secrets().get(name))


# --- detection --------------------------------------------------------------------------------

def _probe_comfy(url: str, timeout: float = 1.0) -> dict | None:
    from .comfy.client import ComfyClient, ComfyError
    import httpx
    c = ComfyClient(url, timeout_s=timeout)
    c.http.timeout = httpx.Timeout(timeout)
    try:
        return c.health()
    except (ComfyError, httpx.HTTPError, ValueError):
        return None
    finally:
        c.http.close()


def _blender_candidates() -> list[str]:
    out = [config.get("tools.blender") or "", shutil.which("blender") or "", "/snap/bin/blender"]
    for pat in ("~/blender*/blender", "~/Applications/blender*/blender", "/opt/blender*/blender", "/usr/local/blender*/blender"):
        out += sorted(glob.glob(os.path.expanduser(pat)), reverse=True)
    found = [shutil.which(os.path.expanduser(p)) for p in out if p]
    return list(dict.fromkeys(f for f in found if f))


UE_TAIL = "Engine/Binaries/Linux/UnrealEditor-Cmd"
UE_GLOBS = ("~/UnrealEngine*/", "~/*/UnrealEngine*/", "~/*/UnrealEngine/*/", "~/UnrealEngine/*/", "/opt/UnrealEngine*/",
            "/opt/unreal*/", "/opt/UnrealEngine/*/", "/mnt/*/UnrealEngine/*/", "/mnt/*/UnrealEngine*/",
            "/media/*/*/UnrealEngine*/", "~/Epic Games/UE_*/")


def _unreal_candidates() -> list[str]:
    out = [config.get("unreal.editor_cmd") or ""]
    for pat in UE_GLOBS:
        out += sorted(glob.glob(os.path.expanduser(pat) + UE_TAIL), reverse=True)
    return [p for p in dict.fromkeys(out) if p and Path(p).is_file()]


def detect() -> dict[str, str]:
    """Best guesses for each item (the current value when it already works)."""
    d: dict[str, str] = {"storage": str(config.ap_root())}
    cur_url = config.get("comfyui.url") or "http://127.0.0.1:8188"
    for url in dict.fromkeys([cur_url, "http://127.0.0.1:8188", "http://127.0.0.1:8000"]):
        if _probe_comfy(url):
            d["comfy_url"] = url
            break
    else:
        d["comfy_url"] = cur_url
    d["comfy_backend"] = config.get("comfyui.backend") or "sdk"
    d["sdk_url"] = config.get("comfyui.sdk_url") or "http://127.0.0.1:8190"
    if Path("~/ComfyUI/main.py").expanduser().is_file() and not config.user().get("comfyui", {}).get("service", {}).get("cwd"):
        d["comfy_cwd"] = "~/ComfyUI"
    if (b := _blender_candidates()):
        d["blender"] = b[0]
    if (u := _unreal_candidates()):
        d["unreal"] = u[0]
    d["unreal_projects"] = str(_ue_projects_default())
    gpus = gpu.list_gpus()
    d["gpu_vision"] = str(config.get("jobs.gpu0_gpu", 0))
    comfy = str(config.get("jobs.comfy_gpu", 1))
    if gpus and int(comfy) >= len(gpus):
        comfy = str(len(gpus) - 1)                     # one GPU: both on it
    d["gpu_comfy"] = comfy
    return d


def _ue_projects_default() -> Path:
    from . import ue
    return ue.projects_dir()


def _repo_default(key: str):
    node = config._read(config.BACKENDS_TOML)
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    return node


def state() -> dict:
    """What the Setup screen shows: each item with its current value, the detected value, and
    whether it applies (backend, paid defaults); the GPUs found; first run or not."""
    det = detect()
    values = {i.id: current(i) for i in ITEMS}
    for i in ITEMS:                                   # still the repo default and something better found: offer that
        if det.get(i.id) not in (None, "", values[i.id]) and values[i.id] == str(_repo_default(i.key) or "") \
                and not i.key.startswith("secret:"):
            values[i.id] = ""
    items = []
    for i in ITEMS:
        items.append(asdict(i) | {"value": values[i.id], "detected": det.get(i.id, ""),
                                   "has_value": _has_secret(i) if i.kind == "secret" else bool(values[i.id]),
                                   "shown": _shown(i, values | {k: v for k, v in det.items() if not values.get(k)})})
    return {"first_run": first_run(), "config": str(config.user_toml()), "secrets": str(config.secrets_toml()),
            "gpus": gpu.list_gpus(), "items": items}


# --- tests: one plain sentence each ------------------------------------------------------------

def _res(ok: bool, message: str, warn: bool = False) -> dict:
    return {"ok": ok, "warn": warn, "message": message}


def _test_storage(v: str, _all: dict) -> dict:
    if not v:
        return _res(False, "Choose a folder.")
    p = Path(v).expanduser()
    if not p.is_absolute():
        return _res(False, "Use a full path, e.g. /home/you/terraformer-data.")
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        return _res(False, f"You can't write to {probe}. Pick a folder you own.")
    free = shutil.disk_usage(probe).free / 1e9
    what = "exists" if p.exists() else "will be created"
    projects = len(list((p / "projects").glob("*/project.json"))) if (p / "projects").is_dir() else 0
    msg = f"Works: {p} {what}, {free:.0f} GB free" + (f", {projects} project(s) in it." if projects else ".")
    if free < 50:
        return _res(True, msg + " Models need about 50 GB or more.", warn=True)
    return _res(True, msg)


def _test_comfy_url(v: str, _all: dict) -> dict:
    u = urlparse(v or "")
    if u.scheme not in ("http", "https") or not u.hostname:
        return _res(False, "Enter an address like http://127.0.0.1:8188.")
    stats = _probe_comfy(v, timeout=3.0)
    if not stats:
        return _res(False, f"Nothing answers at {v}. Start ComfyUI (ap comfy start, or Start ComfyUI in the Status box) "
                           "and test again; check the port.")
    ver = (stats.get("system") or {}).get("comfyui_version") or "?"
    devs = [d.get("name", "") for d in stats.get("devices") or []]
    dev = devs[0].split(" : ")[0].split(" ", 1)[-1].strip() if devs else ""     # "cuda:0 <name> : native"
    return _res(True, f"Works: ComfyUI {ver} answers at {v}" + (f" on {dev}." if dev else "."))


def proxy_installed() -> Path | None:
    exe = config.ap_root() / "envs" / "comfy-api-proxy" / "bin" / "comfy-api-proxy"
    return exe if exe.is_file() else None


def _test_sdk(v: str, all_: dict) -> dict:
    """The whole sdk path: proxy installed, answering, and the Comfy SDK client reaching ComfyUI
    through it (comfy.client.connect's SdkComfyClient.health)."""
    import httpx
    u = urlparse(v or "")
    if u.scheme not in ("http", "https") or not u.port:
        return _res(False, "Enter an address like http://127.0.0.1:8190.")
    if not proxy_installed():
        return _res(False, "The Comfy API proxy isn't installed. Run: scripts/comfy_api_proxy.sh install "
                           "(then ap comfy start starts it with ComfyUI).")
    comfy_url = all_.get("comfy_url") or config.get("comfyui.url")
    try:
        r = httpx.get(f"{v.rstrip('/')}/api/v2/health", timeout=3)
        r.raise_for_status()
    except httpx.HTTPError:
        return _res(True, f"The proxy is installed but not running at {v}: `ap comfy start` (or Start ComfyUI in "
                          "the Status box) starts it; test again then.", warn=True)
    try:
        from .comfy.sdk_client import SdkComfyClient
        c = SdkComfyClient(comfy_url, v, timeout_s=5)
        c.http.timeout = httpx.Timeout(5)
        c.health()
    except Exception as e:                            # noqa: BLE001 - shown to the user in plain words
        return _res(False, f"The proxy answers, but the Comfy SDK couldn't reach ComfyUI through it: {e}")
    return _res(True, f"Works: the Comfy SDK reaches ComfyUI at {comfy_url} through the proxy at {v}.")


def _comfy_answers(all_: dict) -> bool:
    return bool(_probe_comfy(all_.get("comfy_url") or config.get("comfyui.url") or "", timeout=1.0))


def _only_for_start(res: dict, all_: dict) -> dict:
    """A launch setting that fails matters only for Start ComfyUI when ComfyUI already answers."""
    if res["ok"] or not _comfy_answers(all_):
        return res
    return _res(True, res["message"] + " ComfyUI already answers, so this only matters for Start ComfyUI.", warn=True)


def _test_launch(v: str, all_: dict) -> dict:
    return _only_for_start(_check_launch(v, all_), all_)


def _check_launch(v: str, all_: dict) -> dict:
    import shlex
    if not v:
        return _res(True, "Not set: start ComfyUI yourself before running jobs.", warn=True)
    try:
        argv = [os.path.expanduser(t) for t in shlex.split(v.format(port=8188, url=""))]
    except (ValueError, KeyError, IndexError) as e:
        return _res(False, f"Can't read the command: {e}.")
    cwd = Path(os.path.expanduser(all_.get("comfy_cwd") or config.get("comfyui.service.cwd") or ".")).expanduser()
    exe = argv[0]
    found = shutil.which(exe) or (Path(exe).is_file() and exe) or (cwd / exe).is_file()
    if not found:
        return _res(False, f"{exe} wasn't found. Use the full path to your ComfyUI's python or launch script.")
    script = next((a for a in argv[1:] if a.endswith(".py")), None)       # python main.py: main.py must be there
    if script and not (Path(script).is_file() or (cwd / script).is_file()):
        return _res(False, f"{script} isn't in {cwd}. Set the ComfyUI folder to your ComfyUI checkout, or give the "
                           "script's full path.")
    if "{port}" not in v:
        return _res(True, f"{exe} found, but the command has no {{port}}: ComfyUI picks its own port.", warn=True)
    return _res(True, f"Looks right: {exe} found. Start ComfyUI runs it (nothing started by this test).")


def _test_cwd(v: str, all_: dict) -> dict:
    return _only_for_start(_check_cwd(v, all_), all_)


def _check_cwd(v: str, _all: dict) -> dict:
    if not v:
        return _res(True, "Not set: the launch command runs from the app's folder (fine for a launch script "
                          "given by its full path).")
    p = Path(v).expanduser()
    if not p.is_dir():
        return _res(False, f"{p} isn't a folder.")
    return _res(True, f"Works: {p}" + (" holds a ComfyUI checkout." if (p / "main.py").is_file() else " exists (no main.py in it)."),
                warn=not (p / "main.py").is_file())


def _test_secret(name: str, paid: bool, optional: bool = False):
    def t(v: str, _all: dict) -> dict:
        have = v or os.environ.get(name) or config.secrets().get(name)
        if not have and optional:
            return _res(True, "Not set: not needed for a local ComfyUI.", warn=True)
        if not have:
            return _res(False, f"No {name} set.")
        if len(have.strip()) < 16:
            return _res(False, f"{name} looks too short to be a key.")
        return _res(True, f"{name} is set. Not checked online: that would call a paid service; "
                          "the first real run tells.", warn=True)
    return t


def _test_blender(v: str, _all: dict) -> dict:
    exe = shutil.which(os.path.expanduser(v or "blender"))
    if not exe:
        return _res(False, f"{v or 'blender'} wasn't found. Install Blender (blender.org, or snap install blender "
                           "--classic) and give its full path.")
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return _res(False, f"{exe} didn't run: {e}.")
    line = next((ln.strip() for ln in r.stdout.splitlines() if ln.startswith("Blender ")), "")
    if r.returncode != 0 or not line:
        return _res(False, f"{exe} ran but didn't say it is Blender ({(r.stderr or r.stdout).strip()[:150]}).")
    try:
        major, minor = (int(x) for x in line.split()[1].split(".")[:2])
        if (major, minor) < (4, 2):
            return _res(False, f"{line} is too old: Blender 4.2 or newer.")
    except ValueError:
        pass
    return _res(True, f"Works: {line} ({exe}).")


def ue_version(editor_cmd: Path) -> str:
    """The engine version from Engine/Build/Build.version next to UnrealEditor-Cmd ("" if unknown)."""
    for parent in editor_cmd.parents:
        f = parent / "Build" / "Build.version"
        if parent.name == "Engine" and f.is_file():
            try:
                b = json.loads(f.read_text())
                return f"{b['MajorVersion']}.{b['MinorVersion']}.{b['PatchVersion']}"
            except (OSError, ValueError, KeyError):
                return ""
    return ""


def _test_unreal(v: str, _all: dict) -> dict:
    if not v:
        return _res(True, "Not set: the Unreal tab stays off; everything else works.", warn=True)
    p = Path(v).expanduser()
    if p.is_dir() and (p / UE_TAIL).is_file():
        p = p / UE_TAIL
    if not p.is_file():
        return _res(False, f"{p} doesn't exist. Point it at {UE_TAIL} inside your Unreal Engine folder.")
    if not os.access(p, os.X_OK):
        return _res(False, f"{p} isn't executable (chmod +x, or a different file).")
    ver = ue_version(p)
    if ver and not ver.startswith("5."):
        return _res(False, f"Unreal Engine {ver} found; the pipeline needs UE 5 (5.8 tested).")
    return _res(True, f"Found Unreal Engine {ver or '(version unknown)'} at {p}. Not started by this test.",
                warn=bool(ver) and not ver.startswith("5.8"))


def _test_ue_projects(v: str, _all: dict) -> dict:
    p = Path(v or str(_ue_projects_default())).expanduser()
    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        return _res(False, f"You can't write to {probe}.")
    return _res(True, f"Works: {p} {'exists' if p.exists() else 'will be created'}.")


def _test_gpu(v: str, all_: dict) -> dict:
    gpus = gpu.list_gpus()
    try:
        i = int(v)
    except (TypeError, ValueError):
        return _res(False, "Enter a GPU index (0, 1, …).")
    if not gpus:
        return _res(False, "No GPU found (no rocm-smi or nvidia-smi answered). ComfyUI, TRELLIS and the vision model need one.")
    hit = next((g for g in gpus if g["index"] == i), None)
    if not hit:
        return _res(False, f"There is no GPU {i}: this machine has {', '.join(str(g['index']) for g in gpus)}.")
    other = all_.get("gpu_comfy" if all_.get("_id") == "gpu_vision" else "gpu_vision")
    msg = f"Works: GPU {i} is {hit['name']} ({hit['vram_gb']:.0f} GB)."
    if other is not None and str(other) == str(i) and len(gpus) > 1:
        return _res(True, msg + " Both roles share it: jobs will wait for each other.", warn=True)
    return _res(True, msg)


def _test_choice(v: str, _all: dict) -> dict:
    if v not in ("sdk", "httpx"):
        return _res(False, "Choose sdk or httpx.")
    return _res(True, "sdk: Comfy SDK through the local proxy (test the proxy below)." if v == "sdk"
                else "httpx: ComfyUI's own HTTP API, no proxy needed.")


TESTS = {
    "storage": _test_storage, "comfy_backend": _test_choice, "comfy_url": _test_comfy_url, "sdk_url": _test_sdk,
    "comfy_launch": _test_launch, "comfy_cwd": _test_cwd,
    "comfy_api_key": _test_secret("COMFY_API_KEY", True, optional=True), "gemini_key": _test_secret("GEMINI_API_KEY", True),
    "anthropic_key": _test_secret("ANTHROPIC_API_KEY", True), "blender": _test_blender, "unreal": _test_unreal,
    "unreal_projects": _test_ue_projects, "gpu_vision": _test_gpu, "gpu_comfy": _test_gpu,
}


def test(item_id: str, value: str | None = None, values: dict | None = None) -> dict:
    """Test one item with `value` (default: its current value); `values` are the other items'
    values on screen (the sdk test uses the ComfyUI address, the GPU tests each other)."""
    item = BY_ID.get(item_id)
    if not item:
        raise ValueError(f"no setup item {item_id!r}")
    values = dict(values or {})
    v = current(item) if value is None else str(value).strip()
    values["_id"] = item_id
    return TESTS[item_id](v, values) | {"id": item_id}


# --- saving -------------------------------------------------------------------------------------

def _typed(item: Item, v: str):
    if item.kind == "gpu":
        return int(v)
    if item.kind == "path" and v:
        return v if v.startswith("~") else str(Path(v).expanduser())
    return v


def save(values: dict, secrets: dict | None = None) -> dict:
    """Write the given items into the user config (a "" for an optional item removes it, so its
    default applies) and the given API keys into secrets.toml. Creates the storage folder."""
    upd: dict = {}
    remove = []
    for item_id, v in values.items():
        item = BY_ID.get(item_id)
        if not item or item.key.startswith("secret:"):
            continue
        v = "" if v is None else str(v).strip()
        sec, _, key = item.key.rpartition(".")
        if v == "" and item.optional:
            remove.append(item.key)
            continue
        if v == "":
            continue
        node = upd
        for part in sec.split("."):
            node = node.setdefault(part, {})
        node[key] = _typed(item, v)
    if str(values.get("gpu_comfy", "")).strip() != "":
        # Start ComfyUI pins the ComfyUI GPU: keep [comfyui.service] gpu in step with the role
        upd.setdefault("comfyui", {}).setdefault("service", {})["gpu"] = str(int(values["gpu_comfy"]))
    path = config.save_user(upd, remove)
    for name, v in (secrets or {}).items():
        if v is not None and (v := str(v).strip()) != "":
            config.save_secret(name, v)
    root = config.ap_root()
    for sub in ("projects", "logs", "envs", "models", "outputs", "inputs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return {"config": str(path), "storage": str(root)}
