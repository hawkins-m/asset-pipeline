"""Unreal Engine 5.8 side of world mode: project setup, import, renders, layout read-back,
backups.

The UE project lives outside the repo, at <[unreal] projects_dir>/<slug>/<Name>.uproject
(default ~/Projects/Unreal, on a different drive from $AP_ROOT); `backup()` snapshots it to
<[unreal] backup_dir>/<slug>/<timestamp>/. Pipeline-imported content is disposable (re-import
recreates it); backups are for the work done in UE itself (lighting, Sequencer...). Editor
Python scripts in unreal/ run headless through UnrealEditor-Cmd's pythonscript commandlet
with -nullrhi: no GPU, so imports never compete with ComfyUI or TRELLIS. Each script takes
a single JSON args file (no quoting trouble in -script=) and writes a JSON report.

Engine: [unreal] editor_cmd in config/backends.toml, or AP_UE_EDITOR_CMD.
"""
import json
import os
import re
import shutil
import time
from pathlib import Path

import numpy as np

from . import blender, config, ue_coords
from .project import ProjectStore, read_json, write_json
from .stages import s7_export, sw_site

SCRIPTS = config.REPO_ROOT / "unreal"
ENGINE_VERSION = "5.8"
PLUGINS = ["PythonScriptPlugin", "EditorScriptingUtilities", "Interchange", "LevelSequenceEditor"]
# Nanite needs SM6 (Vulkan on Linux); Lumen + virtual shadow maps suit Nanite and need no
# lightmap builds.
DEFAULT_ENGINE_INI = """[/Script/Engine.RendererSettings]
r.DynamicGlobalIlluminationMethod=1
r.ReflectionMethod=1
r.Shadow.Virtual.Enable=1
r.GenerateMeshDistanceFields=True
r.AllowStaticLighting=False
r.Nanite.ProjectEnabled=True

[/Script/LinuxTargetPlatform.LinuxTargetSettings]
-TargetedRHIs=SF_VULKAN_SM5
+TargetedRHIs=SF_VULKAN_SM6
"""
POSITION_TOLERANCE_CM = 1.0


def editor_cmd() -> Path:
    path = os.environ.get("AP_UE_EDITOR_CMD") or config.backends().get("unreal", {}).get("editor_cmd")
    if not path or not Path(path).is_file():
        raise FileNotFoundError(f"UnrealEditor-Cmd not found ({path!r}): set [unreal] editor_cmd in "
                                f"config/backends.toml or AP_UE_EDITOR_CMD")
    return Path(path)


def _setting(env: str, key: str, default):
    return os.environ.get(env) or config.backends().get("unreal", {}).get(key, default)


def projects_dir() -> Path:
    return Path(_setting("AP_UE_PROJECTS", "projects_dir", "~/Projects/Unreal")).expanduser()


def backups_dir() -> Path:
    return Path(_setting("AP_UE_BACKUPS", "backup_dir", "/mnt/storage/backups/unreal")).expanduser()


def project_dir(store: ProjectStore) -> Path:
    return projects_dir() / store.load().slug


def uproject(store: ProjectStore) -> Path:
    return project_dir(store) / f"{s7_export.pascal(store.load().slug)}.uproject"


def work_dir(store: ProjectStore) -> Path:
    return s7_export.export_dir(store) / "ue"


def init(store: ProjectStore) -> Path:
    """Create the UE project (idempotent: an existing .uproject is kept)."""
    up = uproject(store)
    if up.is_file():
        return up
    d = up.parent
    (d / "Content").mkdir(parents=True, exist_ok=True)
    (d / "Config").mkdir(exist_ok=True)
    up.write_text(json.dumps({
        "FileVersion": 3, "EngineAssociation": ENGINE_VERSION, "Category": "",
        "Description": f"asset-pipeline world: {store.load().slug}",
        "Plugins": [{"Name": p, "Enabled": True} for p in PLUGINS]}, indent=2) + "\n")
    ini = d / "Config" / "DefaultEngine.ini"
    if not ini.exists():
        ini.write_text(DEFAULT_ENGINE_INI)
    store.log_run({"stage": "ue.init", "uproject": str(up)})
    return up


