"""Stage 5 (single-image path): an asset's chosen view -> textured GLB with TRELLIS.2.

Runs scripts/trellis as a subprocess on GPU 0. The wrapper claims the GPU (lock file) and
stops the local VLM first. Under a UI job, Stop kills the subprocess's process group and
removes the claim even if the wrapper had no chance to.

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
        mode: str = "1024_cascade", seed: int | None = 42, gpu: int = 0) -> dict:
    """TRELLIS.2 on the asset's chosen view (or `view`). Returns the run's stats. seed=None draws a
    new random seed (Make 3D, new seed); every result JSON carries the seed it used."""
    if seed is None:
        seed = random.randrange(2**31)
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
    cmd = [str(TRELLIS), str(store.root / key),
           "--type", mode, "--seed", str(seed), "--out-dir", str(stage)]
    stem = f"{Path(key).stem[:24]}_{mode}_s{seed}"
    t = time.time()
    with (out / "trellis.log").open("a") as log:
        log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                                env=dict(os.environ, TRELLIS_GPU=str(gpu), AP_ROOT=str(config.ap_root())))
    job = current_job()
    release = job.on_cancel(lambda: _stop(proc, gpu)) if job else (lambda: None)
    try:
        while proc.poll() is None:
            if job and job.cancel_event.is_set():
                _stop(proc, gpu)
                raise Canceled(f"TRELLIS on {key} canceled")
            time.sleep(1)
    finally:
        release()
    if job and job.cancel_event.is_set():
        shutil.rmtree(stage, ignore_errors=True)
        raise Canceled(f"TRELLIS on {key} canceled")
    if proc.returncode != 0 or not (stage / f"{stem}.json").is_file():
        shutil.rmtree(stage, ignore_errors=True)
        tail = (out / "trellis.log").read_text().splitlines()[-15:]
        raise RuntimeError(f"trellis exited with code {proc.returncode}:\n" + "\n".join(tail))
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
