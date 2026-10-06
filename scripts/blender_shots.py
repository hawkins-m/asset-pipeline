"""World mode shot passes in headless Blender (asset_pipeline/stages/sw_shots.py runs it).

    blender -b GREYBOX.blend --factory-startup --python scripts/blender_shots.py -- \
        --out-dir DIR --colors COLORS.json [--shots a,b] [--passes ids,depth,normal,preview] \
        [--depth-scale 4000] [--virtual CAMS.json] [--preview-samples 16]

Per shot camera (objects tagged ap_shot; --virtual adds temporary cameras for site
previews), into DIR/<shot>/:
    ids.png        flat slot colour per pixel (COLORS: slot id -> [r, g, b] bytes, sky black)
    depth_raw.png  16-bit: camera Z depth / depth-scale (sky 0)
    normal.png     camera-space normal * 0.5 + 0.5
    preview.png    grey preview (sun + sky), for people

The data passes use Cycles on the CPU at 1 sample with a tiny box filter, no dither and the
Raw view transform. Each pixel then holds exactly one surface's emission value: no
anti-aliasing blends two slot colours into a third that doesn't exist.
"""
import json
import math
import sys
import time

import bpy
from mathutils import Vector

args = sys.argv[sys.argv.index("--") + 1:]


def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default


out_dir = opt("--out-dir")
colors = json.load(open(opt("--colors")))
passes = opt("--passes", "ids,depth,normal,preview").split(",")
depth_scale = float(opt("--depth-scale", "4000"))
preview_samples = int(opt("--preview-samples", "16"))
want = set(opt("--shots", "").split(",")) - {""}
UNTAGGED = colors.get("__untagged__", [255, 0, 255])

sc = bpy.context.scene
vl = bpy.context.view_layer


def log(msg):
    print("SHOTS", msg, flush=True)


def emission_material(name, build):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Strength"].default_value = 1.0
    nt.links.new(em.outputs["Emission"], out.inputs["Surface"])
    nt.links.new(build(nt), em.inputs["Color"])
    return mat


def _ids(nt):
    return nt.nodes.new("ShaderNodeObjectInfo").outputs["Color"]


def _depth(nt):
    cam = nt.nodes.new("ShaderNodeCameraData")
    div = nt.nodes.new("ShaderNodeMath")
    div.operation = "DIVIDE"
    div.inputs[1].default_value = depth_scale
    nt.links.new(cam.outputs["View Z Depth"], div.inputs[0])
    return div.outputs[0]


def _normal(nt):
    geo = nt.nodes.new("ShaderNodeNewGeometry")
    vt = nt.nodes.new("ShaderNodeVectorTransform")
    vt.vector_type, vt.convert_from, vt.convert_to = "NORMAL", "WORLD", "CAMERA"
    ma = nt.nodes.new("ShaderNodeVectorMath")
    ma.operation = "MULTIPLY_ADD"
    ma.inputs[1].default_value = (0.5, 0.5, 0.5)
    ma.inputs[2].default_value = (0.5, 0.5, 0.5)
    nt.links.new(geo.outputs["Normal"], vt.inputs["Vector"])
    nt.links.new(vt.outputs["Vector"], ma.inputs[0])
    return ma.outputs["Vector"]


MATS = {"ids": emission_material("AP_pass_ids", _ids), "depth": emission_material("AP_pass_depth", _depth),
        "normal": emission_material("AP_pass_normal", _normal)}


def slot_of(o):
    while o:
        if "ap_id" in o:
            return str(o["ap_id"])
        o = o.parent
    return None


meshes = [o for o in sc.objects if o.type == "MESH"]
preview_colors = {o.name: tuple(o.color) for o in meshes}
id_colors = {}
for o in meshes:
    c = colors.get(slot_of(o) or "", UNTAGGED)
    id_colors[o.name] = (c[0] / 255, c[1] / 255, c[2] / 255, 1.0)

world = bpy.data.worlds.new("AP_pass_world")
world.use_nodes = True
bg = world.node_tree.nodes["Background"]
sc.world = world

