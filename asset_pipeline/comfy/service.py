"""Start, stop and watch the local ComfyUI server (and comfy-api-proxy for the sdk backend).

This only launches and stops ComfyUI with the machine's own launch command; it never changes
ComfyUI's install or environment. Settings, in config [comfyui.service] (machine values go in
$AP_ROOT/backends.local.toml):

    launch            command line; {port} and {url} are filled in, ~ is expanded
    cwd               working directory for it ("" = the current one)
    gpu               GPU index to pin it to (HIP_VISIBLE_DEVICES / CUDA_VISIBLE_DEVICES,
                      asset_pipeline.gpu); "" leaves the environment alone
    start_timeout_s   how long to wait for it to answer

The port is the one in [comfyui] url, so the server and the clients can't disagree. ComfyUI
runs detached (its own session) and logs to $AP_ROOT/logs/comfyui.log; it keeps running when
the app or the CLI exits. Stop sends SIGTERM to the process group this module started, or,
for a ComfyUI started by hand, to the process listening on the port (only if its command line
looks like ComfyUI), then SIGKILL after a grace period.

The UI runs start / stop / restart as jobs in the "service" lane; `Gate` holds jobs of the
"comfy" lane until ComfyUI answers (see jobs.JobQueue gates).
"""
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .. import config, gpu as gpu_mod

PROXY_SCRIPT = config.REPO_ROOT / "scripts" / "comfy_api_proxy.sh"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_RE = re.compile(r"Traceback|Error|Exception|Cannot import|IMPORT FAILED|out of memory", re.I)
DEFAULTS = {"launch": "python main.py --listen 127.0.0.1 --port {port}", "cwd": "~/ComfyUI", "gpu": "",
            "start_timeout_s": 300, "stop_timeout_s": 30}


class ComfyDown(RuntimeError):
    """ComfyUI isn't running and nothing is starting it (the UI offers "Start ComfyUI and run")."""


def settings() -> dict:
    c = config.backends()["comfyui"]
    s = DEFAULTS | (c.get("service") or {})
    u = urlparse(c["url"])
    s["url"] = c["url"].rstrip("/")
    s["port"] = u.port or (443 if u.scheme == "https" else 80)
    s["host"] = u.hostname or "127.0.0.1"
    s["backend"] = os.environ.get("AP_COMFY_BACKEND") or c.get("backend", "sdk")
    s["sdk_url"] = (os.environ.get("AP_COMFY_SDK_URL") or c.get("sdk_url") or "http://127.0.0.1:8190").rstrip("/")
    s["log"] = config.ap_root() / "logs" / "comfyui.log"
    s["pidfile"] = config.ap_root() / "logs" / "comfyui.json"
    return s


def command(s: dict | None = None) -> list[str]:
    s = s or settings()
    return [os.path.expanduser(t) for t in shlex.split(str(s["launch"]).format(port=s["port"], url=s["url"]))]


def launch_env(s: dict | None = None) -> dict:
    s = s or settings()
    env = dict(os.environ)
    if str(s.get("gpu", "")).strip() != "":
        env.update(gpu_mod.visible_devices_env(int(s["gpu"])))
    return env


# --- health ---------------------------------------------------------------------------------

def healthy(s: dict | None = None, timeout: float = 2.0) -> bool:
    """ComfyUI itself answers (through the ComfyClient interface, at [comfyui] url: the proxy
    in front of it for the sdk backend is checked separately, proxy_up)."""
    from .client import ComfyClient, ComfyError
    s = s or settings()
    c = ComfyClient(s["url"], timeout_s=timeout)
    c.http.timeout = httpx.Timeout(timeout)          # a quick probe, not the client's 60 s
    try:
        c.health()
        return True
    except (ComfyError, httpx.HTTPError, ValueError):
        return False
    finally:
        c.http.close()


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def proxy_up(s: dict | None = None) -> bool:
    s = s or settings()
    u = urlparse(s["sdk_url"])
    return _port_open(u.hostname or "127.0.0.1", u.port or 8190)


def ready(s: dict | None = None) -> bool:
    """ComfyUI answers (and the proxy is up when the backend is sdk)."""
    s = s or settings()
    return healthy(s) and (s["backend"] != "sdk" or proxy_up(s))


# --- processes ------------------------------------------------------------------------------

def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    st = Path(f"/proc/{pid}/stat")
    return not (st.is_file() and ") Z" in st.read_text())     # a zombie isn't running


def _argv(pid: int) -> list[str]:
    try:
        return [a.decode(errors="replace") for a in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if a]
    except OSError:
        return []


def _cmdline(pid: int) -> str:
    return " ".join(_argv(pid))


def listener_pid(port: int) -> int | None:
    """The process listening on a TCP port (ss), or None. Matching by PID, never pkill -f."""
    try:
        out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"pid=(\d+)", out)
    return int(m.group(1)) if m else None


