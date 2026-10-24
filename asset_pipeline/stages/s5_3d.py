"""Stage 5 (single-image path): an asset's chosen view -> textured GLB with TRELLIS.2.

Runs scripts/trellis as a subprocess on GPU 0. The wrapper claims the GPU (lock file) and
stops the local VLM first. Under a UI job, Stop kills the subprocess's process group and
removes the claim even if the wrapper had no chance to. However a run ends, its process group
is killed and its claim removed, so nothing it started keeps GPU memory.

Out of GPU memory: the run frees what the app can (the vision model; on one shared GPU
ComfyUI's models), waits for the memory, and retries once. If that fails too the error says in
plain words what was needed, what was free and what holds the GPU (gpu_policy.GpuMemoryError);
the full log stays in trellis.log. AP_TRELLIS_CMD replaces scripts/trellis (tests, simulations).

Every usage tag goes this way for now; hero assets get the multi-view path (orbit video
-> multi-view 3D, PLAN.md) once it exists.

Layout under the project:
    3d/<plan>/<asset id>/<view stem>_<mode>_s<seed>.glb, .json, _input.png
    3d/<plan>/<asset id>/trellis.log
"""
import json
import os
import random
import shutil
import signal
import subprocess
import time
from pathlib import Path

from .. import config, filestore, review
from ..jobs import Canceled, current_job
from ..project import ProjectStore

OUT = "3d"
MODES = ("1024_cascade", "512", "1024")
TRELLIS = config.REPO_ROOT / "scripts" / "trellis"


def out_dir(store: ProjectStore, plan: str, asset_id: str) -> Path:
    return store.root / OUT / plan / asset_id


def _reap(proc: subprocess.Popen, gpu: int) -> None:
    """After the wrapper exits: kill anything left in its process group (it was started in a new
    session, so the group is its own), drop its GPU claim. Never pid 0/1 or our own group: killpg(0)
    would signal the app itself."""
    if isinstance(proc.pid, int) and proc.pid > 1 and proc.pid != os.getpgrp():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    (config.ap_root() / "logs" / f"trellis-gpu{gpu}-{proc.pid}.lock").unlink(missing_ok=True)


def _command() -> list[str]:
    import shlex
    return shlex.split(os.environ["AP_TRELLIS_CMD"]) if os.environ.get("AP_TRELLIS_CMD") else [str(TRELLIS)]


def _failure_line(lines: list[str]) -> str:
    """The last line that says what went wrong (no traceback frames)."""
    for ln in reversed(lines):
        t = ln.strip()
        if t and not t.startswith(("File \"", "Traceback", "^", "~")) and not t.startswith("    "):
            return t[:300]
    return "no output"


