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
from PIL import Image

from . import blender, config, ue_coords
from . import gpu as gpu_mod
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
    rep = _render(store, man["map"], [{"id": s["id"], "resolution": s["resolution"]} for s in want],
                  work_dir(store) / "shots", "ap_shot_render", timeout_s)
    store.log_run({"stage": "ue.render", "ok": rep["ok"], "shots": len(rep["shots"]),
                   "seconds": rep.get("seconds")})
    return rep


VRAM_LIMIT_GB = 4.0       # more than this held on a GPU by others (Blender, ComfyUI, the VLM): don't render there


def gpu_vram() -> dict[int, float]:
    """VRAM in use per GPU (GB), via gpu.vram_used_gb. On AMD, card N = GPU N = Vulkan adapter N.
    On NVIDIA the Vulkan adapter index usually, but not always, matches the CUDA index (untested)."""
    return gpu_mod.vram_used_gb()


def pick_gpu(preferred: int, limit_gb: float = VRAM_LIMIT_GB, wait_s: float = 1800.0, log=print) -> int:
    """The GPU to render on: `preferred` unless others hold more than limit_gb there, then the
    other one; if both are busy, wait (polling every 30 s) up to wait_s, then refuse. Every
    Unreal render goes through this (the 2026-10-14 device-lost crashes came from a Blender
    session sharing the GPU)."""
    t0 = time.time()
    while True:
        used = gpu_vram()
        for g in (preferred, 1 - preferred):
            if used.get(g, 99.0) <= limit_gb:
                log(f"rendering on GPU {g} (VRAM in use: " + ", ".join(f"GPU {k} {v:.1f} GB" for k, v in sorted(used.items())) + ")")
                return g
        if time.time() - t0 > wait_s:
            raise RuntimeError("both GPUs hold more than %.0f GB of other work: " % limit_gb
                               + ", ".join(f"GPU {k} {v:.1f} GB" for k, v in sorted(used.items())))
        log("both GPUs busy (" + ", ".join(f"GPU {k} {v:.1f} GB" for k, v in sorted(used.items())) + "); waiting")
        time.sleep(30)


def _render(store: ProjectStore, map_: str, shots: list[dict], out: Path, name: str, timeout_s: int,
            adapter: int = 0) -> dict:
    """Capture the tagged cameras of a level with the full editor offscreen (ap_shot_render.py),
    on Vulkan adapter `adapter` (0 = GPU 0; 1 = GPU 1, ComfyUI's: only while it's down), or the
    other one when pick_gpu finds `adapter` busy. The report says which ("adapter")."""
    adapter = pick_gpu(adapter)
    up = init(store)
    work = work_dir(store)
    report = work / f"{name}_report.json"
    report.unlink(missing_ok=True)
    args_file = work / f"{name}_args.json"
    write_json(args_file, {"map": map_, "out_dir": str(out), "report": str(report), "timeout_s": timeout_s,
                           "shots": shots})
    editor = editor_cmd().with_name("UnrealEditor")
    cmd = [str(editor), str(up), "-RenderOffscreen", f"-graphicsadapter={adapter}", "-unattended", "-nosplash", "-NoTraceServer",
           # -ExecutePythonScript would quit the editor right after the script returns (before
           # its tick callbacks run); a console `py` command leaves the quitting to the script
           # textures build synchronously on load: asynchronously, freshly imported ones render as
           # the grey placeholder checker (and a tint read off their lowest mip goes wrong)
           "-NoSound", "-dpcvars=Editor.AsyncTextureCompilation=0",
           f"-ExecCmds=py {SCRIPTS / 'ap_shot_render.py'}", "-stdout",
           "-FullStdOutLogOutput", "-NoLogTimes"]
    os.environ["AP_UE_ARGS"] = str(args_file)
    log = work / f"{name}.log"
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
    rep["adapter"] = adapter
    return rep


PREVIEW_MAP = "/Game/AP/Maps/MaterialPreview"
# scripts/blender_render_asset.py's Cycles look: sun 3.2 W/m2 at 40 deg elevation (rotation_euler
# x 50, z -40 deg: light travelling along (0.49, 0.59, -0.64)), Standard view at exposure -0.15, flat sky 0.55 x (0.55, 0.62, 0.72).
# Unreal: lux = W/m2 x 1.2 at EV100 0 (exposure 1 / 1.2); yaw mirrored for its Y axis.
BLENDER_LOOK = {"sun_lux": 3.2 * 1.2, "sun_elevation": 40.0, "sun_yaw": -50.0, "exposure_bias": -0.15,
                "sky_intensity": 1.0}


