"""Preview one library building in Unreal with library materials (editor Python, UE 5.8; run
headless by `ap library ue-preview`, then rendered by ap_shot_render.py).

args.json:
    {"asset": "<id>", "glb": "...glb", "map": "/Game/AP/Maps/MaterialPreview", "shot": "<shot id>",
     "camera": {"az": -35, "el": 0.35, "dist_factor": 1.55, "focal_mm": 50, "sensor_mm": [36, 25.6]},
     "slots": {"<Blender material name>": {"surface": {"type", "rgb", "uv_scale", "acg_id",
               "textures": {"color", "normal_dx", "rough"}, "megascans": [names], "megascans_m": 2.0}}
               | {"plain": {"rgb_lin", "rough", "metal"}}},
     "mask_default": "...png (white)", "report": "...json"}

What it builds:
    Materials/M_AP_Surface           tinted surface master: BaseColorTex x (TargetColor / the
                                     texture's average, read off its lowest mip), so any scan
                                     (ambientCG or Megascans) takes the sheet's colour and keeps
                                     its own detail; RoughnessTex.G (plain roughness maps and
                                     Megascans' packed ORD alike); NormalTex (DirectX); UVScale
    Materials/Surfaces/<acg id>/     the ambientCG maps, imported once
    Preview/<asset>/SM_<asset>       the GLB, one MI per material slot (MI_<asset>_<slot>)
    the map: the mesh, a ground plane, sun / sky / atmosphere, and a CineCamera tagged
    ap:shot=<shot> at the Blender review render's three-quarter view
A Megascans surface is used when one of the type's names matches a material instance anywhere
under /Game (added through Fab); otherwise the ambientCG maps. The report says which.
"""
import json
import math
import os
import re
import sys
import traceback

import unreal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ap_import  # noqa: E402  (master_material, environment, set_tags, tools)

SURFACE = "/Game/AP/Materials/M_AP_Surface"
SURFACE_VERSION = "3"
TEX_ROOT = "/Game/AP/Materials/Surfaces"
PREVIEW = "/Game/AP/Preview"

eal = unreal.EditorAssetLibrary
mel = unreal.MaterialEditingLibrary
tools = unreal.AssetToolsHelpers.get_asset_tools()
log = ap_import.log


def default_mask(path):
    """A white linear texture with Masks compression: the RoughnessTex default (a Masks sampler
    with the engine's sRGB white square fails to compile, and every instance turns WorldGrid)."""
    return import_texture(path, f"{TEX_ROOT}/_Default", "rough")


