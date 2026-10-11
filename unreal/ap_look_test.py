"""Look test of one library building in its place (editor Python, UE 5.8; run headless by
`ue.look_test`, rendered afterwards by ap_shot_render.py). Builds its own level and never touches
the city level.

args.json:
    {"asset", "glb", "context_glb", "terrain_glb", "map": "/Game/AP/Maps/LookTest_<asset>",
     "slots": {...as ap_material_preview...}, "mask_default": png,
     "building": {"loc": [cm], "rot": [roll, pitch, yaw]},
     "cameras": [{"id", "transform": {"loc", "rot"}, "focal_mm", "sensor_mm": [w, h]}],
     "sun": {"pitch", "yaw"}, "fog_density", "report"}
The context (greybox neighbours) and terrain come in world coordinates, Nanite on, with plain
materials per kind (building, green, water, ground).
"""
import json
import os
import re
import sys
import traceback

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ap_import  # noqa: E402
import ap_material_preview as amp  # noqa: E402

eal = unreal.EditorAssetLibrary
mel = unreal.MaterialEditingLibrary
log = ap_import.log
LOOK = "/Game/AP/LookTest"
KIND_RGB = {"building": [0.62, 0.6, 0.57], "green": [0.12, 0.2, 0.08], "water": [0.03, 0.08, 0.09],
            "ground": [0.32, 0.29, 0.24], "paving": [0.62, 0.6, 0.55]}
KIND_ROUGH = {"water": 0.03, "paving": 0.45}


def import_static(glb, folder, name):
    """A GLB as one Nanite static mesh at folder/SM_<name> (kept when the GLB is unchanged)."""
    target = f"{folder}/SM_{name}"
    digest = ap_import.sha(glb)
    if eal.does_asset_exist(target):
        old = eal.load_asset(target)
        if eal.get_metadata_tag(old, "ap:glb_sha") == digest:
            return old
    tmp = f"{folder}/_import_{name}"
    if eal.does_directory_exist(tmp):
        eal.delete_directory(tmp)
    task = unreal.AssetImportTask()
    task.filename, task.destination_path = glb, tmp
    task.automated, task.replace_existing, task.save = True, True, False
    amp.tools.import_asset_tasks([task])
    mesh = next((unreal.load_object(None, p) for p in task.imported_object_paths
                 if isinstance(unreal.load_object(None, p), unreal.StaticMesh)), None)
    if mesh is None:
        raise RuntimeError(f"no static mesh in {glb}")
    target = f"{folder}/SM_{name}"
    if eal.does_asset_exist(target):
        eal.consolidate_assets(mesh, [eal.load_asset(target)])
        if eal.does_asset_exist(target):
            eal.delete_asset(target)
    eal.rename_asset(mesh.get_path_name().split(".")[0], target)
    for p in eal.list_assets(tmp, recursive=True):
        if eal.find_asset_data(p).asset_class_path.asset_name == "ObjectRedirector":
            eal.delete_asset(p)
    eal.delete_directory(tmp)
    mesh = eal.load_asset(target)
    ns = mesh.get_editor_property("nanite_settings")
    ns.enabled = True
    mesh.set_editor_property("nanite_settings", ns)
    eal.set_metadata_tag(mesh, "ap:glb_sha", digest)
    return mesh


