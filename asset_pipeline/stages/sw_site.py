"""World mode, site: layout.json -> terrain heightmap -> greybox.blend -> greybox.json.

The layout generates the greybox; after that the .blend is the truth (hand edits in
Blender) until the UE import, after which the UE level is (PLAN.md "World mode").
greybox.json is always read back from the .blend, never from the layout.

Layout under the project:
    site/layout.json             SiteLayout (edit, then `ap site build`)
    site/terrain.png + .json     16-bit heightmap (rows north -> south) and its z range / size
    site/greybox.blend           built, then hand-edited (.prev.blend: the one before a rebuild)
    site/greybox.json            slots + shots + untagged meshes, read from the .blend
    site/build.json              sha256 of the .blend as last built or written by the pipeline
    site/build/                  spec.json + terrain.npy handed to Blender, blender.log
    site/city.json + city_plan.png   city mode: the plan's stats and a top-down plan
"""
import hashlib
import json
import math
import shutil
import time
from pathlib import Path

import numpy as np
from PIL import Image

from .. import blender, city, config, greybox
from ..project import ProjectStore, read_json, write_json
from ..schema import (CitySpec, CivicNode, District, Kit, Plaza, Plot, Radial, Ring, RingRow, SiteLayout,
                      Shot, ShotSpec, Slot, TerrainSpec)

SITE = "site"
SCRIPT = config.REPO_ROOT / "scripts" / "blender_greybox.py"
BLENDER_TERRAIN_MAX = 337   # samples per side of the greybox terrain mesh (~7 m on 2.4 km)
BLENDER_TERRAIN_STEP_M = 8.0  # finer than this isn't needed; larger sites get more samples
BLENDER_TERRAIN_CAP = 1009


class SiteEdited(ValueError):
    """greybox.blend was edited by hand; rebuilding from the layout would replace it."""


def site_dir(store: ProjectStore) -> Path:
    return store.root / SITE


def blend_path(store: ProjectStore) -> Path:
    return site_dir(store) / "greybox.blend"


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_layout(store: ProjectStore) -> SiteLayout:
    data = read_json(site_dir(store) / "layout.json")
    if data is None:
        raise FileNotFoundError(f"no site/layout.json in {store.root} (run `ap site init`)")
    return SiteLayout.model_validate(data)


def save_layout(store: ProjectStore, layout: SiteLayout) -> None:
    write_json(site_dir(store) / "layout.json", layout.model_dump(mode="json"))


def starter_layout() -> SiteLayout:
    """A small neutral layout to start from (one of each element; no project content).
    Real layouts live in the project folder, never in this repo."""
    return SiteLayout(
        name="starter",
        districts=[District(id="centre", radius=(0, 80)), District(id="outer", radius=(80, 1e9))],
        kits=[Kit(id="colonnade", module_m=4, storey_m=6, column_d_m=0.9, ring_radii=[22, 110])],
        rings=[Ring(id="inner", radius=60, width=12), Ring(id="green", radius=125, width=20, role="garden")],
        radials=[Radial(id=f"r{a:03d}", angle=a, width=16, r_from=45, r_to=400) for a in (0, 90, 180, 270)],
        plazas=[Plaza(id="centre", radius=45)],
        plots=[Plot(id="rotunda", type="rotunda", size=(36, 36, 36), kit="colonnade"),
               Plot(id="stoa-a", type="stoa", arc=(110, 20, 70), kit="colonnade")],
        rows=[RingRow(id="block", radius=180, count=8, angle_offset=45, type="block", size=(30, 20, 16),
                      kit="colonnade"),
              RingRow(id="villa", radius=280, count=16, angle_offset=11.25, type="villa", size=(34, 34, 8),
                      kit="colonnade")],
        shots=[ShotSpec(id="wide", tier="wide", pos=(-600, -500, 300), look_at=(0, 0, 0), lens_mm=28),
               ShotSpec(id="street", pos=(0, -52, 19.7), look_at=(0, 0, 28), lens_mm=24)])