def library_slots(job: Path) -> dict:
    """The Unreal material spec per slot of a library build (its blender_kit job.json): surfaces
    with the scan the build used (full-resolution shared maps), tint and contrast as parameters;
    plain materials as they are."""
    from . import material_lib
    slots = {}
    for name, m in read_json(job)["materials"].items():
        if m.get("uv") == "plain":
            slots[name] = {"plain": {"rgb_lin": m["color"], "rough": m["rough"], "metal": m.get("metal", 0.0)}}
        elif m.get("type"):
            src = material_lib.fetch(m["type"], acg=m.get("acg"))
            lib = material_lib.LIBRARY[m["type"]]
            slots[name] = {"surface": {
                "type": m["type"], "rgb_lin": [round(float(v), 5) for v in material_lib._srgb_to_lin(np.array(m["colour"], float))],
                "acg_id": src["acg"], "textures": {k: src[k] for k in ("color", "normal_dx", "rough")},
                "megascans": lib["megascans"], "uv_scale": 1.0, "contrast": m.get("contrast", 1.0),
                "uv_scale_megascans": src["tile_m"][0] / lib.get("megascans_m", 2.0)}}
    return slots


def material_preview(store: ProjectStore, asset: str, glb: Path, job: Path, out: Path,
                     size=(900, 640), timeout_s: int = 3600) -> dict:
    """One library building in Unreal with its library materials (Megascans when the project has
    the type's surface, else the ambientCG maps), rendered from the Blender review render's
    three-quarter view to out/threeq.png. job: the build's blender_kit job.json (its materials)."""
    from . import material_lib, vlm
    if editor_running(uproject(store)):
        raise RuntimeError("the Unreal editor has this project open: close it first")
    if list((config.ap_root() / "logs").glob("trellis-gpu0-*.lock")):
        raise RuntimeError("GPU 0 is busy with TRELLIS; try again when it's done")
    slots = library_slots(job)
    paving = material_lib.fetch("concrete")
    ground = {"type": "concrete", "rgb_lin": [0.42, 0.40, 0.37], "acg_id": paving["acg"], "tile_m": paving["tile_m"],
              "textures": {k: paving[k] for k in ("color", "normal_dx", "rough")}, "megascans": [],
              "uv_aspect": paving["tile_m"][0] / paving["tile_m"][1], "contrast": 0.6}
    shot = f"preview_{asset}"
    mask = work_dir(store) / "T_AP_White_M.png"
    mask.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (255, 255, 255)).save(mask)
    rep = run_script(store, "ap_material_preview.py", {
        "asset": asset, "glb": str(glb), "map": PREVIEW_MAP, "shot": shot, "slots": slots, "mask_default": str(mask), "ground": ground,
        "look": BLENDER_LOOK,
        "camera": {"az": -35, "el": 0.35, "dist_factor": 1.55, "focal_mm": 50, "sensor_mm": [36, 36 * size[1] / size[0]]}},
        f"UE preview {asset}")
    if "error" in rep:
        raise RuntimeError(f"UE preview failed: {rep['error']}\n{rep.get('traceback', '')}")
    bad = [ln for ln in (work_dir(store) / "ap_material_preview.log").read_text(errors="replace").splitlines()
           if "Failed to compile Material" in ln and "/AP/" in ln]
    if bad:          # it would render as the WorldGrid checker
        raise RuntimeError("UE preview: a material failed to compile:\n" + "\n".join(bad[:5]))
    vlm.down(gpu=0)
    ren = _render(store, PREVIEW_MAP, [{"id": shot, "resolution": list(size)}], out, "ap_preview_render", timeout_s)
    src = out / f"{shot}.png"
    if src.is_file():
        src.replace(out / "threeq.png")
    rep["render"] = ren
    write_json(out / "ue_preview.json", rep)
    store.log_run({"stage": "ue.material_preview", "asset": asset, "ok": rep["ok"] and ren["ok"]})
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


def _blender_to_ue_xf(m) -> dict:
    t = ue_coords.to_ue(m)
    return {"loc": t["loc"], "rot": t["rot"]}


