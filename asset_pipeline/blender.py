"""Headless Blender runs shared by the stages (cleanup, greybox, shot passes).

Blender runs on the CPU here, so it never competes with GPU jobs. Its output goes through a
pipe into the log: the snap Blender silently does nothing (exit 0, no output) when its
stdout is a regular file. A running job's Stop kills the whole process group.
"""
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .jobs import Canceled, current_job

BLENDER = Path("/snap/bin/blender")


def run(script: Path, args: list[str], log_path: Path, what: str, blend: Path | None = None,
        exe: Path | None = None) -> None:
    """Run `blender -b [blend] --factory-startup --python script -- args`, appending its
    output to log_path. Raises Canceled if the job is stopped, RuntimeError (with the
    log's tail) on a non-zero exit."""
    cmd = [str(exe or BLENDER), "-b"] + ([str(blend)] if blend else []) + \
          ["--factory-startup", "--python", str(script), "--", *map(str, args)]
    run_logged(cmd, log_path, what)


def run_logged(cmd: list[str], log_path: Path, what: str, cwd: Path | None = None) -> None:
    """Run any command with its output piped into log_path (appended), cancellable from a
    job's Stop (the whole process group is killed)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a")
    log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, cwd=cwd, errors="replace")
    pump = threading.Thread(target=lambda: [log.write(line) or log.flush() for line in proc.stdout],
                            daemon=True)
    pump.start()
    job = current_job()
    release = job.on_cancel(lambda: stop(proc)) if job else (lambda: None)
    try:
        while proc.poll() is None:
            if job and job.cancel_event.is_set():
                stop(proc)
                raise Canceled(f"{what} canceled")
            time.sleep(0.5)
    finally:
        release()
        pump.join(timeout=5)
        log.close()
    if job and job.cancel_event.is_set():
        raise Canceled(f"{what} canceled")
    if proc.returncode != 0:
        tail = log_path.read_text().splitlines()[-15:]
        raise RuntimeError(f"{Path(cmd[0]).name} exited with code {proc.returncode}:\n" + "\n".join(tail))


def stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
