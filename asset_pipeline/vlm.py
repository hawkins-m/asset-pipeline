"""Local vision-LLM server lifecycle: one model on one GPU, never alongside TRELLIS.

Models are configured in config/backends.toml [local]: the default (32B, llama.cpp) and
a fallback (8B, transformers) used when the default can't start. Both unload their
weights after `idle_unload_s` without requests and reload on the next one.

State lives on disk so the CLI, the UI and `trellis` agree:
    $AP_ROOT/logs/vlm.json                  {"model", "backend", "url", "pid", "gpu"}
    $AP_ROOT/logs/trellis-gpu<N>-<pid>.lock  a TRELLIS job holds GPU N (scripts/trellis)

`trellis` writes its lock and then calls `ap vlm down --gpu N`, which waits for a running
request to finish before stopping the server. up() refuses a GPU that a TRELLIS job holds.
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

from . import config
from .llm.base import LLMError


class VLMError(LLMError):
    pass


def _logs() -> Path:
    d = config.ap_root() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cfg() -> dict:
    return config.backends().get("local", {})


def _alive(pid: int) -> bool:
    """Running, and not a zombie: a server the UI process started and down() stopped stays
    a zombie until reaped, and kill(pid, 0) still succeeds on it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    if stat.rsplit(")", 1)[1].split()[0] == "Z":
        try:
            os.waitpid(pid, os.WNOHANG)  # reap it if it's our child
        except ChildProcessError:
            pass
        return False
    return True