def surface_material(mask_default):
    m = eal.load_asset(SURFACE) if eal.does_asset_exist(SURFACE) else None
    if m and eal.get_metadata_tag(m, "ap:version") == SURFACE_VERSION:
        return m
    if m:
        mel.delete_all_material_expressions(m)
    else:
        m = tools.create_asset(SURFACE.rsplit("/", 1)[1], SURFACE.rsplit("/", 1)[0], unreal.Material,
                               unreal.MaterialFactoryNew())

    def expr(cls, x, y, **props):
        e = mel.create_material_expression(m, cls, x, y)
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e
    white = ap_import.engine_asset("/Engine/EngineResources/WhiteSquareTexture")
    uv = expr(unreal.MaterialExpressionTextureCoordinate, -1900, 0)
    uvs = expr(unreal.MaterialExpressionScalarParameter, -1900, 120, parameter_name="UVScale", default_value=1.0)
    # UV x (UVScale, UVScale x UVAspect): UVAspect for scans that aren't square (a 2:1 paving scan)
    asp = expr(unreal.MaterialExpressionScalarParameter, -2100, 220, parameter_name="UVAspect", default_value=1.0)
    one = expr(unreal.MaterialExpressionConstant, -2100, 320, r=1.0)
    av = expr(unreal.MaterialExpressionAppendVector, -1900, 260)
    mel.connect_material_expressions(one, "", av, "A")
    mel.connect_material_expressions(asp, "", av, "B")
    sc = expr(unreal.MaterialExpressionMultiply, -1750, 180)
    mel.connect_material_expressions(uvs, "", sc, "A")
    mel.connect_material_expressions(av, "", sc, "B")
    uvm = expr(unreal.MaterialExpressionMultiply, -1600, 50)
    mel.connect_material_expressions(uv, "", uvm, "A")
    mel.connect_material_expressions(sc, "", uvm, "B")
    base = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, -400, parameter_name="BaseColorTex", texture=white)
    mel.connect_material_expressions(uvm, "", base, "UVs")
    # the same texture at its smallest mip: its average colour
    mean = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, -150, parameter_name="BaseColorTex", texture=white,
                mip_value_mode=unreal.TextureMipValueMode.TMVM_MIP_LEVEL, const_mip_value=14)
    target = expr(unreal.MaterialExpressionVectorParameter, -1300, 100, parameter_name="TargetColor",
                  default_value=unreal.LinearColor(0.5, 0.5, 0.5, 1))
    floor = expr(unreal.MaterialExpressionMax, -1000, -150, const_b=0.002)
    mel.connect_material_expressions(mean, "RGB", floor, "A")
    ratio = expr(unreal.MaterialExpressionDivide, -800, -50)
    mel.connect_material_expressions(target, "", ratio, "A")
    mel.connect_material_expressions(floor, "", ratio, "B")
    tinted = expr(unreal.MaterialExpressionMultiply, -600, -300)
    mel.connect_material_expressions(base, "RGB", tinted, "A")
    mel.connect_material_expressions(ratio, "", tinted, "B")
    # Contrast: the scan's spread around the target colour, scaled to the sheet band's
    # (target + k x (tinted - target), as material_lib.tinted bakes it for Blender)
    con = expr(unreal.MaterialExpressionScalarParameter, -600, -550, parameter_name="Contrast", default_value=1.0)
    cl = expr(unreal.MaterialExpressionLinearInterpolate, -500, -450)
    mel.connect_material_expressions(target, "", cl, "A")
    mel.connect_material_expressions(tinted, "", cl, "B")
    mel.connect_material_expressions(con, "", cl, "Alpha")
    amount = expr(unreal.MaterialExpressionScalarParameter, -600, -100, parameter_name="TintAmount", default_value=1.0)
    lerp = expr(unreal.MaterialExpressionLinearInterpolate, -400, -300)
    mel.connect_material_expressions(base, "RGB", lerp, "A")
    mel.connect_material_expressions(cl, "", lerp, "B")
    mel.connect_material_expressions(amount, "", lerp, "Alpha")
    sat = expr(unreal.MaterialExpressionSaturate, -200, -300)
    mel.connect_material_expressions(lerp, "", sat, "")
    mel.connect_material_property(sat, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rtex = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, 300, parameter_name="RoughnessTex", texture=mask_default,
                sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
    mel.connect_material_expressions(uvm, "", rtex, "UVs")
    rmul = expr(unreal.MaterialExpressionScalarParameter, -1000, 450, parameter_name="RoughnessMultiplier", default_value=1.0)
    r1 = expr(unreal.MaterialExpressionMultiply, -800, 350)
    mel.connect_material_expressions(rtex, "G", r1, "A")
    mel.connect_material_expressions(rmul, "", r1, "B")
    rs = expr(unreal.MaterialExpressionSaturate, -600, 350)
    mel.connect_material_expressions(r1, "", rs, "")
    mel.connect_material_property(rs, "", unreal.MaterialProperty.MP_ROUGHNESS)
    normal = expr(unreal.MaterialExpressionTextureSampleParameter2D, -1300, 650, parameter_name="NormalTex",
                  texture=ap_import.engine_asset("/Engine/EngineMaterials/DefaultNormal"),
                  sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    mel.connect_material_expressions(uvm, "", normal, "UVs")
    flat = expr(unreal.MaterialExpressionConstant3Vector, -1000, 600, constant=unreal.LinearColor(0, 0, 1, 1))
    nstr = expr(unreal.MaterialExpressionScalarParameter, -1000, 800, parameter_name="NormalStrength", default_value=1.0)
    nl = expr(unreal.MaterialExpressionLinearInterpolate, -600, 650)
    mel.connect_material_expressions(flat, "", nl, "A")
    mel.connect_material_expressions(normal, "RGB", nl, "B")
    mel.connect_material_expressions(nstr, "", nl, "Alpha")
    mel.connect_material_property(nl, "", unreal.MaterialProperty.MP_NORMAL)
    mel.recompile_material(m)
    eal.set_metadata_tag(m, "ap:version", SURFACE_VERSION)
    eal.save_loaded_asset(m)
    log(f"built surface material v{SURFACE_VERSION}")
    return m


# --- textures ------------------------------------------------------------------------------

def import_texture(path, folder, kind):
    """An ambientCG map as a texture (kept when already imported). kind: color | normal | rough."""
    name = "T_" + os.path.splitext(os.path.basename(path))[0].replace("-", "_")
    target = f"{folder}/{name}"
    if not eal.does_asset_exist(target):
        task = unreal.AssetImportTask()
        task.filename, task.destination_path, task.destination_name = path, folder, name
        task.automated, task.replace_existing, task.save = True, True, False
        tools.import_asset_tasks([task])
    tex = eal.load_asset(target)
    if tex is None:
        raise RuntimeError(f"could not import {path}")
    if kind == "normal":
        tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)
        tex.set_editor_property("srgb", False)
    elif kind == "rough":
        tex.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)
        tex.set_editor_property("srgb", False)
    eal.save_loaded_asset(tex)
    return tex


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