def looks_like_comfy(argv: list[str]) -> bool:
    """A ComfyUI server: python running a main.py, or a comfyui / comfy executable. Judged by
    program names, not substrings: a substring match on the whole command line also hits
    test runs and editors with "comfy" in a path (the pkill -f trap in CLAUDE.md and docs/notes/world-and-city.md)."""
    names = [os.path.basename(a).lower() for a in argv[:3]]
    return "main.py" in names or any(n in ("comfyui", "comfy") for n in names)


def _foreign(pid: int) -> bool:
    """Never signal this process or its parent, whatever listens on the port."""
    return pid in (os.getpid(), os.getppid())


def _read_pidfile(s: dict) -> dict:
    try:
        return json.loads(Path(s["pidfile"]).read_text())
    except (OSError, ValueError):
        return {}


def status(s: dict | None = None) -> dict:
    s = s or settings()
    rec = _read_pidfile(s)
    managed = rec.get("pid") if _alive(rec.get("pid")) and rec.get("port") == s["port"] else None
    lpid = listener_pid(s["port"])
    up = healthy(s)
    out = {"url": s["url"], "port": s["port"], "gpu": s.get("gpu", ""), "backend": s["backend"], "up": up,
           "pid": managed or lpid, "managed": bool(managed), "log": str(s["log"]),
           "state": "running" if up else ("starting" if managed or lpid else "stopped")}
    if s["backend"] == "sdk":
        out["proxy"] = {"url": s["sdk_url"], "up": proxy_up(s)}
    return out


def _tail_errors(path: Path, since: int = 0, n: int = 8) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(since)
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    errs = [ln for ln in lines if ERROR_RE.search(ln)]
    return (errs or lines)[-n:]