def state() -> dict | None:
    """The running server, or None (a stale state file is removed)."""
    f = _logs() / "vlm.json"
    try:
        st = json.loads(f.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if not _alive(st["pid"]):
        f.unlink(missing_ok=True)
        return None
    return st


def trellis_jobs(gpu: int) -> list[int]:
    """PIDs of live TRELLIS jobs holding `gpu` (stale lock files are removed)."""
    pids = []
    for f in _logs().glob(f"trellis-gpu{gpu}-*.lock"):
        pid = int(f.stem.rsplit("-", 1)[1])
        if _alive(pid):
            pids.append(pid)
        else:
            f.unlink(missing_ok=True)
    return pids


def client(st: dict):
    """Adapter for a running server; its name records which model drafted a plan."""
    if st["backend"] == "llamacpp":
        from .llm.llamacpp import LlamaCppVision
        c = LlamaCppVision(st["url"])
    else:
        from .llm.local import LocalVision
        c = LocalVision(st["url"])
    c.name = f"local-{st['model']}"
    return c


def busy(st: dict) -> bool:
    try:
        if st["backend"] == "llamacpp":
            return any(s.get("is_processing") for s in httpx.get(st["url"] + "/slots", timeout=5).json())
        return bool(httpx.get(st["url"] + "/health", timeout=5).json().get("busy"))
    except (httpx.HTTPError, ValueError):
        return False


def _healthy(url: str) -> bool:
    try:
        return httpx.get(url + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def _command(model: str, m: dict, gpu: int, idle: float) -> list[str]:
    root = config.ap_root()
    port = m["url"].rsplit(":", 1)[1]
    if m["backend"] == "llamacpp":
        files = [root / "models" / m["gguf"], root / "models" / m["mmproj"],
                 root / "envs" / "llamacpp" / "llama-server"]
        missing = [str(f) for f in files if not f.exists()]
        if missing:
            raise VLMError(f"{model}: missing {', '.join(missing)}; run scripts/install_llamacpp_vlm.sh")
        # Vulkan device N is HIP GPU N on this machine (checked by VRAM use); 1024 image
        # tokens: llama.cpp's minimum for Qwen-VL grounding, and what the 8B server uses.
        return [str(files[2]), "-m", str(files[0]), "--mmproj", str(files[1]),
                "--host", "127.0.0.1", "--port", port, "--device", f"Vulkan{gpu}",
                "-ngl", "999", "-c", str(m.get("ctx", 8192)), "-np", "1",
                "--image-min-tokens", "1024", "--image-max-tokens", "1024",
                "--sleep-idle-seconds", str(int(idle) if idle > 0 else -1)]
    py = root / "envs" / "qwen-vl" / "bin" / "python"
    if not py.exists():
        raise VLMError(f"{model}: {py} missing; run scripts/install_qwen_vl.sh")
    return [str(py), str(config.REPO_ROOT / "scripts" / "vlm_server.py"),
            "--port", port, "--idle-unload", str(idle)]


def _start(model: str, gpu: int, idle: float) -> dict:
    m = _cfg().get("models", {}).get(model)
    if not m:
        raise VLMError(f"unknown local model {model!r} (config/backends.toml [local.models])")
    if _healthy(m["url"]):
        raise VLMError(f"{m['url']} is already taken by a server this tool didn't start")
    cmd = _command(model, m, gpu, idle)
    log = (_logs() / f"vlm-{model}.log").open("a")
    env = dict(os.environ, HIP_VISIBLE_DEVICES=str(gpu), AP_ROOT=str(config.ap_root()))
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
    deadline = time.time() + 600  # the 32B loads 24 GB of weights before /health is 200
    while not _healthy(m["url"]):
        if proc.poll() is not None:
            raise VLMError(f"{model} server exited with code {proc.returncode}; see {log.name}")
        if time.time() > deadline:
            proc.kill()
            raise VLMError(f"{model} server not ready after 600 s; see {log.name}")
        time.sleep(0.5)
    st = {"model": model, "backend": m["backend"], "url": m["url"], "pid": proc.pid, "gpu": gpu}
    (_logs() / "vlm.json").write_text(json.dumps(st))
    return st


def up(model: str | None = None, gpu: int = 0, idle_unload: float | None = None,
       log=print) -> dict:
    """Start `model` (default: the configured one, then the fallback). A server already
    running with the same model and GPU is reused; a different one is stopped first."""
    if (pids := trellis_jobs(gpu)):
        raise VLMError(f"a TRELLIS job (pid {', '.join(map(str, pids))}) is using GPU {gpu}; "
                       f"try again when it finishes")
    cfg = _cfg()
    order = [model] if model else [cfg.get("model", "32b"), cfg.get("fallback", "8b")]
    st = state()
    if st and st["model"] in order[:1] and st["gpu"] == gpu:
        return st
    if st:
        down()
    idle = cfg.get("idle_unload_s", 600) if idle_unload is None else idle_unload
    err = None
    for m in dict.fromkeys(order):
        try:
            return _start(m, gpu, idle)
        except VLMError as e:
            err = e
            if m != order[-1]:
                log(f"vlm: {e}; falling back")
    raise err


def down(gpu: int | None = None, wait_s: float = 900, force: bool = False, log=print) -> str:
    """Stop the server (only if it's on `gpu`, when given). Waits for a request in flight
    unless force."""
    st = state()
    if not st or (gpu is not None and st["gpu"] != gpu):
        return "not running" if not st else f"running on GPU {st['gpu']}, left alone"
    if not force and busy(st):
        log(f"vlm: waiting for the running request to finish (up to {wait_s:.0f} s)")
        deadline = time.time() + wait_s
        while busy(st) and time.time() < deadline:
            time.sleep(2)
    os.killpg(st["pid"], signal.SIGTERM)
    for _ in range(100):
        if not _alive(st["pid"]):
            break
        time.sleep(0.1)
    else:
        os.killpg(st["pid"], signal.SIGKILL)
    (_logs() / "vlm.json").unlink(missing_ok=True)
    return f"stopped {st['model']} (pid {st['pid']}, GPU {st['gpu']})"


def status() -> dict:
    st = state()
    if not st:
        return {"up": False, "default": _cfg().get("model", "32b")}
    try:
        health = httpx.get(st["url"] + "/health", timeout=2).json()
    except (httpx.HTTPError, ValueError):
        health = {}
    return {"up": True, **st, "busy": busy(st), "health": health}


class AutoLocal:
    """The "local" provider: starts the configured server on first use (GPU 0)."""

    def __init__(self):
        self._client = None

    @property
    def name(self) -> str:
        return self._client.name if self._client else "local"

    def json_text(self, system, prompt, images, schema):
        if self._client is None:
            self._client = client(state() or up())
        return self._client.json_text(system, prompt, images, schema)