sc.render.engine = "CYCLES"
sc.cycles.device = "CPU"
sc.render.use_persistent_data = True
sc.render.dither_intensity = 0.0
sc.render.film_transparent = False
sc.render.resolution_percentage = 100
sc.render.image_settings.file_format = "PNG"

sun = bpy.data.objects.new("AP_sun", bpy.data.lights.new("AP_sun", "SUN"))
sun.data.energy = 3.5
sun.data.angle = math.radians(2)
sun.rotation_euler = (math.radians(50), 0, math.radians(35))


def data_settings():
    sc.cycles.samples = 1
    sc.cycles.use_adaptive_sampling = False
    sc.cycles.use_denoising = False
    sc.cycles.pixel_filter_type = "BOX"
    sc.cycles.filter_width = 0.01
    sc.cycles.max_bounces = 0
    sc.view_settings.view_transform = "Raw"
    sc.view_settings.look = "None"
    sc.view_settings.exposure = 0.0
    sc.view_settings.gamma = 1.0
    bg.inputs["Color"].default_value = (0, 0, 0, 1)
    bg.inputs["Strength"].default_value = 0.0
    if sun.name in sc.collection.objects:
        sc.collection.objects.unlink(sun)


def preview_settings():
    vl.material_override = None
    for o in meshes:
        o.color = preview_colors[o.name]
    sc.cycles.samples = preview_samples
    sc.cycles.use_denoising = True
    sc.cycles.pixel_filter_type = "BLACKMAN_HARRIS"
    sc.cycles.filter_width = 1.5
    sc.cycles.max_bounces = 4
    sc.view_settings.view_transform = "AgX"
    bg.inputs["Color"].default_value = (0.55, 0.68, 0.85, 1)
    bg.inputs["Strength"].default_value = 0.8
    if sun.name not in sc.collection.objects:
        sc.collection.objects.link(sun)


def render(path, color_mode="RGB", depth="8"):
    sc.render.image_settings.color_mode = color_mode
    sc.render.image_settings.color_depth = depth
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


cams = [o for o in sc.objects if o.type == "CAMERA" and "ap_shot" in o]
for v in json.load(open(opt("--virtual"))) if opt("--virtual") else []:
    cam = bpy.data.objects.new(v["id"], bpy.data.cameras.new(v["id"]))
    sc.collection.objects.link(cam)
    pos, look = Vector(v["pos"]), Vector(v["look_at"])
    cam.location = pos
    cam.rotation_euler = (look - pos).to_track_quat("-Z", "Y").to_euler()
    cam.data.lens, cam.data.sensor_width, cam.data.sensor_fit = v.get("lens_mm", 35), v.get("sensor_mm", 36), "HORIZONTAL"
    cam.data.clip_end = 20000.0
    cam["ap_shot"] = v["id"]
    cam["ap_res_x"], cam["ap_res_y"] = v.get("resolution", [1344, 768])
    cams.append(cam)
if want:
    cams = [c for c in cams if c["ap_shot"] in want]
    missing = want - {c["ap_shot"] for c in cams}
    if missing:
        raise SystemExit(f"no camera for shot(s): {', '.join(sorted(missing))}")

for cam in cams:
    shot = cam["ap_shot"]
    t = time.time()
    sc.camera = cam
    sc.render.resolution_x = int(cam.get("ap_res_x", 1344))
    sc.render.resolution_y = int(cam.get("ap_res_y", 768))
    d = f"{out_dir}/{shot}"
    if {"ids", "depth", "normal"} & set(passes):
        data_settings()
    if "ids" in passes:
        for o in meshes:
            o.color = id_colors[o.name]
        vl.material_override = MATS["ids"]
        render(f"{d}/ids.png")
    if "depth" in passes:
        vl.material_override = MATS["depth"]
        render(f"{d}/depth_raw.png", "BW", "16")
    if "normal" in passes:
        vl.material_override = MATS["normal"]
        render(f"{d}/normal.png")
    if "preview" in passes:
        preview_settings()
        render(f"{d}/preview.png")
    log(f"{shot}: {', '.join(passes)} in {time.time() - t:.1f}s")
