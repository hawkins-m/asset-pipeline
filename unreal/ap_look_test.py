"""Look test of one library building in its place (editor Python, UE 5.8; run headless by
`ue.look_test`, rendered afterwards by ap_shot_render.py). Builds its own level and never touches
the city level.

args.json:
    {"asset", "glb", "context_glb", "terrain_glb", "map": "/Game/AP/Maps/LookTest_<asset>",
     "slots": {...as ap_material_preview...}, "mask_default": png,
     "building": {"loc": [cm], "rot": [roll, pitch, yaw]},
     "cameras": [{"id", "transform": {"loc", "rot"}, "focal_mm", "sensor_mm": [w, h]}],
     "sun": {"pitch", "yaw", "temperature", "lux"}, "fog": {...} | "fog_density", "report",
     "sky": {"intensity", "color"}, "white_balance": K,
     "ground": {kind: {"tint", "roughness", "normal_strength", "tile_m": [u, v], "color", "color_scale",
                       "rough", "normal"}} for paving, water, lawn, gravel}
The context (greybox neighbours) and terrain come in world coordinates, Nanite on, with a
material per kind (building, green, lawn, water, paving, gravel, ground, backdrop). Kinds with a
"ground" spec take M_AP_Ground: world-aligned maps (the greybox has no UVs) x Tint.
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
            "ground": [0.32, 0.29, 0.24], "paving": [0.62, 0.6, 0.55], "lawn": [0.15, 0.3, 0.08],
            "gravel": [0.45, 0.4, 0.33], "backdrop": [0.1, 0.13, 0.08]}
KIND_ROUGH = {"water": 0.03, "paving": 0.45}
GROUND = "/Game/AP/Materials/M_AP_Ground"
GROUND_VERSION = "1"


def ground_material(mask_default):
    """World-aligned surface master: UV = world XY / TileCm; BaseColorTex x ColorScale x Tint,
    RoughnessTex.G x Roughness, NormalTex (DirectX) blended in by NormalStrength."""
    m = eal.load_asset(GROUND) if eal.does_asset_exist(GROUND) else None
    if m and eal.get_metadata_tag(m, "ap:version") == GROUND_VERSION:
        return m
    if m:
        mel.delete_all_material_expressions(m)
    else:
        m = amp.tools.create_asset(GROUND.rsplit("/", 1)[1], GROUND.rsplit("/", 1)[0], unreal.Material,
                                   unreal.MaterialFactoryNew())

    def expr(cls, x, y, **props):
        e = mel.create_material_expression(m, cls, x, y)
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e
    white = ap_import.engine_asset("/Engine/EngineResources/WhiteSquareTexture")
    wp = expr(unreal.MaterialExpressionWorldPosition, -2000, 0)
    wxy = expr(unreal.MaterialExpressionComponentMask, -1800, 0, r=True, g=True, b=False, a=False)
    mel.connect_material_expressions(wp, "", wxy, "")
    tile = expr(unreal.MaterialExpressionVectorParameter, -2000, 200, parameter_name="TileCm",
                default_value=unreal.LinearColor(100, 100, 0, 0))
    txy = expr(unreal.MaterialExpressionComponentMask, -1800, 200, r=True, g=True, b=False, a=False)
    mel.connect_material_expressions(tile, "", txy, "")
    uv = expr(unreal.MaterialExpressionDivide, -1600, 100)
    mel.connect_material_expressions(wxy, "", uv, "A")
    mel.connect_material_expressions(txy, "", uv, "B")
    base = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, -400, parameter_name="BaseColorTex", texture=white)
    mel.connect_material_expressions(uv, "", base, "UVs")
    tint = expr(unreal.MaterialExpressionVectorParameter, -1300, -150, parameter_name="Tint",
                default_value=unreal.LinearColor(0.5, 0.5, 0.5, 1))
    cs = expr(unreal.MaterialExpressionScalarParameter, -1300, -50, parameter_name="ColorScale", default_value=1.0)
    m1 = expr(unreal.MaterialExpressionMultiply, -1000, -300)
    mel.connect_material_expressions(base, "RGB", m1, "A")
    mel.connect_material_expressions(tint, "", m1, "B")
    m2 = expr(unreal.MaterialExpressionMultiply, -800, -250)
    mel.connect_material_expressions(m1, "", m2, "A")
    mel.connect_material_expressions(cs, "", m2, "B")
    sat = expr(unreal.MaterialExpressionSaturate, -600, -250)
    mel.connect_material_expressions(m2, "", sat, "")
    mel.connect_material_property(sat, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rtex = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, 200, parameter_name="RoughnessTex",
                texture=mask_default, sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    mel.connect_material_expressions(uv, "", rtex, "UVs")
    rough = expr(unreal.MaterialExpressionScalarParameter, -1000, 350, parameter_name="Roughness", default_value=0.5)
    r1 = expr(unreal.MaterialExpressionMultiply, -800, 250)
    mel.connect_material_expressions(rtex, "G", r1, "A")
    mel.connect_material_expressions(rough, "", r1, "B")
    rs = expr(unreal.MaterialExpressionSaturate, -600, 250)
    mel.connect_material_expressions(r1, "", rs, "")
    mel.connect_material_property(rs, "", unreal.MaterialProperty.MP_ROUGHNESS)
    normal = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, 550, parameter_name="NormalTex",
                  texture=ap_import.engine_asset("/Engine/EngineMaterials/DefaultNormal"),
                  sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    mel.connect_material_expressions(uv, "", normal, "UVs")
    flat = expr(unreal.MaterialExpressionConstant3Vector, -1000, 500, constant=unreal.LinearColor(0, 0, 1, 1))
    nstr = expr(unreal.MaterialExpressionScalarParameter, -1000, 700, parameter_name="NormalStrength", default_value=1.0)
    nl = expr(unreal.MaterialExpressionLinearInterpolate, -600, 550)
    mel.connect_material_expressions(flat, "", nl, "A")
    mel.connect_material_expressions(normal, "RGB", nl, "B")
    mel.connect_material_expressions(nstr, "", nl, "Alpha")
    mel.connect_material_property(nl, "", unreal.MaterialProperty.MP_NORMAL)
    mel.recompile_material(m)
    eal.set_metadata_tag(m, "ap:version", GROUND_VERSION)
    eal.save_loaded_asset(m)
    log(f"built ground material v{GROUND_VERSION}")
    return m


def ground_instance(kind, spec, master):
    """MI_look_<kind> under M_AP_Ground with the spec's maps (imported once per file) and values."""
    path = f"{LOOK}/Materials/MI_look_{kind}"
    folder, name = path.rsplit("/", 1)
    mi = eal.load_asset(path) if eal.does_asset_exist(path) else amp.tools.create_asset(
        name, folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    mel.set_material_instance_parent(mi, master)
    tex_dir = f"{LOOK}/Textures"
    for key, param, tk in (("color", "BaseColorTex", "color"), ("rough", "RoughnessTex", "rough"),
                           ("normal", "NormalTex", "normal")):
        if spec.get(key):
            mel.set_material_instance_texture_parameter_value(mi, param, amp.import_texture(spec[key], tex_dir, tk))
    u, v = spec["tile_m"]
    mel.set_material_instance_vector_parameter_value(mi, "TileCm", unreal.LinearColor(u * 100, v * 100, 0, 0))
    mel.set_material_instance_vector_parameter_value(mi, "Tint", unreal.LinearColor(*spec["tint"], 1.0))
    mel.set_material_instance_scalar_parameter_value(mi, "ColorScale", spec.get("color_scale", 1.0))
    mel.set_material_instance_scalar_parameter_value(mi, "Roughness", spec["roughness"])
    mel.set_material_instance_scalar_parameter_value(mi, "NormalStrength", spec.get("normal_strength", 1.0))
    mel.update_material_instance(mi)
    eal.save_loaded_asset(mi)
    return mi


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


def attach_children(placements, actors):
    """Attached pieces: each child actor goes under its parent's (keeping its world transform),
    and parent and children share an outliner folder."""
    n = 0
    for pl in placements:
        par = actors.get(pl.get("parent"))
        kid = actors.get(pl["key"])
        if par is None or kid is None:
            continue
        rule = unreal.AttachmentRule.KEEP_WORLD
        kid.attach_to_actor(par, "", rule, rule, rule, False)
        folder = f"AP/Look/{par.get_actor_label()}"
        par.set_folder_path(folder)
        kid.set_folder_path(folder)
        n += 1
    return n


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
    if "placements" in args:                # a look scene: possibly none (nothing approved yet)
        placements, entries = args["placements"], args.get("entries") or {}
    else:                                   # the single-building look test
        placements = [{"key": asset, "entry": asset, "glb": args["glb"], **args["building"]}]
        entries = {asset: args["slots"]}
    mis, actors = {}, {}
    for pl in placements:                   # library builds, their materials shared per entry
        e = pl["entry"]
        mesh = import_static(pl["glb"], f"{LOOK}/{asset}/builds", pl.get("mesh") or pl["key"])
        own = entries.get(e)                # None: a TRELLIS piece keeps the materials in its GLB
        for i, sm in enumerate(mesh.get_editor_property("static_materials") if own is not None else []):
            slot = re.sub(r"(_\d+|\.\d+)$", "", str(sm.get_editor_property("material_slot_name")))
            spec = own.get(slot)
            if spec is None:
                report["unmatched_slots"].append(f"{e}:{slot}")
                continue
            if (e, slot) not in mis:
                mis[(e, slot)] = amp.slot_instance(e, slot, spec, surface, pbr, report)
            mesh.set_material(i, mis[(e, slot)])
        eal.save_loaded_asset(mesh)
        bld = spawn(eas, unreal.StaticMeshActor, unreal.Vector(*pl["loc"]), unreal.Rotator(*pl["rot"]),
                    f"AP_Look_{pl['key']}", [f"ap:look={pl['key']}"] + ([f"ap:parent={pl['parent']}"] if pl.get("parent") else []))
        bld.static_mesh_component.set_static_mesh(mesh)
        if pl.get("scale"):
            bld.set_actor_scale3d(unreal.Vector(*pl["scale"]))
        actors[pl["key"]] = bld
    report["attached"] = attach_children(placements, actors)
    report["placed"] = len(placements)
    # the greybox around it and the terrain, in world coordinates
    folder = f"{LOOK}/{asset}"
    if args.get("greybox_rgb"):              # the massing of every slot without an approved build
        KIND_RGB["building"] = list(args["greybox_rgb"])
        KIND_ROUGH["building"] = 0.9
    gspec = args.get("ground") or {}
    gm = ground_material(amp.default_mask(args["mask_default"])) if gspec else None
    kind_mis = {k: ground_instance(k, v, gm) for k, v in gspec.items()}
    report["ground"] = sorted(kind_mis)
    for name in ("context", "held", "terrain", "backdrop"):
        if not args.get(f"{name}_glb"):
            continue
        m = import_static(args[f"{name}_glb"], folder, name)
        for i, sm in enumerate(m.get_editor_property("static_materials")):
            k = re.sub(r"(_\d+|\.\d+)$", "", str(sm.get_editor_property("material_slot_name")))
            if name in ("backdrop", "held"):     # held: greybox of builds not approved yet, matte grey
                k = "backdrop" if name == "backdrop" else "building"
            m.set_material(i, kind_mis.get(k) or kind_material(k if k in KIND_RGB else "building", pbr))
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
    sk = args.get("sky") or {}
    if sk.get("intensity") is not None:     # raised for the shadows' cool fill
        sky.light_component.set_intensity(float(sk["intensity"]))
    if sk.get("color"):
        sky.light_component.set_light_color(unreal.LinearColor(*sk["color"], 1.0))
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
        pp = [("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL),
              ("auto_exposure_apply_physical_camera_exposure", False),
              ("auto_exposure_bias", float(args["exposure_bias"]))]
        if args.get("white_balance"):            # the camera's white point (6500 K: neutral)
            pp.append(("white_temp", float(args["white_balance"])))
        for prop, val in pp:
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