def run_script(store: ProjectStore, script: str, args: dict, what: str) -> dict:
    """Run unreal/<script> in the headless editor; returns its JSON report."""
    up = init(store)
    work = work_dir(store)
    work.mkdir(parents=True, exist_ok=True)
    name = Path(script).stem
    report = work / f"{name}_report.json"
    report.unlink(missing_ok=True)
    args_file = work / f"{name}_args.json"
    write_json(args_file, args | {"report": str(report)})
    cmd = [str(editor_cmd()), str(up), "-run=pythonscript", f"-script={SCRIPTS / script} {args_file}",
           "-unattended", "-nosplash", "-nullrhi", "-NoTraceServer", "-stdout", "-FullStdOutLogOutput", "-NoLogTimes"]
    log = work / f"{name}.log"
    log.unlink(missing_ok=True)
    try:
        blender.run_logged(cmd, log, what, cwd=up.parent)
        exit_error = None
    except RuntimeError as e:   # the commandlet exits 1 whenever anything logged an error
        exit_error = str(e).splitlines()[0]
    rep = read_json(report)
    if rep is None:
        tail = log.read_text(errors="replace").splitlines()[-20:] if log.exists() else []
        raise RuntimeError(f"{what}: the editor wrote no report ({exit_error or 'exit 0'}):\n" + "\n".join(tail))
    rep["log_errors"] = log_errors(log)
    if exit_error:
        rep["exit"] = exit_error
    return rep


def log_errors(log: Path, limit: int = 20) -> list[str]:
    """Distinct error lines of an editor log (engine noise about the trace server excluded)."""
    seen = []
    for line in log.read_text(errors="replace").splitlines():
        if ("Error:" in line or "LogPython: Error" in line) and "Warning/Error Summary" not in line \
                and "UnrealTrace" not in line and line not in seen:
            seen.append(line.strip())
    return seen[:limit]


def verify_samples(gb: dict, index: dict, samples: list[dict]) -> tuple[list[str], list[str]]:
    """Each sampled UE instance must sit where the greybox puts some instance of that asset
    in that slot (order may differ), relative to the slot actor's *current* UE transform:
    from_ue(actor) @ piece.matrix_local, mirrored to cm. That checks the mirror and rotator
    conventions end to end, independently of the importer's arithmetic, and stays valid
    after the slot was moved in UE. Returns (errors, slots moved away from the greybox)."""
    pieces: dict[tuple[str, str], list[np.ndarray]] = {}
    slot_m = {s["id"]: np.asarray(s["matrix"]) for s in gb["slots"]}
    for s in gb["slots"]:
        for p in s["pieces"]:
            pieces.setdefault((s["id"], index[p["mesh"]]["id"]), []).append(np.asarray(p["matrix_local"]))
    bad, moved = [], set()
    for smp in samples:
        actor = ue_coords.from_ue(smp["actor"])
        if not np.allclose(actor, slot_m[smp["slot"]], atol=1e-3):
            moved.add(smp["slot"])
        cands = [ue_coords.C @ (actor @ m)[:3, 3] * ue_coords.M_TO_CM for m in pieces.get((smp["slot"], smp["asset"]), [])]
        d = min((float(np.linalg.norm(np.asarray(smp["world_cm"]) - c)) for c in cands), default=float("inf"))
        if d > POSITION_TOLERANCE_CM:
            bad.append(f"{smp['slot']}/{smp['asset']}: {d:.1f} cm from the greybox")
    return bad, sorted(moved)


