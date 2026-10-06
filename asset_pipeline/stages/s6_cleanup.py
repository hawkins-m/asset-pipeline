"""Stage 6: a 3D result -> a cleaned GLB in headless Blender (scripts/blender_cleanup.py).

Every asset: uniform scale to the plan's real-world size (height fit by default), pivot at
the base centre, transforms applied. Game-tagged assets also get decimated to a triangle
budget for their category with base colour, roughness and normal maps baked from the
original. Cine and hero assets keep the full mesh and textures. Blender runs on the CPU
(Workbench/Cycles CPU), so this never competes with GPU jobs.

Layout under the project:
    cleanup/<plan>/<asset id>/<source stem>_<usage>.glb + .json (report)
"""
import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from .. import config
from ..jobs import Canceled, current_job
from ..project import ProjectStore
from . import s1_plan, s5_3d

OUT = "cleanup"
BLENDER = Path("/snap/bin/blender")
SCRIPT = config.REPO_ROOT / "scripts" / "blender_cleanup.py"
# Game triangle budgets by plan category (the bake carries the detail). Measured
# 2026-10-05 in Cycles: a market stall (thin posts, awning) warped at 15k and was close to
# the original at 30k; a house was fine at 30k. Others are starting points.
BUDGETS = {"prop": 10000, "structure": 30000, "building": 30000, "vegetation": 15000,
           "rock": 8000, "vehicle": 25000, "terrain": 10000, "other": 12000}


def out_dir(store: ProjectStore, plan: str, asset_id: str) -> Path:
    return store.root / OUT / plan / asset_id


def _asset(store: ProjectStore, plan: str, asset_id: str):
    p = s1_plan.load(store, plan)
    if not p:
        raise FileNotFoundError(f"no plan {plan}")
    for a in p.assets:
        if a.id == asset_id:
            return a
    raise ValueError(f"no asset {asset_id!r} in plan {plan}")


def run(store: ProjectStore, plan: str, asset_id: str, glb: str | None = None,
        fit: str = "height", budget: int | None = None, texture_size: int = 2048) -> dict:
    """Clean the asset's newest 3D result (or `glb`, project-relative). Returns the report."""
    if fit not in ("height", "geomean"):
        raise ValueError("fit must be height or geomean")
    a = _asset(store, plan, asset_id)
    if glb:
        src = store.root / glb
    else:
        results = s5_3d.results(store, plan, asset_id)
        if not results:
            raise ValueError(f"{plan}/{asset_id}: no 3D result yet (Make 3D first)")
        src = store.root / results[0]["glb"]
    if not src.is_file():
        raise FileNotFoundError(f"no such GLB: {src}")
    d = a.dimensions
    retopo = (budget or BUDGETS.get(a.category, 10000)) if a.usage == "game" else 0
    out = out_dir(store, plan, asset_id)
    out.mkdir(parents=True, exist_ok=True)
    dst = out / f"{src.stem}_{a.usage}.glb"
    report = dst.with_suffix(".json")
    cmd = [str(BLENDER), "-b", "--factory-startup", "--python", str(SCRIPT), "--",
           "--in", str(src), "--out", str(dst), "--report", str(report),
           "--dims", f"{d.width},{d.depth},{d.height}", "--fit", fit,
           "--retopo", str(retopo), "--texture-size", str(texture_size)]
    t = time.time()
    log = (out / "blender.log").open("a")
    log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(cmd)}\n")
    log.flush()
    # Through a pipe, not straight into the log file: the snap Blender silently does
    # nothing (exit 0, no output, no report) when its stdout is a regular file.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    pump = threading.Thread(target=lambda: [log.write(line) or log.flush() for line in proc.stdout],
                            daemon=True)
    pump.start()
    job = current_job()
    release = job.on_cancel(lambda: _stop(proc)) if job else (lambda: None)
    try:
        while proc.poll() is None:
            if job and job.cancel_event.is_set():
                _stop(proc)
                raise Canceled(f"cleanup of {plan}/{asset_id} canceled")
            time.sleep(0.5)
    finally:
        release()
        pump.join(timeout=5)
        log.close()
    if job and job.cancel_event.is_set():
        raise Canceled(f"cleanup of {plan}/{asset_id} canceled")
    if proc.returncode != 0 or not report.is_file():
        tail = (out / "blender.log").read_text().splitlines()[-15:]
        raise RuntimeError(f"blender exited with code {proc.returncode}:\n" + "\n".join(tail))
    rep = json.loads(report.read_text()) | {"usage": a.usage, "glb": dst.relative_to(store.root).as_posix(),
                                            "source": src.relative_to(store.root).as_posix()}
    report.write_text(json.dumps(rep, indent=2))
    store.log_run({"stage": "cleanup", "plan": plan, "asset": asset_id, "usage": a.usage,
                   "faces": rep["output_faces"], "warnings": rep["warnings"],
                   "seconds": round(time.time() - t, 1)})
    return rep


def _stop(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


def results(store: ProjectStore, plan: str, asset_id: str) -> list[dict]:
    """Cleanup reports for an asset, newest first."""
    out = out_dir(store, plan, asset_id)
    reps = []
    for f in sorted(out.glob("*.json"), key=lambda p: -p.stat().st_mtime):
        rep = json.loads(f.read_text())
        if (store.root / rep.get("glb", "")).is_file():
            reps.append({k: rep.get(k) for k in ("glb", "source", "usage", "output_faces", "output_dims_m",
                                                  "dims_vs_plan", "warnings", "retopo_method", "seconds")})
    return reps