def look_test(store: ProjectStore, asset: str, shot: str, slot: str, bbox, sun_offset_deg: float = 30.0,
              sun_elevation_deg: float = 20.0, fog_density: float = 0.02, timeout_s: int = 3600) -> dict:
    """A library building in its place in Unreal, in a level of its own (LookTest_<asset>): the
    greybox around it (scripts/blender_look_context.py), its library materials, the shot's
    camera, a sun sun_offset_deg right of the view at sun_elevation_deg (read off the frame:
    the shot data has no sun), sky, clouds, haze; two close cameras on the building's wing
    junction and copings. Renders to export/look/<asset>/."""
    from . import kit_proc
    gb = sw_site.load_greybox(store)
    s = next(x for x in gb["shots"] if x["id"] == shot)
    look = s7_export.export_dir(store) / "look"
    look.mkdir(parents=True, exist_ok=True)
    job = {"slot": slot, "piece": "insula:mass", "camera": s["matrix"], "lens_mm": s["lens_mm"], "sensor_mm": s["sensor_mm"],
           "resolution": s["resolution"], "bbox": list(bbox), "radius": 600,
           "out_context": str(look / "context.glb"), "out_terrain": str(look / "terrain.glb")}
    write_json(look / "context_job.json", job)
    lg = look / "context.log"
    blender.run(config.REPO_ROOT / "scripts/blender_look_context.py", [look / "context_job.json"], lg, "look context",
                blend=store.root / "site/greybox.blend")
    line = next(ln for ln in lg.read_text().splitlines() if ln.startswith("REPORT "))
    ctx = json.loads(line[7:])
    pl = ctx["placement"]
    yaw = np.radians(pl["yaw_deg"])
    B = np.eye(4)
    B[:2, :2] = [[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]]
    B[:3, 3] = pl["loc"]
    C = np.array(s["matrix"])
    w, h = s["resolution"]
    cams = [{"id": f"look_{shot}", "transform": _blender_to_ue_xf_cam(C), "focal_mm": s["lens_mm"],
             "sensor_mm": [s["sensor_mm"], round(s["sensor_mm"] * h / w, 4)], "resolution": [w, h]}]
    for cid, az, el, tgt, dist, lens in (("look_junction", -60, 0.15, (-6, -18, 10), 20, 35),
                                         ("look_coping", -35, 0.6, (0, -4, 20), 26, 50)):
        a = np.radians(az)
        d = np.array([np.sin(a), -np.cos(a), el])
        d /= np.linalg.norm(d)
        tw = (B @ np.array([*tgt, 1.0]))[:3]
        pw = (B @ np.array([*(np.array(tgt) + d * dist), 1.0]))[:3]
        cams.append({"id": cid, "transform": _blender_to_ue_xf_cam(_look_at(pw, tw)), "focal_mm": lens,
                     "sensor_mm": [36.0, 36.0 * 640 / 900], "resolution": [900, 640]})
    # the sun: ahead of the camera and to its right (the frame is backlit), above the horizon
    fwd, right = -C[:3, 2], C[:3, 0]
    fwd[2], right[2] = 0, 0
    fwd, right = fwd / np.linalg.norm(fwd), right / np.linalg.norm(right)
    o = np.radians(sun_offset_deg)
    to_sun = fwd * np.cos(o) + right * np.sin(o)
    travel = -to_sun                                    # horizontal direction the light travels, Blender
    sun = {"yaw": float(np.degrees(np.arctan2(-travel[1], travel[0]))), "pitch": -sun_elevation_deg}
    d = kit_proc.out_dir(store, asset)
    mask = work_dir(store) / "T_AP_White_M.png"
    mask.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (255, 255, 255)).save(mask)
    if editor_running(uproject(store)):
        raise RuntimeError("the Unreal editor has this project open: close it first")
    mp = f"/Game/AP/Maps/LookTest_{s7_export.pascal(asset)}"
    rep = run_script(store, "ap_look_test.py", {
        "asset": asset, "glb": str(d / "procedural.glb"), "context_glb": job["out_context"], "terrain_glb": job["out_terrain"],
        "map": mp, "slots": library_slots(d / "procedural" / "job.json"), "mask_default": str(mask),
        "building": _blender_to_ue_xf(B), "cameras": cams, "sun": sun, "fog_density": fog_density}, f"UE look test {asset}")
    if "error" in rep:
        raise RuntimeError(f"UE look test failed: {rep['error']}\n{rep.get('traceback', '')}")
    bad = [ln for ln in (work_dir(store) / "ap_look_test.log").read_text(errors="replace").splitlines()
           if "Failed to compile Material" in ln and "/AP/" in ln]
    if bad:
        raise RuntimeError("UE look test: a material failed to compile:\n" + "\n".join(bad[:5]))
    from . import vlm
    vlm.down(gpu=0)
    out = look / asset
    rep["render"] = _render(store, mp, [{"id": c["id"], "resolution": c["resolution"]} for c in cams], out,
                            "ap_look_render", timeout_s)
    rep |= {"context": ctx, "sun": sun, "cameras": cams, "map": mp}
    write_json(out / "look_test.json", rep)
    store.log_run({"stage": "ue.look_test", "asset": asset, "shot": shot, "ok": rep["ok"] and rep["render"]["ok"]})
    return rep