def import_(store: ProjectStore, reset_layout: bool = False, prune: bool = False) -> dict:
    """Import export/manifest.json into the project's UE level."""
    man_path = s7_export.export_dir(store) / "manifest.json"
    if not man_path.is_file():
        raise FileNotFoundError("no export/manifest.json (run `ap export` first)")
    t = time.time()
    rep = run_script(store, "ap_import.py", {"manifest": str(man_path), "reset_layout": reset_layout,
                                             "prune": prune}, "UE import")
    if "error" in rep:
        raise RuntimeError(f"UE import failed: {rep['error']}\n{rep.get('traceback', '')}")
    gb = sw_site.load_greybox(store)
    index = read_json(s7_export.export_dir(store) / "mesh_index.json")
    rep["sample_errors"], rep["moved_in_ue"] = verify_samples(gb, index, rep["checks"]["samples"])
    rep["ok"] = rep["ok"] and not rep["sample_errors"]
    rep["seconds"] = round(time.time() - t, 1)
    write_json(work_dir(store) / "ap_import_report.json", rep)
    store.log_run({"stage": "ue.import", "ok": rep["ok"], "imported": len(rep["assets"]["imported"]),
                   "created": len(rep["actors"]["created"]), "instances": rep["checks"]["instances_total"],
                   "seconds": rep["seconds"]})
    return rep


def render_shots(store: ProjectStore, shots: list[str] | None = None, timeout_s: int = 5400) -> dict:
    """Render the level's shot cameras to export/ue/shots/<shot>.png with the full editor
    offscreen on GPU 0 (Vulkan adapter 0; ComfyUI owns GPU 1). The first run compiles the
    project's shaders, which takes a while; later runs reuse the shader cache."""
    from . import vlm
    locks = list((config.ap_root() / "logs").glob("trellis-gpu0-*.lock"))
    if locks:
        raise RuntimeError(f"GPU 0 is busy with TRELLIS ({locks[0].name}); try again when it's done")
    vlm.down(gpu=0)
    man = s7_export.load_manifest(store)
    want = [s for s in man["shots"] if not shots or s["id"] in shots]
    if shots and len(want) != len(shots):
        raise ValueError(f"unknown shot(s): {sorted(set(shots) - {s['id'] for s in want})}")
    up = init(store)
    work = work_dir(store)
    out = work / "shots"
    report = work / "ap_shot_render_report.json"
    report.unlink(missing_ok=True)
    args_file = work / "ap_shot_render_args.json"
    write_json(args_file, {"map": man["map"], "out_dir": str(out), "report": str(report), "timeout_s": timeout_s,
                           "shots": [{"id": s["id"], "resolution": s["resolution"]} for s in want]})
    editor = editor_cmd().with_name("UnrealEditor")
    cmd = [str(editor), str(up), "-RenderOffscreen", "-graphicsadapter=0", "-unattended", "-nosplash", "-NoTraceServer",
           # -ExecutePythonScript would quit the editor right after the script returns (before
           # its tick callbacks run); a console `py` command leaves the quitting to the script
           "-NoSound", f"-ExecCmds=py {SCRIPTS / 'ap_shot_render.py'}", "-stdout",
           "-FullStdOutLogOutput", "-NoLogTimes"]
    os.environ["AP_UE_ARGS"] = str(args_file)
    log = work / "ap_shot_render.log"
    log.unlink(missing_ok=True)
    try:
        blender.run_logged(cmd, log, "UE shot render", cwd=up.parent)
    except RuntimeError:
        pass  # judged by the report
    finally:
        os.environ.pop("AP_UE_ARGS", None)
    rep = read_json(report)
    if rep is None:
        tail = log.read_text(errors="replace").splitlines()[-25:] if log.exists() else []
        raise RuntimeError("UE shot render wrote no report:\n" + "\n".join(tail))
    rep["log_errors"] = log_errors(log)
    store.log_run({"stage": "ue.render", "ok": rep["ok"], "shots": len(rep["shots"]),
                   "seconds": rep.get("seconds")})
    return rep


BACKUP_EXCLUDE = ["Intermediate/", "Saved/", "DerivedDataCache/"]