_mis = None


def megascans_textures(names):
    """The first Megascans material instance under /Game whose name matches one of names:
    ({color, normal, rough} textures, its path), or (None, None)."""
    global _mis
    if _mis is None:
        ar = unreal.AssetRegistryHelpers.get_asset_registry()
        ar.scan_paths_synchronous(["/Game"], True)
        _mis = [a for a in ar.get_assets_by_class(unreal.TopLevelAssetPath("/Script/Engine", "MaterialInstanceConstant"), True)
                if not str(a.package_path).startswith("/Game/AP")]
    for want in names:
        w = _norm(want)
        for a in _mis:
            if w and w in _norm(str(a.asset_name)) + _norm(str(a.package_path)):
                mi = unreal.load_asset(str(a.package_name))
                tex = {}
                for p in mi.get_editor_property("texture_parameter_values"):
                    n, t = str(p.parameter_info.name).lower(), p.parameter_value
                    if t is None:
                        continue
                    if any(k in n for k in ("albedo", "basecolor", "base color", "diffuse")):
                        tex.setdefault("color", t)
                    elif "normal" in n:
                        tex.setdefault("normal", t)
                    elif "rough" in n or "ord" in n:        # Megascans pack AO / Roughness / Displacement
                        tex.setdefault("rough", t)
                if "color" in tex:
                    return tex, str(a.package_name)
    return None, None


# --- materials per slot --------------------------------------------------------------------