def start(log=print, on_line=None, cancel=None, s: dict | None = None) -> dict:
    """Launch ComfyUI if it isn't answering, wait until it does (streaming its log lines to
    on_line), then the proxy for the sdk backend. Raises RuntimeError with the log's error
    lines if it exits or doesn't answer in time. cancel() -> True stops waiting."""
    s = s or settings()
    on_line = on_line or (lambda ln: None)
    if healthy(s):
        log(f"ComfyUI already running at {s['url']}")
    else:
        lpid = listener_pid(s["port"])
        logp = Path(s["log"])
        logp.parent.mkdir(parents=True, exist_ok=True)
        offset = logp.stat().st_size if logp.is_file() else 0
        proc = None
        if lpid:   # something holds the port but doesn't answer yet: a ComfyUI still loading?
            if _foreign(lpid) or not looks_like_comfy(_argv(lpid)):
                raise RuntimeError(f"port {s['port']} is in use by pid {lpid} ({_cmdline(lpid)[:120]}), not ComfyUI")
            log(f"ComfyUI (pid {lpid}) is starting; waiting for it")
        else:
            cmd, cwd = command(s), os.path.expanduser(s.get("cwd") or "") or None
            if cwd and not Path(cwd).is_dir():
                raise RuntimeError(f"ComfyUI cwd {cwd} does not exist: set [comfyui.service] cwd")
            log(f"starting ComfyUI: {shlex.join(cmd)}" + (f" (GPU {s['gpu']})" if str(s.get("gpu", "")) != "" else ""))
            with open(logp, "ab") as lf:
                lf.write(f"\n--- ap comfy start {time.strftime('%Y-%m-%d %H:%M:%S')}: {shlex.join(cmd)}\n".encode())
                lf.flush()
                try:
                    proc = subprocess.Popen(cmd, cwd=cwd, env=launch_env(s), stdin=subprocess.DEVNULL, stdout=lf,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as e:
                    raise RuntimeError(f"could not run the launch command {cmd[0]!r}: {e}. Set [comfyui.service] launch") from e
            Path(s["pidfile"]).write_text(json.dumps({"pid": proc.pid, "port": s["port"], "cmd": cmd,
                                                      "started": time.time()}))
        t0, pos = time.time(), offset
        while not healthy(s, timeout=1.0):
            pos = _stream(logp, pos, on_line)
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(f"ComfyUI exited with code {proc.returncode} during startup:\n"
                                   + "\n".join(_tail_errors(logp, offset)))
            if proc is None and not listener_pid(s["port"]) and not _alive(lpid):
                raise RuntimeError("ComfyUI stopped during startup:\n" + "\n".join(_tail_errors(logp, offset)))
            if cancel and cancel():
                raise RuntimeError("stopped waiting for ComfyUI (it may still be starting; Stop ends it)")
            if time.time() - t0 > float(s["start_timeout_s"]):
                raise RuntimeError(f"ComfyUI did not answer within {s['start_timeout_s']} s (still running; "
                                   f"see {logp}):\n" + "\n".join(_tail_errors(logp, offset)))
            time.sleep(1.0)
        _stream(logp, pos, on_line)
        log(f"ComfyUI answering at {s['url']} after {time.time() - t0:.0f} s")
    if s["backend"] == "sdk" and not proxy_up(s):
        start_proxy(s, log)
    return status(s)


def _stream(path: Path, pos: int, on_line) -> int:
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            chunk = f.read()
    except OSError:
        return pos
    end = chunk.rfind(b"\n")
    if end < 0:
        return pos
    for ln in ANSI_RE.sub("", chunk[:end].decode(errors="replace")).splitlines():
        if ln.strip() and ln.strip() not in ("[INFO]", "[WARNING]"):     # colour codes off, bare level tags skipped
            on_line(ln.rstrip())
    return pos + end + 1


def start_proxy(s: dict | None = None, log=print) -> None:
    s = s or settings()
    port = urlparse(s["sdk_url"]).port or 8190
    log(f"starting comfy-api-proxy on port {port}")
    env = dict(os.environ, COMFYUI_URL=s["url"], PROXY_PORT=str(port), AP_ROOT=str(config.ap_root()))
    r = subprocess.run(["bash", str(PROXY_SCRIPT), "start"], env=env, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"comfy-api-proxy did not start: {(r.stderr or r.stdout).strip()[-500:]}")
    for _ in range(30):
        if proxy_up(s):
            return
        time.sleep(0.5)
    raise RuntimeError(f"comfy-api-proxy started but {s['sdk_url']} isn't answering")


def stop_proxy(s: dict | None = None, log=print) -> None:
    s = s or settings()
    env = dict(os.environ, AP_ROOT=str(config.ap_root()))
    r = subprocess.run(["bash", str(PROXY_SCRIPT), "stop"], env=env, capture_output=True, text=True, timeout=60)
    log((r.stdout or r.stderr).strip() or "comfy-api-proxy stopped")


def stop(log=print, s: dict | None = None) -> dict:
    """Stop ComfyUI (and the proxy when the backend is sdk). Returns the status after."""
    s = s or settings()
    if s["backend"] == "sdk" and proxy_up(s):
        stop_proxy(s, log)
    rec = _read_pidfile(s)
    pid = rec.get("pid") if _alive(rec.get("pid")) and rec.get("port") == s["port"] else None
    group = pid is not None
    if pid is None:
        pid = listener_pid(s["port"])
        if pid and (_foreign(pid) or not looks_like_comfy(_argv(pid))):
            raise RuntimeError(f"port {s['port']} is held by pid {pid} ({_cmdline(pid)[:120]}), not ComfyUI: not stopping it")
    if pid is None:
        log("ComfyUI is not running")
        Path(s["pidfile"]).unlink(missing_ok=True)
        return status(s)

    def send(sig):
        try:
            os.killpg(pid, sig) if group else os.kill(pid, sig)
        except ProcessLookupError:
            pass
    log(f"stopping ComfyUI (pid {pid})")
    send(signal.SIGTERM)
    t0 = time.time()
    while (_alive(pid) or listener_pid(s["port"])) and time.time() - t0 < float(s["stop_timeout_s"]):
        time.sleep(0.5)
    if _alive(pid) or listener_pid(s["port"]):
        log("still running after SIGTERM: SIGKILL")
        send(signal.SIGKILL)
        lp = listener_pid(s["port"])
        if lp and lp != pid and not _foreign(lp) and looks_like_comfy(_argv(lp)):
            os.kill(lp, signal.SIGKILL)
        time.sleep(1.0)
    Path(s["pidfile"]).unlink(missing_ok=True)
    log("ComfyUI stopped")
    return status(s)


def restart(log=print, on_line=None, cancel=None) -> dict:
    stop(log)
    return start(log, on_line, cancel)


# --- the UI's gate for jobs that need ComfyUI -------------------------------------------------

class Gate:
    """Holds "comfy" lane jobs: submit() refuses (ComfyDown) when ComfyUI is down and nothing
    is starting it; wait(job) blocks a queued job until ComfyUI answers, or fails it when the
    start failed. The service jobs report their state here."""

    def __init__(self, ready_fn=ready, poll_s: float = 1.0):
        self.ready_fn, self.poll_s = ready_fn, poll_s
        self.state, self.error = "idle", None      # idle | starting | failed
        self.lines: deque = deque(maxlen=200)
        self._lock = threading.Lock()
        self._cache = (0.0, False)

    def is_ready(self, max_age: float = 2.0) -> bool:
        t, up = self._cache
        if time.time() - t > max_age:
            up = bool(self.ready_fn())
            self._cache = (time.time(), up)
        return up

    def starting(self) -> None:
        with self._lock:
            self.state, self.error = "starting", None
            self._cache = (0.0, False)

    def finished(self, error: str | None = None) -> None:
        with self._lock:
            self.state, self.error = ("failed", error) if error else ("idle", None)
            self._cache = (0.0, False)

    def check_submit(self) -> None:
        if self.state == "starting" or self.is_ready(max_age=0):
            return
        raise ComfyDown("ComfyUI is not running")

    def wait(self, job=None) -> None:
        from ..jobs import check_canceled
        while not self.is_ready(max_age=0):
            if self.state == "failed":
                raise RuntimeError(f"ComfyUI did not start: {self.error}")
            if self.state != "starting":
                raise ComfyDown("ComfyUI is not running (it stopped before this job ran)")
            if job is not None:
                job.note = "waiting for ComfyUI to start" + (f" · {self.lines[-1][:120]}" if self.lines else "")
            check_canceled()
            time.sleep(self.poll_s)
        if job is not None:
            job.note = ""
