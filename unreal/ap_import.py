"""World mode, phase 5: import an export/manifest.json into Unreal Engine 5.8 (editor Python).

Run headless by `ap ue import` (UnrealEditor-Cmd ... -run=pythonscript), or inside the
editor: `py /path/to/ap_import.py /path/to/args.json`. args.json:
    {"manifest": ".../export/manifest.json", "report": ".../import_report.json",
     "reset_layout": false, "prune": false}

What it builds (all under /Game/AP):
    Materials/M_AP_PBR                       master material (base colour texture x tint,
                                             normal texture, roughness, metallic)
    Library/<id>/SM_<id>, MI_<id>            one Nanite static mesh + material instance per asset
    Maps/<Name>                              the level:
        AP_<district>_<slot>   one actor per slot, an InstancedStaticMeshComponent
                               "ISM_<asset>" per asset it uses (instances relative to it)
        AP_Shot_<shot>         one CineCameraActor per shot (filmback + focal length)
        AP_Env_*               sun, sky light, atmosphere, fog (created once if missing)
    Cinematics/LS_<Name>                     Level Sequence, one camera cut per shot

Ownership (the UE level is the layout's source of truth once imported):
  - actors are found again by their tags (ap:id=<slot>, ap:shot=<shot>);
  - an actor's transform is only set when it's created, or with reset_layout;
  - the pipeline owns, and rewrites on every import: meshes, the instances inside a slot,
    materials, camera lens and the sequence's camera cuts;
  - tags not starting with "ap:" are kept;
  - pipeline actors missing from the manifest are reported, and deleted only with prune.
"""
import hashlib
import json
import os
import sys
import traceback

import unreal

LIB = "/Game/AP/Library"
MATERIAL = "/Game/AP/Materials/M_AP_PBR"
TMP = "/Game/AP/_import"
CINE = "/Game/AP/Cinematics"
MATERIAL_VERSION = "2"   # bump when the master graph changes: it's rebuilt in place

eal = unreal.EditorAssetLibrary
mel = unreal.MaterialEditingLibrary
tools = unreal.AssetToolsHelpers.get_asset_tools()


def log(msg):
    unreal.log(f"AP_IMPORT {msg}")


def tr(t):
    """Manifest transform -> unreal.Transform (rotator is roll, pitch, yaw)."""
    return unreal.Transform(location=unreal.Vector(*t["loc"]), rotation=unreal.Rotator(*t["rot"]),
                            scale=unreal.Vector(*t["scale"]))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- materials -------------------------------------------------------------------------------

def engine_asset(path):
    """Engine content isn't in the commandlet's asset registry: load the package directly."""
    a = unreal.load_asset(path)
    if a is None:
        raise RuntimeError(f"engine asset {path} not found")
    return a


def master_material():
    m = eal.load_asset(MATERIAL) if eal.does_asset_exist(MATERIAL) else None
    if m and eal.get_metadata_tag(m, "ap:version") == MATERIAL_VERSION:
        return m
    if m:  # an older graph: rebuild it in place, so instances keep their parent
        mel.delete_all_material_expressions(m)
    else:
        m = tools.create_asset("M_AP_PBR", os.path.dirname(MATERIAL), unreal.Material, unreal.MaterialFactoryNew())

    def expr(cls, x, y, **props):
        e = mel.create_material_expression(m, cls, x, y)
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e
    base = expr(unreal.MaterialExpressionTextureSampleParameter2D, -900, -300, parameter_name="BaseColorTex",
                texture=engine_asset("/Engine/EngineResources/WhiteSquareTexture"))
    tint = expr(unreal.MaterialExpressionVectorParameter, -900, -50, parameter_name="Tint",
                default_value=unreal.LinearColor(1, 1, 1, 1))
    mul = expr(unreal.MaterialExpressionMultiply, -500, -200)
    mel.connect_material_expressions(base, "RGB", mul, "A")
    mel.connect_material_expressions(tint, "", mul, "B")
    mel.connect_material_property(mul, "", unreal.MaterialProperty.MP_BASE_COLOR)
    rough = expr(unreal.MaterialExpressionScalarParameter, -500, 50, parameter_name="Roughness", default_value=0.75)
    mel.connect_material_property(rough, "", unreal.MaterialProperty.MP_ROUGHNESS)
    metal = expr(unreal.MaterialExpressionScalarParameter, -500, 150, parameter_name="Metallic", default_value=0.0)
    mel.connect_material_property(metal, "", unreal.MaterialProperty.MP_METALLIC)
    normal = expr(unreal.MaterialExpressionTextureSampleParameter2D, -900, 250, parameter_name="NormalTex",
                  texture=engine_asset("/Engine/EngineMaterials/DefaultNormal"),
                  sampler_type=unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)
    mel.connect_material_property(normal, "RGB", unreal.MaterialProperty.MP_NORMAL)
    mel.recompile_material(m)
    eal.set_metadata_tag(m, "ap:version", MATERIAL_VERSION)
    eal.save_loaded_asset(m)
    log(f"built master material v{MATERIAL_VERSION}")
    return m