def starter_city() -> SiteLayout:
    """A neutral city-mode layout (no project content): a coast, four civic nodes of
    different sizes, one monument."""
    return SiteLayout(
        name="starter city",
        terrain=TerrainSpec(extent_m=8000, resolution=2017, shore_m=1400, headland_m=150, headland_width_m=900,
                            hill_height_m=180, hill_from_m=300, hill_to_m=3200, coast_amp_m=160,
                            coast_wavelength_m=2600, city_radius_m=0, z_range=(-60, 400)),
        kits=[Kit(id="civic", module_m=4, storey_m=6, column_d_m=0.9)],
        city=CitySpec(nodes=[
            CivicNode(id="centre", center=(0, -600), radius=300, role="forum", monument="rotunda", radials=8,
                      spiral_deg_per_100m=4, weight=1.3, avenue_m=1800),
            CivicNode(id="harbour", center=(-1500, -1120), radius=170, role="harbour", radials=5, rot=20),
            CivicNode(id="market", center=(1500, -500), radius=150, role="market", radials=6, rot=10, weight=0.8),
            CivicNode(id="hill", center=(-600, 750), radius=200, role="hill", radials=6, weight=0.9)],
            bounds=(-2500, -1500, 2500, 1500)))


def city_shots(layout: SiteLayout) -> list[ShotSpec]:
    """Auto-placed city-mode shots for a layout (terrain generated as the build would)."""
    t = layout.terrain
    h = greybox.make_terrain(t) if not t.heightmap else None
    if h is None:
        raise ValueError("city_shots needs a generated terrain")
    return city.shots(layout, city.apply_pads(layout, h))


def plan_city(store: ProjectStore) -> dict:
    """City mode without Blender: site/city_plan.png (top-down plan) and site/city.json
    (area shares, counts, warnings). Returns the stats."""
    layout = load_layout(store)
    if layout.city is None:
        raise ValueError("the layout has no city spec (ap site init --city, or add \"city\" to layout.json)")
    h = terrain(store, layout)
    P = city.plan(layout, h)
    city.plan_image(P, h).save(site_dir(store) / "city_plan.png")
    write_json(site_dir(store) / "city.json", P.stats)
    return P.stats


USER_SHOT_FIELDS = ("prompt_append", "prompt_override", "nudge_pos", "nudge_target", "lens_override",
                    "tier_override")


def set_city_shots(store: ProjectStore) -> list[ShotSpec]:
    """Replace the layout's shots with the auto-placed city shots (rebuild to apply). The
    user's edits to a shot (prompt, nudges, lens, tier) carry over by id."""
    layout = load_layout(store)
    old = {s.id: s for s in layout.shots}
    shots = city_shots(layout)
    for s in shots:
        if s.id in old:
            for k in USER_SHOT_FIELDS:
                setattr(s, k, getattr(old[s.id], k))
    layout.shots = shots
    save_layout(store, layout)
    return layout.shots


def import_catalog(store: ProjectStore, path: Path) -> SiteLayout:
    """Merge a typology catalog YAML (typologies, districts, materials, overlays, checks)
    into site/layout.json."""
    from .. import catalog
    layout = catalog.apply(load_layout(store), catalog.load(path))
    save_layout(store, layout)
    return layout


def export_catalog(store: ProjectStore) -> str:
    from .. import catalog
    return catalog.dump(load_layout(store))


def init(store: ProjectStore, layout_file: Path | None = None, force: bool = False,
         city_mode: bool = False) -> SiteLayout:
    """Copy a layout (default: the neutral starter, or with city_mode the starter city) into
    the project's site/ and switch the project to world mode. A city layout without shots
    gets the auto-placed ones."""
    dst = site_dir(store) / "layout.json"
    if dst.exists() and not force:
        raise FileExistsError(f"{dst} exists (use force to replace it)")
    layout = (SiteLayout.model_validate(json.loads(Path(layout_file).read_text())) if layout_file
              else starter_city() if city_mode else starter_layout())
    if layout.city is not None and not layout.shots:
        layout.shots = city_shots(layout)
    save_layout(store, layout)
    project = store.load()
    project.mode = "world"
    store.save(project)
    return layout