def _stop(proc: subprocess.Popen, gpu: int) -> None:
    """SIGTERM the wrapper and TRELLIS (one process group), SIGKILL after 15 s, then drop
    the GPU claim: the wrapper's EXIT trap can't run if it was SIGKILLed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=15)
    (config.ap_root() / "logs" / f"trellis-gpu{gpu}-{proc.pid}.lock").unlink(missing_ok=True)


def run(store: ProjectStore, plan: str, asset_id: str, view: str | None = None,
        mode: str = "1024_cascade", seed: int | None = 42, gpu: int | None = None) -> dict:
    """TRELLIS.2 on the asset's chosen view (or `view`). Returns the run's stats. seed=None draws a
    new random seed (Make 3D, new seed); every result JSON carries the seed it used."""
    if seed is None:
        seed = random.randrange(2**31)
    if gpu is None:
        from .. import gpu_policy
        gpu = gpu_policy.settings()["gpus"]["gpu0"]
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    key = review.rel(store, view) if view else review.chosen(store, plan, asset_id)
    if not key:
        raise ValueError(f"{plan}/{asset_id}: choose a view first")
    out = out_dir(store, plan, asset_id)
    out.mkdir(parents=True, exist_ok=True)
    from .. import versions
    versions.migrate_asset(store, asset_id, plan)       # results from before the bank become versions first
    # TRELLIS writes into a staging dir; its files replace a rerun's old ones (same view, mode and
    # seed: the same version) only once it has succeeded, so a failed run costs no version
    stage = out / "_staging"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    cmd = [*_command(), str(store.root / key),
           "--type", mode, "--seed", str(seed), "--out-dir", str(stage)]
    stem = f"{Path(key).stem[:24]}_{mode}_s{seed}"
    t = time.time()
    job = current_job()
    from .. import gpu_policy, profiles
    for attempt in (1, 2):
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir()
        logf = out / "trellis.log"
        with logf.open("a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}" + (" (retry)" if attempt > 1 else "") + "\n")
            log.flush()
            start = logf.stat().st_size
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                    env=dict(os.environ, TRELLIS_GPU=str(gpu), AP_ROOT=str(config.ap_root()),
                                             **profiles.allocator_env()))
        release = job.on_cancel(lambda: _stop(proc, gpu)) if job else (lambda: None)
        try:
            while proc.poll() is None:
                if job and job.cancel_event.is_set():
                    _stop(proc, gpu)
                    raise Canceled(f"TRELLIS on {key} canceled")
                time.sleep(1)
        finally:
            release()
            _reap(proc, gpu)                              # ended, failed or canceled: nothing keeps VRAM
        if job and job.cancel_event.is_set():
            shutil.rmtree(stage, ignore_errors=True)
            raise Canceled(f"TRELLIS on {key} canceled")
        if proc.returncode == 0 and (stage / f"{stem}.json").is_file():
            break
        shutil.rmtree(stage, ignore_errors=True)
        with logf.open() as fh:
            fh.seek(start)
            run_log = fh.read()
        if gpu_policy.is_oom(run_log):
            if job:
                job.note = "Out of GPU memory: freeing memory and trying once more…"
            need, free, holders = gpu_policy.free_for_retry("gpu0", "gpu", job)
            if attempt == 1:
                continue
            lighter = "pick a lighter 3D mode (512)" if mode != "512" else "free the GPU first"
            raise gpu_policy.GpuMemoryError(gpu_policy.oom_message(need, free, holders, lighter))
        raise RuntimeError(f"TRELLIS failed (exit code {proc.returncode}): {_failure_line(run_log.splitlines())}. "
                           f"The full log is {OUT}/{plan}/{asset_id}/trellis.log.")
    for f in sorted(stage.iterdir()):
        filestore.detach(out / f.name)                  # a rerun's old files: own files, not shared ones
        os.replace(f, out / f.name)
    shutil.rmtree(stage, ignore_errors=True)
    stats_file = out / f"{stem}.json"
    stats = json.loads(stats_file.read_text())
    if stats.get("seed") != seed:
        stats["seed"] = seed
        stats_file.write_text(json.dumps(stats, indent=2))
    record = {"view": key, "mode": mode, "seed": seed, "glb": f"{OUT}/{plan}/{asset_id}/{stem}.glb",
              "input": f"{OUT}/{plan}/{asset_id}/{stem}_input.png", "stats": stem + ".json",
              "seconds": round(time.time() - t, 1)}
    filestore.intern(store, out / f"{stem}.glb")
    versions.record_trellis(store, plan, asset_id, record)          # every run is a version
    store.log_run({"stage": "3d.trellis", "plan": plan, "asset": asset_id, **record})
    return record


def result_time(glb: Path) -> float:
    """When a result was made (or restored): its stats JSON's mtime. The GLB may be a shared
    file-store reference whose mtime belongs to every entry linking it; the JSON is the entry's."""
    j = glb.with_suffix(".json")
    return (j if j.is_file() else glb).stat().st_mtime


def results(store: ProjectStore, plan: str, asset_id: str) -> list[dict]:
    """Finished GLBs for an asset, newest first."""
    out = out_dir(store, plan, asset_id)
    rows = []
    for glb in sorted(out.glob("*.glb"), key=lambda p: -result_time(p)):
        stats = json.loads(glb.with_suffix(".json").read_text()) if glb.with_suffix(".json").is_file() else {}
        rows.append({"glb": glb.relative_to(store.root).as_posix(),
                     "input": (glb.parent / f"{glb.stem}_input.png").relative_to(store.root).as_posix(),
                     "faces": stats.get("glb_faces"), "vertices": stats.get("glb_vertices"),
                     "raw_ratio": round(stats["raw_faces"] / stats["raw_vertices"], 2)
                     if stats.get("raw_vertices") else None,
                     "peak_vram_gb": stats.get("peak_vram_gb"), "seed": stats.get("seed"), "name": glb.stem,
                     "file_bytes": glb.stat().st_size})
    return rows