def material_instance(asset, master):
    aid = asset["id"]
    path = f"{LIB}/{aid}/MI_{aid}"
    mi = eal.load_asset(path) if eal.does_asset_exist(path) else tools.create_asset(
        f"MI_{aid}", f"{LIB}/{aid}", unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
    mel.set_material_instance_parent(mi, master)
    mel.set_material_instance_vector_parameter_value(mi, "Tint", unreal.LinearColor(*asset["tint"], 1.0))
    mel.update_material_instance(mi)
    eal.save_loaded_asset(mi)
    return mi


# --- meshes ----------------------------------------------------------------------------------

def import_mesh(asset, root, mi, report):
    """Interchange glTF import -> /Game/AP/Library/<id>/SM_<id> with Nanite and the MI.
    Skipped when the GLB is unchanged; a changed GLB replaces the old mesh in place
    (references kept)."""
    aid = asset["id"]
    target = f"{LIB}/{aid}/SM_{aid}"
    glb = os.path.join(root, asset["glb"])
    digest = sha(glb)
    old = eal.load_asset(target) if eal.does_asset_exist(target) else None
    if old and eal.get_metadata_tag(old, "ap:glb_sha") == digest:
        report["assets"]["skipped"].append(aid)
        return old
    task = unreal.AssetImportTask()
    task.filename, task.destination_path = glb, f"{TMP}/{aid}"
    task.automated, task.replace_existing, task.save = True, True, False
    tools.import_asset_tasks([task])
    new = None
    for p in task.imported_object_paths:
        obj = unreal.load_object(None, p)
        if isinstance(obj, unreal.StaticMesh):
            new = obj
    if new is None:
        raise RuntimeError(f"{aid}: Interchange imported no static mesh from {glb}")
    if old:
        eal.consolidate_assets(new, [old])          # references to the old mesh -> new one
    if not eal.rename_asset(new.get_path_name().split(".")[0], target):
        raise RuntimeError(f"{aid}: could not move the import to {target}")
    mesh = eal.load_asset(target)
    ns = mesh.get_editor_property("nanite_settings")
    ns.enabled = True
    mesh.set_editor_property("nanite_settings", ns)
    for i in range(len(mesh.get_editor_property("static_materials"))):
        mesh.set_material(i, mi)
    eal.set_metadata_tag(mesh, "ap:glb_sha", digest)
    eal.save_loaded_asset(mesh)
    report["assets"]["imported"].append(aid)
    return mesh


def clean_tmp():
    if not eal.does_directory_exist(TMP):
        return
    redirectors = [eal.load_asset(p) for p in eal.list_assets(TMP, recursive=True)
                   if eal.find_asset_data(p).asset_class_path.asset_name == "ObjectRedirector"]
    if redirectors:
        tools.fixup_referencers([r for r in redirectors if r])
    eal.delete_directory(TMP)


# --- level -----------------------------------------------------------------------------------

def tagged(actor):
    """{"ap:id": "...", "ap:shot": "...", ...} from an actor's tags."""
    out = {}
    for t in actor.tags:
        k, _, v = str(t).partition("=")
        if k.startswith("ap:"):
            out[k] = v
    return out


def set_tags(actor, tags):
    keep = [t for t in actor.tags if not str(t).startswith("ap:")]
    actor.tags = keep + [unreal.Name(t) for t in tags]


def ism_components(actor):
    return {c.get_name(): c for c in actor.get_components_by_class(unreal.InstancedStaticMeshComponent)}


def add_ism(actor, name):
    sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
    root = sds.k2_gather_subobject_data_for_instance(actor)[0]
    handle, fail = sds.add_new_subobject(
        unreal.AddNewSubobjectParams(parent_handle=root, new_class=unreal.InstancedStaticMeshComponent))
    if str(fail):
        raise RuntimeError(f"{actor.get_actor_label()}: can't add {name}: {fail}")
    sds.rename_subobject(handle, unreal.Text(name))
    return unreal.SubobjectDataBlueprintFunctionLibrary.get_object(
        unreal.SubobjectDataBlueprintFunctionLibrary.get_data(handle))


def environment(eas, existing_env):
    """Sun, sky light, atmosphere and fog, once (tag ap:env=<kind>); then they're yours."""
    specs = [("sun", unreal.DirectionalLight, unreal.Rotator(0, -42, 35)),
             ("skylight", unreal.SkyLight, unreal.Rotator(0, 0, 0)),
             ("atmosphere", unreal.SkyAtmosphere, unreal.Rotator(0, 0, 0)),
             ("fog", unreal.ExponentialHeightFog, unreal.Rotator(0, 0, 0))]
    made = []
    for kind, cls, rot in specs:
        if kind in existing_env:
            continue
        a = eas.spawn_actor_from_class(cls, unreal.Vector(0, 0, 5000), rot)
        a.set_actor_label(f"AP_Env_{kind}")
        set_tags(a, [f"ap:env={kind}", "ap:source=pipeline"])
        a.set_folder_path("AP/Environment")
        if kind == "sun":
            a.light_component.set_editor_property("atmosphere_sun_light", True)
            a.light_component.set_mobility(unreal.ComponentMobility.MOVABLE)
        if kind == "skylight":
            a.light_component.set_editor_property("real_time_capture", True)
            a.light_component.set_mobility(unreal.ComponentMobility.MOVABLE)
        made.append(kind)
    return made


def place_slots(man, meshes, eas, by_tag, args, report):
    for s in man["slots"]:
        actor = by_tag.get(("ap:id", s["id"]))
        if actor is None:
            actor = eas.spawn_actor_from_class(unreal.Actor, unreal.Vector(0, 0, 0))
            actor.set_actor_transform(tr(s["transform"]), False, False)
            report["actors"]["created"].append(s["id"])
        elif args.get("reset_layout"):
            actor.set_actor_transform(tr(s["transform"]), False, False)
            report["actors"]["reset"].append(s["id"])
        else:
            report["actors"]["kept_transform"].append(s["id"])
        actor.set_actor_label(s["label"])
        set_tags(actor, s["tags"])
        actor.set_folder_path(s["folder"])
        have = ism_components(actor)
        want = {f"ISM_{c['asset']}": c for c in s["components"]}
        for name, c in want.items():
            ism = have.get(name) or add_ism(actor, name)
            ism.set_static_mesh(meshes[c["asset"]])
            ism.clear_instances()
            ism.add_instances([tr(t) for t in c["instances"]], False, False)
        for name, comp in have.items():
            if name.startswith("ISM_") and name not in want:   # the slot no longer uses it
                comp.clear_instances()
                comp.set_static_mesh(None)


def place_cameras(man, eas, by_tag, args, report):
    cams = {}
    for s in man["shots"]:
        cam = by_tag.get(("ap:shot", s["id"]))
        if cam is None:
            cam = eas.spawn_actor_from_class(unreal.CineCameraActor, unreal.Vector(0, 0, 0))
            cam.set_actor_transform(tr(s["transform"]), False, False)
            report["cameras"]["created"].append(s["id"])
        elif args.get("reset_layout"):
            cam.set_actor_transform(tr(s["transform"]), False, False)
        cam.set_actor_label(s["label"])
        set_tags(cam, s["tags"])
        cam.set_folder_path(s["folder"])
        cc = cam.get_cine_camera_component()
        fb = cc.get_editor_property("filmback")
        fb.sensor_width, fb.sensor_height = s["sensor_width_mm"], s["sensor_height_mm"]
        cc.set_editor_property("filmback", fb)
        cc.set_editor_property("current_focal_length", s["focal_length_mm"])
        cams[s["id"]] = cam
    return cams


def sequence(man, cams, report):
    sq = man["sequence"]
    path = f"{CINE}/{sq['name']}"
    seq = eal.load_asset(path) if eal.does_asset_exist(path) else tools.create_asset(
        sq["name"], CINE, unreal.LevelSequence, unreal.LevelSequenceFactoryNew())
    for t in list(seq.get_tracks()):
        if isinstance(t, unreal.MovieSceneCameraCutTrack):
            seq.remove_track(t)
    for b in list(seq.get_bindings()):
        if str(b.get_display_name()).startswith("AP_Shot_"):
            b.remove()
    fps, length = sq["fps"], sq["fps"] * sq["seconds_per_shot"]
    seq.set_display_rate(unreal.FrameRate(fps, 1))
    seq.set_playback_start(0)
    seq.set_playback_end(length * len(sq["shots"]))
    track = seq.add_track(unreal.MovieSceneCameraCutTrack)
    for i, shot in enumerate(sq["shots"]):
        binding = seq.add_possessable(cams[shot])
        section = track.add_section()
        section.set_range(i * length, (i + 1) * length)
        section.set_camera_binding_id(seq.get_binding_id(binding))
    eal.save_loaded_asset(seq)
    report["sequence"] = {"path": path, "sections": len(track.get_sections()), "frames": length * len(sq["shots"])}
    return seq


# --- checks ----------------------------------------------------------------------------------

def checks(man, meshes, eas):
    """Re-read what was built and compare with the manifest."""
    out = {"nanite_off": [], "missing_mi": [], "instance_mismatch": [], "samples": []}
    for aid, mesh in meshes.items():
        if not mesh.get_editor_property("nanite_settings").enabled:
            out["nanite_off"].append(aid)
        mat = mesh.get_material(0)
        if not mat or mat.get_name() != f"MI_{aid}":
            out["missing_mi"].append(aid)
    actors = {tagged(a).get("ap:id"): a for a in eas.get_all_level_actors() if "ap:id" in tagged(a)}
    total = 0
    for s in man["slots"]:
        a = actors.get(s["id"])
        have = ism_components(a) if a else {}
        for c in s["components"]:
            ism = have.get(f"ISM_{c['asset']}")
            n = ism.get_instance_count() if ism else 0
            total += n
            if n != len(c["instances"]):
                out["instance_mismatch"].append(f"{s['id']}/{c['asset']}: {n} != {len(c['instances'])}")
        # world position of the first instance of up to two components per slot, with the
        # actor's current transform: `ap ue import` recomputes them from the greybox (an
        # independent check of the rotator / mirror conventions, valid for moved actors too)
        if a:
            t = a.get_actor_transform()
            r = t.rotation.rotator()
            actor_t = {"loc": [t.translation.x, t.translation.y, t.translation.z], "rot": [r.roll, r.pitch, r.yaw],
                       "scale": [t.scale3d.x, t.scale3d.y, t.scale3d.z]}
            for c in s["components"][:2]:
                ism = have.get(f"ISM_{c['asset']}")
                if ism and ism.get_instance_count():
                    w = ism.get_instance_transform(0, True).translation
                    out["samples"].append({"slot": s["id"], "asset": c["asset"], "actor": actor_t,
                                           "world_cm": [w.x, w.y, w.z]})
    out["instances_total"] = total
    out["cameras"] = sum(1 for a in eas.get_all_level_actors() if "ap:shot" in tagged(a))
    return out


# --- main ------------------------------------------------------------------------------------

def main(args):
    man = json.load(open(args["manifest"]))
    root = os.path.dirname(args["manifest"])
    report = {"ok": False, "map": man["map"], "assets": {"imported": [], "skipped": []},
              "actors": {"created": [], "reset": [], "kept_transform": []}, "cameras": {"created": []},
              "warnings": [], "environment": []}
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    # the level is loaded first, so replacing a mesh updates every reference to it
    if eal.does_asset_exist(man["map"]):
        les.load_level(man["map"])
    elif not les.new_level(man["map"]):
        raise RuntimeError(f"can't create level {man['map']}")
    master = master_material()
    meshes = {}
    for a in man["assets"]:
        meshes[a["id"]] = import_mesh(a, root, material_instance(a, master), report)
    clean_tmp()
    by_tag = {}
    env = set()
    for a in eas.get_all_level_actors():
        tg = tagged(a)
        for k in ("ap:id", "ap:shot"):
            if k in tg:
                by_tag[(k, tg[k])] = a
        if "ap:env" in tg:
            env.add(tg["ap:env"])
    report["environment"] = environment(eas, env)
    place_slots(man, meshes, eas, by_tag, args, report)
    cams = place_cameras(man, eas, by_tag, args, report)
    sequence(man, cams, report)
    known = {s["id"] for s in man["slots"]} | {s["id"] for s in man["shots"]}
    for a in eas.get_all_level_actors():
        tg = tagged(a)
        sid = tg.get("ap:id") or tg.get("ap:shot")
        if tg.get("ap:source") == "pipeline" and sid and sid not in known:
            if args.get("prune"):
                eas.destroy_actor(a)
                report["warnings"].append(f"pruned {a.get_actor_label()}")
            else:
                report["warnings"].append(f"{a.get_actor_label()} is not in the manifest (prune to delete)")
    eal.save_directory("/Game/AP", only_if_is_dirty=False, recursive=True)
    les.save_current_level()
    report["checks"] = checks(man, meshes, eas)
    c = report["checks"]
    report["ok"] = not (c["nanite_off"] or c["missing_mi"] or c["instance_mismatch"])
    return report


if __name__ == "__main__":
    args = json.load(open(sys.argv[-1]))
    try:
        rep = main(args)
    except Exception as e:  # the orchestrator reads the report, not the exit code
        rep = {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
        unreal.log_error(rep["traceback"])
    with open(args["report"], "w") as f:
        json.dump(rep, f, indent=1)
    log(f"done ok={rep['ok']}")
