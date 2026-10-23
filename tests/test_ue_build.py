"""`ap ue build` pieces that run without Unreal: the prune count, the render GPU rule, the
report, and texture stripping (which keeps re-imports skippable: the same source gives the
same bytes, so the same content hash)."""
import json
import struct

import numpy as np

from asset_pipeline import city_library as cl, ue_build


def test_expected_prune_counts_pipeline_actors_the_manifest_no_longer_has():
    man = {"slots": [{"id": "a"}, {"id": "b"}], "shots": [{"id": "wide"}]}
    assert ue_build.expected_prune({"a": {}, "b": {}, "old": {}}, {"wide", "gone"}, man) == 2
    assert ue_build.expected_prune({}, set(), man) == 0


def test_renders_use_gpu_1_only_while_comfyui_is_down():
    gpu, why = ue_build.render_gpu(True, {0: 0.5, 1: 0.5})
    assert gpu is None and "ComfyUI is running on GPU 1" in why and "GPU 0" in why
    assert ue_build.render_gpu(False, {0: 0.5, 1: 9.0})[0] is None
    gpu, why = ue_build.render_gpu(False, {0: 3.0, 1: 0.5})
    assert gpu == 1 and why.startswith("rendering on GPU 1")


def test_report_is_short_and_lists_warnings():
    r = {"ok": True, "placed": 555, "attached": 0, "slots_replaced": 38, "greybox_slots": 470, "held": 121,
         "imported": 3, "skipped": 900, "reset": 2, "pruned": 1, "terrain_outer": True, "shots": None,
         "steps": {"export": 20.0, "import": 300.0}, "seconds": 330.0, "warnings": ["no Landscape in the level"]}
    lines = ue_build.report_lines(r)
    assert lines[0] == "OK: 555 library builds placed in 38 slots; 470 slots still greybox, 121 builds held back (slot not fully built or not approved)"
    assert "3 imported, 900 unchanged" in lines[1] and lines[2].startswith("time: 5.5 min")
    assert lines[-1] == "warning: no Landscape in the level" and len(lines) == 4


def _glb(with_image=True):
    """A one-triangle GLB with a base colour texture embedded (bufferView 1)."""
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], np.float32).tobytes()
    png = b"\x89PNG fake image bytes" + b"\0" * 7
    js = {"asset": {"version": "2.0"}, "buffers": [{"byteLength": len(pos) + len(png)}],
          "bufferViews": [{"buffer": 0, "byteOffset": len(png), "byteLength": len(pos)}] if not with_image else
          [{"buffer": 0, "byteOffset": 0, "byteLength": len(png)}, {"buffer": 0, "byteOffset": len(png), "byteLength": len(pos)}],
          "accessors": [{"bufferView": 1 if with_image else 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
          "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "material": 0}]}],
          "materials": [{"name": "wall", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1],
                                                                 **({"baseColorTexture": {"index": 0}} if with_image else {})}}]}
    if with_image:
        js |= {"images": [{"bufferView": 0, "mimeType": "image/png"}], "textures": [{"source": 0, "sampler": 0}],
               "samplers": [{}], "extensionsUsed": ["KHR_texture_transform"]}
    binb = (png if with_image else b"") + pos
    jb = json.dumps(js).encode()
    jb += b" " * (-len(jb) % 4)
    binb += b"\0" * (-len(binb) % 4)
    total = 12 + 8 + len(jb) + 8 + len(binb)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(jb), 0x4E4F534A) + jb
            + struct.pack("<II", len(binb), 0x004E4942) + binb)


def _parts(glb):
    n = struct.unpack_from("<I", glb, 12)[0]
    js = json.loads(glb[20:20 + n])
    off = 20 + n
    return js, glb[off + 8:off + 8 + struct.unpack_from("<I", glb, off)[0]]


def test_stripping_drops_the_images_and_keeps_geometry_and_material_names():
    out = cl.strip_textures(_glb())
    js, binb = _parts(out)
    assert not {"images", "textures", "samplers", "extensionsUsed"} & set(js)
    assert js["materials"] == [{"name": "wall", "pbrMetallicRoughness": {"baseColorFactor": [1, 1, 1, 1]}}]
    v = js["bufferViews"][js["accessors"][0]["bufferView"]]
    pts = np.frombuffer(binb[v["byteOffset"]:v["byteOffset"] + v["byteLength"]], np.float32).reshape(3, 3)
    assert pts.tolist() == [[0, 0, 0], [1, 0, 0], [0, 1, 0]] and len(binb) == 36
    assert struct.unpack_from("<I", out, 8)[0] == len(out)


def test_stripping_is_deterministic_so_an_unchanged_build_is_skipped_on_import():
    assert cl.strip_textures(_glb()) == cl.strip_textures(_glb())
    assert cl.strip_textures(cl.strip_textures(_glb())) == cl.strip_textures(_glb())    # nothing left to strip