def kind_material(kind, pbr):
    path = f"{LOOK}/Materials/MI_look_{kind}"
    folder, name = path.rsplit("/", 1)
    mi = eal.load_asset(path) if eal.does_asset_exist(path) else amp.tools.create_asset(
        name, folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    mel.set_material_instance_parent(mi, pbr)
    mel.set_material_instance_vector_parameter_value(mi, "Tint", unreal.LinearColor(*KIND_RGB[kind], 1.0))
    mel.set_material_instance_scalar_parameter_value(mi, "Roughness", KIND_ROUGH.get(kind, 0.85))
    mel.update_material_instance(mi)
    eal.save_loaded_asset(mi)
    return mi


def spawn(eas, cls, loc, rot, label, tags):
    a = eas.spawn_actor_from_class(cls, loc, rot)
    a.set_actor_label(label)
    ap_import.set_tags(a, tags + ["ap:source=looktest"])
    return a


def main(args):
    report = {"ok": False, "slots": {}, "unmatched_slots": []}
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if eal.does_asset_exist(args["map"]):
        les.load_level(args["map"])
        for a in eas.get_all_level_actors():
            if ap_import.tagged(a).get("ap:source") == "looktest":
                eas.destroy_actor(a)
    elif not les.new_level(args["map"]):
        raise RuntimeError(f"can't create level {args['map']}")
    asset = args["asset"]
    surface, pbr = amp.surface_material(amp.default_mask(args["mask_default"])), ap_import.master_material()
    placements = args.get("placements") or [{"key": asset, "entry": asset, "glb": args["glb"], **args["building"]}]
    entries = args.get("entries") or {asset: args["slots"]}
    mis = {}
    for pl in placements:                   # library builds, their materials shared per entry
        e = pl["entry"]
        mesh = import_static(pl["glb"], f"{LOOK}/{asset}/builds", pl["key"])
        for i, sm in enumerate(mesh.get_editor_property("static_materials")):
            slot = re.sub(r"(_\d+|\.\d+)$", "", str(sm.get_editor_property("material_slot_name")))
            spec = entries[e].get(slot)
            if spec is None:
                report["unmatched_slots"].append(f"{e}:{slot}")
                continue
            if (e, slot) not in mis:
                mis[(e, slot)] = amp.slot_instance(e, slot, spec, surface, pbr, report)
            mesh.set_material(i, mis[(e, slot)])
        eal.save_loaded_asset(mesh)
        bld = spawn(eas, unreal.StaticMeshActor, unreal.Vector(*pl["loc"]), unreal.Rotator(*pl["rot"]),
                    f"AP_Look_{pl['key']}", [f"ap:look={pl['key']}"])
        bld.static_mesh_component.set_static_mesh(mesh)
    report["placed"] = len(placements)
    # the greybox around it and the terrain, in world coordinates
    folder = f"{LOOK}/{asset}"
    for name in ("context", "terrain", "backdrop"):
        if not args.get(f"{name}_glb"):
            continue
        m = import_static(args[f"{name}_glb"], folder, name)
        for i, sm in enumerate(m.get_editor_property("static_materials")):
            k = re.sub(r"(_\d+|\.\d+)$", "", str(sm.get_editor_property("material_slot_name")))
            m.set_material(i, kind_material(k if k in KIND_RGB else "building", pbr))
        eal.save_loaded_asset(m)
        a = spawn(eas, unreal.StaticMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), f"AP_Look_{name}", [])
        a.static_mesh_component.set_static_mesh(m)
    # light: sun at the frame's direction, sky, clouds, haze
    s = args["sun"]
    sun = spawn(eas, unreal.DirectionalLight, unreal.Vector(0, 0, 50000), unreal.Rotator(0, s["pitch"], s["yaw"]),
                "AP_Look_sun", [])
    sun.light_component.set_editor_property("atmosphere_sun_light", True)
    sun.light_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    if s.get("lux"):
        sun.light_component.set_intensity(s["lux"])
    if s.get("temperature"):
        sun.light_component.set_editor_property("use_temperature", True)
        sun.light_component.set_editor_property("temperature", s["temperature"])
    sky = spawn(eas, unreal.SkyLight, unreal.Vector(0, 0, 50000), unreal.Rotator(0, 0, 0), "AP_Look_skylight", [])
    sky.light_component.set_editor_property("real_time_capture", True)
    sky.light_component.set_mobility(unreal.ComponentMobility.MOVABLE)
    spawn(eas, unreal.SkyAtmosphere, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), "AP_Look_atmosphere", [])
    fog = spawn(eas, unreal.ExponentialHeightFog, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), "AP_Look_fog", [])
    fg = args.get("fog") or {"density": args.get("fog_density", 0.02)}
    fc = fog.component
    fc.set_editor_property("fog_density", fg["density"])
    for k, prop in (("falloff", "fog_height_falloff"), ("start_cm", "start_distance")):
        if k in fg:
            fc.set_editor_property(prop, fg[k])
    if "color" in fg:
        for prop in ("fog_inscattering_luminance", "fog_inscattering_color"):
            try:
                fc.set_editor_property(prop, unreal.LinearColor(*fg["color"], 1.0))
                break
            except Exception:
                continue
    if args.get("exposure_bias") is not None:     # manual exposure, set on the paving by the caller
        ppv = spawn(eas, unreal.PostProcessVolume, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), "AP_Look_exposure", [])
        ppv.set_editor_property("unbound", True)
        stt = ppv.get_editor_property("settings")
        for prop, val in (("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL),
                          ("auto_exposure_apply_physical_camera_exposure", False),
                          ("auto_exposure_bias", float(args["exposure_bias"]))):
            stt.set_editor_property(f"override_{prop}", True)
            stt.set_editor_property(prop, val)
        ppv.set_editor_property("settings", stt)
    try:
        spawn(eas, unreal.VolumetricCloud, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0), "AP_Look_clouds", [])
        report["clouds"] = True
    except Exception as e:        # the engine's default cloud material: no download needed
        report["clouds"] = f"skipped: {e}"
    for c in args["cameras"]:
        t = c["transform"]
        cam = spawn(eas, unreal.CineCameraActor, unreal.Vector(*t["loc"]), unreal.Rotator(*t["rot"]), f"AP_Shot_{c['id']}",
                    [f"ap:shot={c['id']}"])
        cc = cam.get_cine_camera_component()
        fb = cc.get_editor_property("filmback")
        fb.sensor_width, fb.sensor_height = c["sensor_mm"]
        cc.set_editor_property("filmback", fb)
        cc.set_editor_property("current_focal_length", c["focal_mm"])
        fs = cc.get_editor_property("focus_settings")
        fs.focus_method = unreal.CameraFocusMethod.DISABLE
        cc.set_editor_property("focus_settings", fs)
    eal.save_directory("/Game/AP", only_if_is_dirty=False, recursive=True)
    les.save_current_level()
    report["ok"] = not report["unmatched_slots"]
    return report


if __name__ == "__main__":
    args = json.load(open(sys.argv[-1]))
    try:
        rep = main(args)
    except Exception as e:
        rep = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
        unreal.log_error(rep["traceback"])
    with open(args["report"], "w") as f:
        json.dump(rep, f, indent=1)
    log(f"look test done ok={rep['ok']}")