def slot_instance(asset, slot, spec, surface_master, pbr_master, report):
    path = f"{PREVIEW}/{asset}/MI_{asset}_{slot}"
    folder, name = path.rsplit("/", 1)
    mi = eal.load_asset(path) if eal.does_asset_exist(path) else tools.create_asset(
        name, folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    if "plain" in spec:
        p = spec["plain"]
        mel.set_material_instance_parent(mi, pbr_master)
        mel.set_material_instance_vector_parameter_value(mi, "Tint", unreal.LinearColor(*p["rgb_lin"], 1.0))
        mel.set_material_instance_scalar_parameter_value(mi, "Roughness", float(p["rough"]))
        mel.set_material_instance_scalar_parameter_value(mi, "Metallic", float(p.get("metal", 0.0)))
        report["slots"][slot] = {"source": "plain"}
    else:
        s = spec["surface"]
        mel.set_material_instance_parent(mi, surface_master)
        tex, src = megascans_textures(s.get("megascans", []))
        uv_scale = s.get("uv_scale", 1.0)
        if tex:
            uv_scale *= s.get("uv_scale_megascans", 1.0)
            report["slots"][slot] = {"source": "megascans", "type": s["type"], "asset": src}
        else:
            folder = f"{TEX_ROOT}/{s['acg_id']}"
            t = s["textures"]
            tex = {"color": import_texture(t["color"], folder, "color"), "normal": import_texture(t["normal_dx"], folder, "normal"),
                   "rough": import_texture(t["rough"], folder, "rough")}
            report["slots"][slot] = {"source": "ambientcg", "type": s["type"], "asset": s["acg_id"],
                                     "megascans_missing": s.get("megascans", [])}
        mel.set_material_instance_texture_parameter_value(mi, "BaseColorTex", tex["color"])
        if tex.get("normal"):
            mel.set_material_instance_texture_parameter_value(mi, "NormalTex", tex["normal"])
        if tex.get("rough"):
            mel.set_material_instance_texture_parameter_value(mi, "RoughnessTex", tex["rough"])
        mel.set_material_instance_vector_parameter_value(mi, "TargetColor", unreal.LinearColor(*s["rgb_lin"], 1.0))
        mel.set_material_instance_scalar_parameter_value(mi, "UVScale", float(uv_scale))
        mel.set_material_instance_scalar_parameter_value(mi, "UVAspect", float(s.get("uv_aspect", 1.0)))
        mel.set_material_instance_scalar_parameter_value(mi, "Contrast", float(s.get("contrast", 1.0)))
    mel.update_material_instance(mi)
    eal.save_loaded_asset(mi)
    return mi


def import_glb(asset, glb):
    """The GLB as /Game/AP/Preview/<asset>/SM_<asset> (re-imported every run)."""
    folder = f"{PREVIEW}/{asset}"
    target = f"{folder}/SM_{asset}"
    tmp = f"{folder}/_import"
    if eal.does_directory_exist(tmp):
        eal.delete_directory(tmp)
    task = unreal.AssetImportTask()
    task.filename, task.destination_path = glb, tmp
    task.automated, task.replace_existing, task.save = True, True, False
    tools.import_asset_tasks([task])
    mesh = next((unreal.load_object(None, p) for p in task.imported_object_paths
                 if isinstance(unreal.load_object(None, p), unreal.StaticMesh)), None)
    if mesh is None:
        raise RuntimeError(f"Interchange imported no static mesh from {glb}")
    if eal.does_asset_exist(target):
        eal.consolidate_assets(mesh, [eal.load_asset(target)])
        if eal.does_asset_exist(target):
            eal.delete_asset(target)
    if not eal.rename_asset(mesh.get_path_name().split(".")[0], target):
        raise RuntimeError(f"could not move the import to {target}")
    for p in eal.list_assets(tmp, recursive=True):   # redirectors left by the move (see ap_import.clean_tmp)
        if eal.find_asset_data(p).asset_class_path.asset_name == "ObjectRedirector":
            eal.delete_asset(p)
    eal.delete_directory(tmp)
    mesh = eal.load_asset(target)
    ns = mesh.get_editor_property("nanite_settings")
    ns.enabled = True
    mesh.set_editor_property("nanite_settings", ns)
    return mesh


def look(eas, lk, report):
    """The Blender review render's light and exposure: the sun at its angle and irradiance (x 1.2,
    so that at EV100 0 a white wall in sun reads as in Blender's Standard view), manual
    exposure, the filmic tone curve off, the sky light scaled to Blender's flat sky."""
    for a in eas.get_all_level_actors():
        kind = ap_import.tagged(a).get("ap:env")
        if kind == "sun":
            a.set_actor_rotation(unreal.Rotator(0.0, -lk["sun_elevation"], lk["sun_yaw"]), False)
            a.light_component.set_intensity(lk["sun_lux"])
        elif kind == "skylight":
            a.light_component.set_intensity(lk["sky_intensity"])
        elif ap_import.tagged(a).get("ap:look") == "ppv":
            eas.destroy_actor(a)
    ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
    ppv.set_actor_label("AP_Preview_look")
    ap_import.set_tags(ppv, ["ap:look=ppv", "ap:source=preview"])
    ppv.set_editor_property("unbound", True)
    st = ppv.get_editor_property("settings")
    applied = []
    for prop, val in (("auto_exposure_method", unreal.AutoExposureMethod.AEM_MANUAL), ("auto_exposure_apply_physical_camera_exposure", False),
                      ("auto_exposure_bias", lk["exposure_bias"]), ("tone_curve_amount", 0.0),
                      ("bloom_intensity", 0.0), ("vignette_intensity", 0.0)):
        try:
            st.set_editor_property(f"override_{prop}", True)
            st.set_editor_property(prop, val)
            applied.append(prop)
        except Exception as e:  # a setting this engine version doesn't have
            report.setdefault("look_skipped", []).append(f"{prop}: {e}")
    ppv.set_editor_property("settings", st)
    report["look"] = applied


def look_at(src, dst):
    d = dst - src
    yaw = math.degrees(math.atan2(d.y, d.x))
    pitch = math.degrees(math.atan2(d.z, math.hypot(d.x, d.y)))
    return unreal.Rotator(0.0, pitch, yaw)          # roll, pitch, yaw


def main(args):
    report = {"ok": False, "slots": {}, "unmatched_slots": []}
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    if eal.does_asset_exist(args["map"]):
        les.load_level(args["map"])
        for a in eas.get_all_level_actors():
            if ap_import.tagged(a).get("ap:source") == "preview":
                eas.destroy_actor(a)
    elif not les.new_level(args["map"]):
        raise RuntimeError(f"can't create level {args['map']}")
    asset = args["asset"]
    mesh = import_glb(asset, args["glb"])
    surface_master, pbr_master = surface_material(default_mask(args["mask_default"])), ap_import.master_material()
    for i, sm in enumerate(mesh.get_editor_property("static_materials")):
        slot = str(sm.get_editor_property("material_slot_name"))
        spec = args["slots"].get(slot) or args["slots"].get(re.sub(r"(_\d+|\.\d+)$", "", slot))
        if spec is None:
            report["unmatched_slots"].append(slot)
            continue
        mesh.set_material(i, slot_instance(asset, slot, spec, surface_master, pbr_master, report))
    eal.save_loaded_asset(mesh)

    actor = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0, 0, 0))
    actor.static_mesh_component.set_static_mesh(mesh)
    actor.set_actor_label(f"AP_Preview_{asset}")
    ap_import.set_tags(actor, ["ap:source=preview"])
    b = mesh.get_bounds()
    ctr, ext = b.origin, b.box_extent
    size = 2.0 * math.sqrt(ext.x ** 2 + ext.y ** 2 + ext.z ** 2)
    # ground, a little under the building, the review render's grey
    ground = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(ctr.x, ctr.y, ctr.z - ext.z - 1.0))
    ground.static_mesh_component.set_static_mesh(ap_import.engine_asset("/Engine/BasicShapes/Plane"))
    ground.set_actor_scale3d(unreal.Vector(size * 6 / 100, size * 6 / 100, 1))
    g = dict(args["ground"])              # paving at its real size: the plane is size x 6 cm across
    g["uv_scale"] = size * 6 / 100 / g.pop("tile_m")[0]
    gm = slot_instance(asset, "ground", {"surface": g}, surface_master, pbr_master, report)
    report["ground"] = report["slots"].pop("ground", None)
    ground.static_mesh_component.set_material(0, gm)
    ground.set_actor_label("AP_Preview_ground")
    ap_import.set_tags(ground, ["ap:source=preview"])
    env = {ap_import.tagged(a).get("ap:env") for a in eas.get_all_level_actors()} - {None}
    ap_import.environment(eas, env)
    look(eas, args["look"], report)

    c = args["camera"]
    az = math.radians(c["az"])
    # Blender's view direction (sin az, -cos az, el) with Y flipped for Unreal
    d = unreal.Vector(math.sin(az), math.cos(az), c["el"])
    d = d * (1.0 / math.sqrt(d.x ** 2 + d.y ** 2 + d.z ** 2))
    pos = ctr + d * (size * c["dist_factor"])
    cam = eas.spawn_actor_from_class(unreal.CineCameraActor, pos, look_at(pos, ctr))
    cam.set_actor_label(f"AP_Shot_{args['shot']}")
    ap_import.set_tags(cam, [f"ap:shot={args['shot']}", "ap:source=preview"])
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
    report["mesh"] = mesh.get_path_name()
    report["bounds_cm"] = [ext.x * 2, ext.y * 2, ext.z * 2]
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
    log(f"preview done ok={rep['ok']}")
