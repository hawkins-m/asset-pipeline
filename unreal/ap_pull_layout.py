"""Read the asset pipeline's actors back from a UE level (editor Python, UE 5.8).

args.json: {"map": "/Game/AP/Maps/<Name>", "report": ".../ap_pull_layout_report.json"}
Writes every actor tagged ap:id / ap:shot / ap:env with its label, tags, folder and
transform ({"loc" cm, "rot" [roll, pitch, yaw], "scale"}); cameras add their lens.
"""
import json
import sys
import traceback

import unreal


def transform(a):
    t = a.get_actor_transform()
    r = t.rotation.rotator()
    return {"loc": [t.translation.x, t.translation.y, t.translation.z], "rot": [r.roll, r.pitch, r.yaw],
            "scale": [t.scale3d.x, t.scale3d.y, t.scale3d.z]}


def main(args):
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if not les.load_level(args["map"]):
        raise RuntimeError(f"can't load {args['map']}")
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    out = []
    for a in eas.get_all_level_actors():
        tags = {k: v for k, _, v in (str(t).partition("=") for t in a.tags) if k.startswith("ap:")}
        if not ({"ap:id", "ap:shot", "ap:env"} & set(tags)):
            continue
        row = {"label": a.get_actor_label(), "tags": tags,
               "other_tags": [str(t) for t in a.tags if not str(t).startswith("ap:")],
               "folder": str(a.get_folder_path()),
               "transform": transform(a)}
        if isinstance(a, unreal.CineCameraActor):
            cc = a.get_cine_camera_component()
            row["focal_length_mm"] = cc.get_editor_property("current_focal_length")
        out.append(row)
    return {"ok": True, "map": args["map"], "actors": sorted(out, key=lambda r: r["label"])}


if __name__ == "__main__":
    args = json.load(open(sys.argv[-1]))
    try:
        rep = main(args)
    except Exception as e:
        rep = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
    with open(args["report"], "w") as f:
        json.dump(rep, f, indent=1)
