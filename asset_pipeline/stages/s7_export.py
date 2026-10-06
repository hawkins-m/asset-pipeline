"""World mode, phase 4: the greybox (and later the asset library) -> a UE import package.

The manifest is in Unreal coordinates (ue_coords: cm, left-handed, Blender Y mirrored), so
the importer only places what it's given. Layout under the project:

    export/manifest.json
    export/assets/<asset id>/SM_<asset id>.glb  (+ textures once real assets exist)
    export/terrain/heightmap.png               16-bit, for an optional manual UE Landscape

Stand-ins (`standins=True`, until phase 3 makes real assets): every greybox mesh becomes an
asset, so the whole importer path (Nanite, instancing, materials, cameras, sequence) can be
proven and the level laid out before any asset exists. Real assets will replace them by
asset id through the same manifest.

Manifest:
    assets  [{id, mesh, glb, faces, bounds_m, tint, textures}]
    slots   [{id, type, category, kit, district, label, folder, tags, transform,
              components: [{asset, instances: [transform relative to the slot]}]}]
    shots   [{id, tier, label, folder, tags, transform, focal_length_mm, sensor_width_mm,
              sensor_height_mm, resolution, notes}]
    sequence {name, fps, seconds_per_shot, shots}
    landscape {heightmap, location, scale, resolution}   for a manual Landscape import
Each transform is {"loc": [x, y, z] cm, "rot": [roll, pitch, yaw] deg, "scale": [x, y, z]}.
"""
import json
import re
import shutil
import time
from pathlib import Path

from .. import blender, config, ue_coords
from ..project import ProjectStore, read_json, write_json
from . import sw_site

EXPORT = "export"
SCRIPT = config.REPO_ROOT / "scripts" / "blender_export_meshes.py"
FPS = 24
SECONDS_PER_SHOT = 5
TIER_ORDER = {"wide": 0, "medium": 1, "tight": 2}
# Stand-in material tints (linear RGB) by slot type; real assets bring their own textures.
TINTS = {"terrain": (0.32, 0.30, 0.24), "sea": (0.06, 0.16, 0.24), "avenue": (0.55, 0.53, 0.50),
         "radial": (0.58, 0.56, 0.52), "plaza": (0.62, 0.60, 0.56), "garden": (0.16, 0.30, 0.10),
         "canal": (0.08, 0.22, 0.32), "pool": (0.10, 0.30, 0.40), "terrace": (0.50, 0.48, 0.42)}
WATER = {"pool-water", "court-pool", "canal", "sea"}
BUILDING_TINT = (0.80, 0.78, 0.72)


def export_dir(store: ProjectStore) -> Path:
    return store.root / EXPORT