def edited(store: ProjectStore) -> bool:
    """Has greybox.blend changed since the pipeline last wrote it?"""
    b = blend_path(store)
    rec = read_json(site_dir(store) / "build.json", default={}) or {}
    return b.is_file() and rec.get("sha256") != _sha(b)


def _record(store: ProjectStore, **extra) -> None:
    rec = read_json(site_dir(store) / "build.json", default={}) or {}
    rec.update(extra, sha256=_sha(blend_path(store)))
    write_json(site_dir(store) / "build.json", rec)


# --- terrain -----------------------------------------------------------------------------

def terrain(store: ProjectStore, layout: SiteLayout) -> np.ndarray:
    """Generate (or load) the heightmap, write site/terrain.png + .json; returns metres."""
    t = layout.terrain
    out = site_dir(store)
    if t.heightmap:
        a = np.array(Image.open(store.root / t.heightmap))
        if a.ndim != 2 or a.shape[0] != a.shape[1]:
            raise ValueError(f"{t.heightmap}: expected a square single-channel 16-bit PNG")
        h = greybox.from_png16(a, t.z_range)
    else:
        h = greybox.make_terrain(t)
    if layout.city is not None:
        h = city.apply_pads(layout, h)
    lo, hi = float(h.min()), float(h.max())
    if lo < t.z_range[0] or hi > t.z_range[1]:
        raise ValueError(f"terrain spans {lo:.1f}..{hi:.1f} m, outside z_range {t.z_range}: widen it")
    Image.fromarray(greybox.to_png16(h, t.z_range)).save(out / "terrain.png")
    write_json(out / "terrain.json", {"extent_m": t.extent_m, "resolution": int(h.shape[0]),
                                      "z_range": list(t.z_range), "min_z": round(lo, 3), "max_z": round(hi, 3),
                                      "rows": "north (+Y) -> south"})
    return h


def _downsample(h: np.ndarray, max_n: int) -> np.ndarray:
    step = max(1, math.ceil((h.shape[0] - 1) / (max_n - 1)))
    return h[::step, ::step]


# --- build / extract ---------------------------------------------------------------------

def build(store: ProjectStore, force: bool = False) -> dict:
    """layout.json -> greybox.blend -> greybox.json. Refuses to replace a hand-edited
    .blend unless force (the old one is kept as greybox.prev.blend either way)."""
    layout = load_layout(store)
    b = blend_path(store)
    if edited(store) and not force:
        raise SiteEdited(f"{b} was edited since it was built; rebuilding replaces those edits "
                         f"(use force; the old file is kept as greybox.prev.blend)")
    t = time.time()
    h = terrain(store, layout)
    spec = greybox.build_spec(layout, h)
    work = site_dir(store) / "build"
    work.mkdir(parents=True, exist_ok=True)
    small = _downsample(h, max(BLENDER_TERRAIN_MAX, min(BLENDER_TERRAIN_CAP,
                                                        math.ceil(layout.terrain.extent_m / BLENDER_TERRAIN_STEP_M) + 1)))
    np.save(work / "terrain.npy", small)
    spec["terrain"]["npy"] = str(work / "terrain.npy")
    spec["terrain"]["resolution"] = int(small.shape[0])
    if spec.get("city"):
        write_json(site_dir(store) / "city.json", spec["city"])
        city.plan_image(city.plan(layout, h), h).save(site_dir(store) / "city_plan.png")
    write_json(work / "spec.json", spec)
    if b.exists():
        shutil.copyfile(b, b.with_suffix(".prev.blend"))
    tmp = b.with_name(".greybox.tmp.blend")
    tmp.unlink(missing_ok=True)
    blender.run(SCRIPT, ["--build", work / "spec.json", "--out", tmp], work / "blender.log", "greybox build")
    if not tmp.is_file():
        raise RuntimeError(f"blender wrote no greybox (see {work / 'blender.log'})")
    tmp.replace(b)
    _record(store, built=time.strftime("%Y-%m-%dT%H:%M:%S"))
    data = extract(store)
    store.log_run({"stage": "site.build", "slots": len(data["slots"]), "shots": len(data["shots"]),
                   "pieces": sum(len(s["pieces"]) for s in data["slots"]), "seconds": round(time.time() - t, 1)})
    return data