def editor_running(up: Path) -> bool:
    """Is an Unreal Editor open on this .uproject? (checked by its command line)"""
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                cmd = (d / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if cmd and b"UnrealEditor" in cmd[0] and str(up).encode() in cmd:
                return True
    return False


def backups(store: ProjectStore) -> list[Path]:
    """Snapshots of the project, oldest first."""
    d = backups_dir() / store.load().slug
    return sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink()) if d.is_dir() else []


def backup_log(store: ProjectStore) -> Path:
    return work_dir(store) / "backup.log"


def backup(store: ProjectStore, keep: int | None = None, force: bool = False) -> dict:
    """Snapshot the UE project to <backup_dir>/<slug>/<YYYYmmdd-HHMMSS>/, without
    Intermediate, Saved and DerivedDataCache (rebuilt by the editor). Unchanged files are
    hard links into the previous snapshot (rsync --link-dest), so each copy costs only what
    changed. Keeps the newest `keep` snapshots; `latest` points at the newest."""
    keep = keep or int(_setting("AP_UE_BACKUP_KEEP", "backup_keep", 10))
    if keep < 1:
        raise ValueError("keep must be at least 1")
    up = uproject(store)
    if not up.is_file():
        raise FileNotFoundError(f"no UE project at {up}")
    if editor_running(up) and not force:
        raise RuntimeError(f"an Unreal Editor has {up.name} open: save and close it first (or force; a "
                           f"snapshot taken mid-save can be inconsistent)")
    root = backups_dir() / store.load().slug
    root.mkdir(parents=True, exist_ok=True)
    previous = backups(store)
    dest = root / time.strftime("%Y%m%d-%H%M%S")
    if dest.exists():
        raise FileExistsError(f"{dest} exists (two backups within a second)")
    t = time.time()
    tmp = dest.with_name(dest.name + ".partial")
    # --checksum: rsync's default size + mtime test would hard-link the previous snapshot's
    # copy of a file re-saved within the same second at the same size (caught by a test)
    # --info=progress2 without incremental recursion: the log's "to-chk=left/total" counts
    # files against the whole tree (the UI's backup dialog reads it)
    cmd = ["rsync", "-a", "--checksum", "--delete", "--info=progress2", "--no-inc-recursive"] + \
          [f"--exclude=/{x}" for x in BACKUP_EXCLUDE]
    if previous:
        cmd.append(f"--link-dest={previous[-1]}")
    cmd += [f"{up.parent}/", f"{tmp}/"]
    try:
        blender.run_logged(cmd, backup_log(store), "UE backup")
    except BaseException:                  # failed or canceled: no partial snapshot left
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    tmp.rename(dest)                       # a snapshot is complete or absent, never partial
    latest = root / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(dest.name)
    removed = []
    for old in backups(store)[:-keep]:
        shutil.rmtree(old)
        removed.append(old.name)
    files = sum(1 for p in dest.rglob("*") if p.is_file())
    new_bytes = sum(p.stat().st_size for p in dest.rglob("*") if p.is_file() and p.stat().st_nlink == 1)
    rep = {"snapshot": str(dest), "files": files, "new_mb": round(new_bytes / 2**20, 1),
           "kept": [p.name for p in backups(store)], "removed": removed, "seconds": round(time.time() - t, 1)}
    store.log_run({"stage": "ue.backup", **{k: rep[k] for k in ("snapshot", "files", "new_mb", "seconds")}})
    return rep


def pull_layout(store: ProjectStore) -> dict:
    """Read the AP actors' transforms back from the UE level (the layout's source of truth
    after import) into site/ue_layout.json, in both UE and Blender coordinates."""
    man = s7_export.load_manifest(store)
    rep = run_script(store, "ap_pull_layout.py", {"map": man["map"]}, "UE layout read-back")
    if "error" in rep:
        raise RuntimeError(f"UE layout read-back failed: {rep['error']}")
    for a in rep["actors"]:
        a["blender_matrix"] = [[round(v, 6) for v in row] for row in ue_coords.from_ue(a["transform"]).tolist()]
    write_json(sw_site.site_dir(store) / "ue_layout.json", rep)
    return rep