def pascal(slug: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[^a-zA-Z0-9]+", slug) if p) or "Project"


def label(*parts: str) -> str:
    """UE actor label: AP_<part>_<part>, file-safe."""
    return "_".join(["AP"] + [re.sub(r"[^A-Za-z0-9-]+", "-", p) for p in parts if p])


def _tint(slot_type: str, piece: str) -> list[float]:
    if piece in WATER:
        return list(TINTS["pool"] if piece != "sea" else TINTS["sea"])
    return list(TINTS.get(slot_type, BUILDING_TINT))


def manifest(gb: dict, index: dict, layout=None, project: str = "") -> dict:
    """Build the manifest from greybox.json data and the mesh export index (pure)."""
    assets: dict[str, dict] = {}
    slots = []
    for s in gb["slots"]:
        comps: dict[str, list] = {}
        for p in s["pieces"]:
            entry = index.get(p["mesh"])
            if entry is None:
                raise ValueError(f"slot {s['id']}: mesh {p['mesh']} was not exported")
            aid = entry["id"]
            assets.setdefault(aid, {"id": aid, "mesh": p["mesh"], "glb": f"assets/{entry['glb']}",
                                    "faces": entry["faces"], "bounds_m": entry["bounds"],
                                    "tint": _tint(s["type"], p["piece"]), "textures": {}})
            comps.setdefault(aid, []).append(ue_coords.to_ue(p["matrix_local"]))
        district = s.get("district") or "site"
        tags = [f"ap:id={s['id']}", f"ap:type={s['type']}", "ap:source=pipeline"]
        tags += [f"ap:kit={s['kit']}"] * bool(s.get("kit")) + [f"ap:district={s['district']}"] * bool(s.get("district"))
        slots.append({"id": s["id"], "type": s["type"], "category": s["category"], "kit": s.get("kit"),
                      "district": s.get("district"), "label": label(district, s["id"]),
                      "folder": f"AP/{district}/{s['category']}", "tags": tags,
                      "transform": ue_coords.to_ue(s["matrix"]),
                      "components": [{"asset": a, "instances": inst} for a, inst in sorted(comps.items())]})
    shots = []
    for s in sorted(gb["shots"], key=lambda s: (TIER_ORDER.get(s["tier"], 9), s["id"])):
        w, h = s["resolution"]
        shots.append({"id": s["id"], "tier": s["tier"], "label": label("Shot", s["id"]), "folder": "AP/Shots",
                      "tags": [f"ap:shot={s['id']}", "ap:source=pipeline"],
                      "transform": ue_coords.camera_to_ue(s["matrix"]), "focal_length_mm": s["lens_mm"],
                      "sensor_width_mm": s["sensor_mm"], "sensor_height_mm": round(s["sensor_mm"] * h / w, 4),
                      "resolution": [w, h], "notes": s.get("notes", "")})
    name = pascal(project)
    out = {"version": 1, "project": project, "name": name, "source": "greybox-standins",
           "greybox_sha256": gb.get("blend_sha256"),
           "units": "UE: cm, left-handed, Z up (Blender Y mirrored; see asset_pipeline/ue_coords.py)",
           "assets": sorted(assets.values(), key=lambda a: a["id"]), "slots": slots, "shots": shots,
           "sequence": {"name": f"LS_{name}", "fps": FPS, "seconds_per_shot": SECONDS_PER_SHOT,
                        "shots": [s["id"] for s in shots]},
           "map": f"/Game/AP/Maps/{name}"}
    if layout is not None:
        t = layout.terrain
        lo, hi = t.z_range
        out["landscape"] = {
            "heightmap": "terrain/heightmap.png", "resolution": t.resolution,
            # heightmap row 0 is north = Blender +Y = UE -Y, so it starts at the UE minimum
            # corner; v = 32768 is the middle of the z range, 512 * scale.z cm spans it.
            "location": [-t.extent_m / 2 * 100, -t.extent_m / 2 * 100, (lo + hi) / 2 * 100],
            "scale": [t.extent_m * 100 / (t.resolution - 1)] * 2 + [round((hi - lo) * 100 / 512, 6)],
            "note": "Optional: Landscape mode > Import from File with these values. The importer "
                    "places the terrain as a Nanite mesh (UE's Python can't create a Landscape)."}
    return out


def counts(m: dict) -> dict:
    inst = sum(len(c["instances"]) for s in m["slots"] for c in s["components"])
    return {"assets": len(m["assets"]), "slots": len(m["slots"]), "instances": inst, "shots": len(m["shots"])}


def export(store: ProjectStore, standins: bool = True) -> dict:
    """greybox.blend -> export/ (GLBs + manifest.json). Returns the manifest's counts."""
    if not standins:
        raise NotImplementedError("real assets come with phase 3 (asset library); use stand-ins")
    if sw_site.stale(store):
        sw_site.extract(store)
    gb = sw_site.load_greybox(store)
    out = export_dir(store)
    t = time.time()
    shutil.rmtree(out / "assets", ignore_errors=True)
    (out / "assets").mkdir(parents=True)
    blender.run(SCRIPT, ["--out-dir", out / "assets", "--index", out / "mesh_index.json"],
                out / "blender.log", "stand-in export", blend=sw_site.blend_path(store))
    index = json.loads((out / "mesh_index.json").read_text())
    layout = sw_site.load_layout(store)
    m = manifest(gb, index, layout, store.load().slug)
    hm = sw_site.site_dir(store) / "terrain.png"
    if hm.is_file():
        (out / "terrain").mkdir(exist_ok=True)
        shutil.copyfile(hm, out / "terrain" / "heightmap.png")
    write_json(out / "manifest.json", m)
    c = counts(m)
    store.log_run({"stage": "export", "standins": standins, **c, "seconds": round(time.time() - t, 1)})
    return c


def load_manifest(store: ProjectStore) -> dict:
    m = read_json(export_dir(store) / "manifest.json")
    if m is None:
        raise FileNotFoundError("no export/manifest.json (run `ap export` first)")
    return m