def _look_at(pos, tgt) -> np.ndarray:
    """A Blender camera matrix at pos looking at tgt (camera -Z forward, +Y up)."""
    f = np.asarray(tgt, float) - np.asarray(pos, float)
    f /= np.linalg.norm(f)
    r = np.cross(f, [0, 0, 1.0])
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = r, u, -f, pos
    return m


def _blender_to_ue_xf_cam(m) -> dict:
    t = ue_coords.camera_to_ue(np.asarray(m).tolist())
    return {"loc": t["loc"], "rot": t["rot"]}


def look_scene(store: ProjectStore, name: str, shot: str, placements: list[dict], entries: dict, sun: dict,
               fog: dict, backdrop: dict | None = None, drop_slots: list[str] | None = None,
               exposure: dict | None = None, radius: float = 600.0, timeout_s: int = 3600,
               sky: dict | None = None, white_balance: float | None = None, ground: dict | None = None,
               adapter: int = 0, greybox_rgb: list[float] | None = None, held: list[dict] | None = None,
               held_slots: list[str] | None = None) -> dict:
    """A shot rebuilt in Unreal with library builds where the greybox stood (a level of its own,
    LookTest_<Name>): placements [{key, entry, glb, x, y, z, yaw, w, d}] in Blender world metres,
    entries {entry: job.json} for their materials, the rest of the greybox within radius as
    context, the terrain and an optional backdrop ridge. sun: {elevation, offset (deg right of
    the view), temperature, lux}; fog: {density, falloff, start_cm, color}. exposure: {"box":
    [x0, y0, x1, y1] (frame pixels), "frame": png}: manual exposure set so the render's mean
    there matches the frame's (one corrective render). sky: {intensity, color} of the sky light;
    white_balance: the camera's white point (K); ground: {kind: material spec} for paving, water,
    lawn and gravel (look.ground: generated maps, world-aligned). adapter: the Vulkan adapter to
    render on (look.run picks it). Built and driven by `ap ue look` (look.run). Renders to
    export/look/<name>/."""
    from . import vlm
    gb = sw_site.load_greybox(store)
    s = next(x for x in gb["shots"] if x["id"] == shot)
    look = s7_export.export_dir(store) / "look" / name
    look.mkdir(parents=True, exist_ok=True)
    job = {"camera": s["matrix"], "lens_mm": s["lens_mm"], "sensor_mm": s["sensor_mm"], "resolution": s["resolution"],
           "radius": radius, "out_context": str(look / "context.glb"), "out_terrain": str(look / "terrain.glb"),
           "drop": [{k: p[k] for k in ("x", "y", "w", "d", "yaw")} for p in placements], "drop_slots": drop_slots or [],
           "hold": [{k: p[k] for k in ("x", "y", "w", "d", "yaw")} for p in held or []], "hold_slots": held_slots or [],
           "out_held": str(look / "held.glb")}
    (look / "held.glb").unlink(missing_ok=True)
    if backdrop:
        job["backdrop"] = backdrop | {"out": str(look / "backdrop.glb")}
    write_json(look / "context_job.json", job)
    lg = look / "context.log"
    blender.run(config.REPO_ROOT / "scripts/blender_look_context.py", [look / "context_job.json"], lg, "look context",
                blend=store.root / "site/greybox.blend")
    ctx = json.loads(next(ln for ln in lg.read_text().splitlines() if ln.startswith("REPORT "))[7:])
    pls = []
    for p in placements:
        yaw = np.radians(p["yaw"])
        B = np.eye(4)
        B[:2, :2] = [[np.cos(yaw), -np.sin(yaw)], [np.sin(yaw), np.cos(yaw)]]
        B[:3, 3] = [p["x"], p["y"], p["z"]]
        pls.append({"key": p["key"], "entry": p["entry"], "glb": str(store.root / p["glb"]), **_blender_to_ue_xf(B)})
    C = np.array(s["matrix"])
    w, h = s["resolution"]
    cam = {"id": f"look_{name}", "transform": _blender_to_ue_xf_cam(C), "focal_mm": s["lens_mm"],
           "sensor_mm": [s["sensor_mm"], round(s["sensor_mm"] * h / w, 4)], "resolution": [w, h]}
    fwd, right = -C[:3, 2].copy(), C[:3, 0].copy()
    fwd[2] = right[2] = 0
    fwd, right = fwd / np.linalg.norm(fwd), right / np.linalg.norm(right)
    o = np.radians(sun["offset"])
    travel = -(fwd * np.cos(o) + right * np.sin(o))
    sun_ue = {"yaw": float(np.degrees(np.arctan2(-travel[1], travel[0]))), "pitch": -float(sun["elevation"]),
              "temperature": sun.get("temperature"), "lux": sun.get("lux")}
    mask = work_dir(store) / "T_AP_White_M.png"
    mask.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (255, 255, 255)).save(mask)
    if editor_running(uproject(store)):
        raise RuntimeError("the Unreal editor has this project open: close it first")
    mp = f"/Game/AP/Maps/LookTest_{s7_export.pascal(name)}"
    args = {"asset": name, "context_glb": job["out_context"], "terrain_glb": job["out_terrain"],
            "held_glb": str(look / "held.glb") if (look / "held.glb").exists() else None,
            "backdrop_glb": job.get("backdrop", {}).get("out"), "map": mp, "placements": pls,
            "entries": {e: library_slots(store.root / j) for e, j in entries.items()}, "mask_default": str(mask),
            "cameras": [cam], "sun": sun_ue, "fog": fog, "exposure_bias": 0.0, "sky": sky,
            "white_balance": white_balance, "ground": ground, "greybox_rgb": greybox_rgb}
    rep, bias, steps = None, 0.0, []
    for attempt in range(int(exposure.get("iterations", 2)) if exposure else 1):
        args["exposure_bias"] = bias
        rep = run_script(store, "ap_look_test.py", args, f"UE look scene {name}")
        if "error" in rep:
            raise RuntimeError(f"UE look scene failed: {rep['error']}\n{rep.get('traceback', '')}")
        bad = [ln for ln in (work_dir(store) / "ap_look_test.log").read_text(errors="replace").splitlines()
               if "Failed to compile Material" in ln and "/AP/" in ln]
        if bad:
            raise RuntimeError("UE look scene: a material failed to compile:\n" + "\n".join(bad[:5]))
        vlm.down(gpu=0)
        rep["render"] = _render(store, mp, [{"id": cam["id"], "resolution": cam["resolution"]}], look, "ap_look_render",
                                timeout_s, adapter=adapter)
        if not exposure:
            break
        x0, y0, x1, y1 = exposure["box"]
        def lum(im):        # mean linear luminance of the paving box
            a = np.asarray(im.convert("RGB").resize((w, h)), float)[y0:y1, x0:x1] / 255.0
            return float(((a ** 2.2) @ [0.2126, 0.7152, 0.0722]).mean())
        got = lum(Image.open(look / f"{cam['id']}.png"))
        want = lum(Image.open(store.root / exposure["frame"]))
        steps.append({"bias": round(bias, 3), "render": round(got, 4), "frame": round(want, 4)})
        rep["exposure"] = steps
        bias += float(np.log2(max(want, 1e-4) / max(got, 1e-4)))
    rep |= {"context": ctx, "sun": sun_ue, "camera": cam, "map": mp}
    write_json(look / "look_scene.json", rep)
    store.log_run({"stage": "ue.look_scene", "name": name, "shot": shot, "ok": rep["ok"] and rep["render"]["ok"]})
    return rep

