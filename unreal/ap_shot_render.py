"""Render the shot cameras of a world-mode level to PNGs (editor Python, UE 5.8, full editor
with -RenderOffscreen; started by `ap ue render`).

Args come from the AP_UE_ARGS environment variable (a JSON file path):
    {"map": "/Game/AP/Maps/<Name>", "shots": [{"id", "resolution": [w, h]}],
     "out_dir": ".../export/ue/shots", "report": ".../ap_shot_render_report.json",
     "warmup_s": 20, "settle_s": 15, "timeout_s": 3600}

Shaders compile asynchronously after the level loads: until the ShaderCompileWorker
processes have been idle for `settle_s`, a screenshot would show grey default materials.
Then each AP_Shot_<id> camera is captured with AutomationLibrary.take_high_res_screenshot.
"""
import json
import os
import subprocess
import time
import traceback

import unreal

args = json.load(open(os.environ["AP_UE_ARGS"]))
state = {"t0": time.time(), "quiet_since": None, "task": None, "current": None, "busy": False,
         "pending": list(args["shots"]), "done": [], "failed": [], "phase": "load"}
report = {"ok": False, "shots": state["done"], "failed": state["failed"]}


def log(msg):
    unreal.log(f"AP_RENDER {msg}")


def finish(error=None):
    unreal.unregister_slate_post_tick_callback(handle)
    report["ok"] = error is None and not state["failed"]
    report["seconds"] = round(time.time() - state["t0"], 1)
    if error:
        report["error"] = error
    with open(args["report"], "w") as f:
        json.dump(report, f, indent=1)
    log(f"done ok={report['ok']}")
    unreal.SystemLibrary.quit_editor()


def compiling():
    r = subprocess.run(["pgrep", "-c", "-f", "ShaderCompileWorker"], capture_output=True, text=True)
    return int((r.stdout or "0").strip() or 0) > 0


def cameras():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    out = {}
    for a in eas.get_all_level_actors():
        for t in a.tags:
            k, _, v = str(t).partition("=")
            if k == "ap:shot":
                out[v] = a
    return out


def tick(dt):
    # take_high_res_screenshot ticks Slate itself, which re-enters this callback: without
    # the guard the nested calls pop every remaining shot and finish before any capture
    if state["busy"]:
        return
    state["busy"] = True
    try:
        step()
    except Exception:
        finish(traceback.format_exc())
    finally:
        state["busy"] = False


def step():
    """One step of the state machine: load, wait for shaders, shoot each camera."""
    now = time.time()
    if now - state["t0"] > args.get("timeout_s", 3600):
        return finish(f"timed out in phase {state['phase']}")
    if state["phase"] == "load":
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        if not les.load_level(args["map"]):
            return finish(f"can't load {args['map']}")
        state["cams"] = cameras()
        state["phase"], state["loaded"] = "warmup", now
        log("level loaded")
        return
    if state["phase"] == "warmup":
        if now - state["loaded"] < args.get("warmup_s", 20):
            return
        if compiling():
            state["quiet_since"] = None
            return
        state["quiet_since"] = state["quiet_since"] or now
        if now - state["quiet_since"] < args.get("settle_s", 15):
            return
        report["shader_wait_s"] = round(now - state["loaded"], 1)
        state["phase"] = "shoot"
        log(f"shaders settled after {report['shader_wait_s']} s")
        return
    task = state["task"]
    if task is not None and not task.is_task_done():
        return
    if state["current"]:
        shot, path = state["current"]
        (state["done"] if os.path.isfile(path) else state["failed"]).append({"id": shot, "file": path})
        state["current"] = None
    if not state["pending"]:
        return finish()
    shot = state["pending"].pop(0)
    cam = state["cams"].get(shot["id"])
    if cam is None:
        state["failed"].append({"id": shot["id"], "error": "no camera"})
        return
    w, h = shot["resolution"]
    path = os.path.join(args["out_dir"], f"{shot['id']}.png")
    if os.path.exists(path):
        os.remove(path)
    state["current"] = (shot["id"], path)
    log(f"shooting {shot['id']}")
    state["task"] = unreal.AutomationLibrary.take_high_res_screenshot(
        w, h, path, camera=cam, capture_hdr=False, delay=1.0, force_game_view=True)


os.makedirs(args["out_dir"], exist_ok=True)
handle = unreal.register_slate_post_tick_callback(tick)