def extract(store: ProjectStore) -> dict:
    """Read the tagged objects of greybox.blend into site/greybox.json."""
    b = blend_path(store)
    if not b.is_file():
        raise FileNotFoundError(f"no {b} (run `ap site build`)")
    was_edited = edited(store)
    work = site_dir(store) / "build"
    out = work / "extract.json"
    out.unlink(missing_ok=True)
    blender.run(SCRIPT, ["--extract", out], work / "blender.log", "greybox extract", blend=b)
    if not out.is_file():
        raise RuntimeError(f"blender wrote no extract (see {work / 'blender.log'})")
    data = json.loads(out.read_text())
    if data["fixed"] and not was_edited:
        _record(store)  # the pipeline's own fixes don't count as hand edits
    data["slots"] = [Slot.model_validate(s).model_dump(mode="json") for s in data["slots"]]
    data["shots"] = [Shot.model_validate(s).model_dump(mode="json") for s in data["shots"]]
    data["blend_sha256"] = _sha(b)
    data["geometry_sha256"] = geometry_sha(data)
    write_json(site_dir(store) / "greybox.json", data)
    return data


def geometry_sha(data: dict) -> str:
    """Hash of the slots alone (not the cameras): what every shot's passes depend on."""
    return hashlib.sha256(json.dumps(data["slots"], sort_keys=True).encode()).hexdigest()


def load_greybox(store: ProjectStore) -> dict:
    data = read_json(site_dir(store) / "greybox.json")
    if data is None:
        raise FileNotFoundError("no site/greybox.json (run `ap site build`)")
    return data


def stale(store: ProjectStore) -> bool:
    """greybox.json no longer matches the .blend (run extract)."""
    b = blend_path(store)
    data = read_json(site_dir(store) / "greybox.json", default=None)
    return bool(b.is_file() and (not data or data.get("blend_sha256") != _sha(b)))


def add_shot(store: ProjectStore, shot: ShotSpec) -> dict:
    """Add or replace a shot camera in the .blend, and in the layout (so a rebuild keeps it)."""
    layout = load_layout(store)
    layout.shots = [s for s in layout.shots if s.id != shot.id] + [shot]
    save_layout(store, layout)
    b = blend_path(store)
    if not b.is_file():
        raise FileNotFoundError(f"no {b} (run `ap site build`)")
    was_edited = edited(store)
    work = site_dir(store) / "build"
    path = work / f"shot_{shot.id}.json"
    write_json(path, shot.model_dump(mode="json"))
    blender.run(SCRIPT, ["--add-shot", path], work / "blender.log", f"add shot {shot.id}", blend=b)
    if not was_edited:
        _record(store)
    return extract(store)


def summary(store: ProjectStore) -> dict:
    data = load_greybox(store)
    types: dict[str, int] = {}
    pieces: dict[str, int] = {}
    for s in data["slots"]:
        types[s["type"]] = types.get(s["type"], 0) + 1
        for p in s["pieces"]:
            pieces[p["piece"]] = pieces.get(p["piece"], 0) + 1
    return {"slots": len(data["slots"]), "types": dict(sorted(types.items())),
            "pieces": dict(sorted(pieces.items())), "shots": [s["id"] for s in data["shots"]],
            "untagged": data["untagged"], "edited": edited(store), "stale": stale(store)}
