"""Vegetation data for world mode: street-tree slots on local streets and plazas, and the
vegetation zones of the library builds (building_parts.zone, written next to each GLB).

Nothing here places plants. Review renders draw proxies on the slots and zones; in Unreal a
PCG graph reads them (the import writes them as tagged data). The street-tree slots come from
the city plan with their own random stream (`CitySpec.street_trees.seed`), so the plan, the
greybox, the shot passes and locked frames never move when they change.

    ap site vegetation PROJECT [--preview SHOT ...]   site/vegetation.json (+ proxy previews)
"""
import math
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, box as sbox
from shapely.strtree import STRtree

from .project import ProjectStore, read_json, write_json

# greybox slot types that are open ground: trees may stand on them
OPEN_TYPES = ("plaza", "park", "garden", "street", "avenue", "radial", "lane", "stair", "terrain", "sea", "ring",
              "way", "aqueduct_park", "pergola_walk")
# plaza pieces trees keep clear of (basins, fountains, plinths), with their clearance in metres
PLAZA_OBSTACLES = {"pool-water": 16.0, "basin-kerb": 16.0, "plinth": 6.0, "fountain": 8.0}


def path(store: ProjectStore) -> Path:
    from .stages import sw_site
    return sw_site.site_dir(store) / "vegetation.json"


def _obstacles(P, gb: dict, clear: float):
    """Footprints trees keep clear of: buildings (typed footprints, plan boxes), civic slots,
    plaza basins and plinths, the existing (avenue, park, forum) trees."""
    from . import library_instances
    geoms = []
    for b in P.typed:
        x, y, _, w, d = library_instances.footprint(b)
        a = math.radians(b.rot)
        c, s = math.cos(a), math.sin(a)
        pts = [(x + u * w / 2 * c - v * d / 2 * s, y + u * w / 2 * s + v * d / 2 * c) for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        geoms.append(shapely.Polygon(pts).buffer(clear))
    for b in P.buildings + [s_ for _, st in P.markets for s_ in st] + P.piers:
        a = math.radians(b.rot)
        c, s = math.cos(a), math.sin(a)
        pts = [(b.x + u * b.w / 2 * c - v * b.d / 2 * s, b.y + u * b.w / 2 * s + v * b.d / 2 * c) for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
        geoms.append(shapely.Polygon(pts).buffer(clear))
    for sq, _ in P.markets:
        geoms.append(sq)
    for sl in gb.get("slots", []):
        if sl.get("typology") not in (None, "None") or sl.get("type") in OPEN_TYPES:
            pass
        elif sl.get("category") in ("building", "structure") and sl.get("type") not in ("pool",):
            (x0, y0, _), (x1, y1, _) = sl["bbox"]
            geoms.append(sbox(x0, y0, x1, y1).buffer(clear))
        m = np.asarray(sl["matrix"], float)
        for pc in sl.get("pieces", []):
            r = PLAZA_OBSTACLES.get(pc["piece"])
            if r:
                q = m @ np.asarray(pc["matrix_local"], float)
                geoms.append(Point(q[0, 3], q[1, 3]).buffer(r))
    for t in P.trees:
        geoms.append(Point(t[0], t[1]).buffer(max(clear, t[3] / 2)))
    return geoms


def street_tree_slots(store: ProjectStore) -> dict:
    """Tree slots along local streets (one row each side on wide streets, else one alternating
    row) and around each civic node's plaza (a ring `plaza_inset_m` inside its edge), spaced
    `spacing_m`, clear of buildings, other ways, basins and the plan's own trees. Returns the
    record written to site/vegetation.json."""
    from . import city
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    if layout.city is None:
        raise ValueError("street trees need a city layout (ap site init --city)")
    cfg = layout.city.street_trees
    heights = sw_site.plan_terrain(store, layout)
    P = city.plan(layout, heights)
    f = city.Fields(layout, heights)
    gb = sw_site.load_greybox(store) if (sw_site.site_dir(store) / "greybox.json").is_file() else {"slots": []}
    rng = np.random.default_rng(cfg.seed)
    tree = STRtree(_obstacles(P, gb, cfg.clear_m))
    ways = [w for w in P.ways]
    way_geoms = [w.line.buffer(w.width / 2 + 0.5) for w in ways]
    way_tree = STRtree(way_geoms)

    def free(x, y, own: int | None) -> bool:
        p = Point(x, y)
        if len(tree.query(p, predicate="intersects")):
            return False
        hits = [int(i) for i in way_tree.query(p, predicate="intersects")]
        return not [i for i in hits if i != own]

    def z_at(x, y):
        return float(f.z(x, y))

    slots = []
    if cfg.streets:
        for i, w in enumerate(ways):
            if w.kind != "street" or w.width < cfg.min_street_w:
                continue
            ln: LineString = w.line
            both = w.width >= cfg.both_sides_w
            for k, s in enumerate(np.arange(cfg.spacing_m / 2, ln.length - cfg.spacing_m / 2 + 1e-6, cfg.spacing_m)):
                p, q = ln.interpolate(s), ln.interpolate(min(s + 1.0, ln.length))
                t = np.array([q.x - p.x, q.y - p.y])
                n = np.array([-t[1], t[0]]) / max(np.linalg.norm(t), 1e-9)
                for side in ((-1, 1) if both else ((-1,) if k % 2 == 0 else (1,))):
                    x, y = np.array([p.x, p.y]) + side * n * (w.width / 2 - 1.2)
                    if free(x, y, i):
                        slots.append({"kind": "street", "way": w.id, "x": round(float(x), 2), "y": round(float(y), 2),
                                      "z": round(z_at(x, y), 2)})
    if cfg.plazas:
        for nd in P.layout.city.nodes:
            r = city._rings(nd)["plaza"] - cfg.plaza_inset_m
            if r < 8:
                continue
            n = max(4, int(2 * math.pi * r / cfg.spacing_m))
            a0 = float(rng.uniform(0, 2 * math.pi / n))
            for k in range(n):
                a = a0 + 2 * math.pi * k / n
                x, y = nd.center[0] + r * math.cos(a), nd.center[1] + r * math.sin(a)
                if free(x, y, None):
                    slots.append({"kind": "plaza", "node": nd.id, "x": round(x, 2), "y": round(y, 2), "z": round(z_at(x, y), 2)})
    for i, s in enumerate(slots):                      # stable ids, a crown size the PCG graph may override
        s["id"] = f"tree-{s['kind']}-{i:05d}"
        s["crown_m"] = round(float(rng.uniform(6.0, 9.0)), 1) if s["kind"] == "street" else round(float(rng.uniform(8.0, 12.0)), 1)
    rec = {"settings": cfg.model_dump(), "frame": "world metres, z up (the greybox's)",
           "counts": {"street": sum(1 for s in slots if s["kind"] == "street"), "plaza": sum(1 for s in slots if s["kind"] == "plaza")},
           "street_trees": slots}
    write_json(path(store), rec)
    return rec


def load(store: ProjectStore) -> dict | None:
    return read_json(path(store), default=None)


def proxies(store: ProjectStore) -> list[dict]:
    """Proxy trees for preview renders: the street-tree slots (a trunk and a round crown)."""
    rec = load(store) or {}
    return [{"x": s["x"], "y": s["y"], "z": s["z"], "w": s["crown_m"], "h": 0.9 * s["crown_m"] + 2.0}
            for s in rec.get("street_trees", [])]


def preview(store: ProjectStore, shots: list[str]) -> list[Path]:
    """The greybox from these shots' cameras with proxy trees on the street-tree slots (the
    preview pass only; the shot's data passes are never touched): site/vegetation/<shot>/preview.png."""
    from .stages import sw_shots, sw_site
    gb = sw_site.load_greybox(store)
    out = sw_site.site_dir(store) / "vegetation"
    work = sw_site.site_dir(store) / "build"
    write_json(work / "proxies.json", proxies(store))
    sw_shots._run(store, out, sw_shots.colors_for([s["id"] for s in gb["slots"]]), ["preview"], shots=shots,
                  what="vegetation preview", extra=["--proxies", work / "proxies.json"])
    return [out / s / "preview.png" for s in shots]


def export_points(store: ProjectStore, out: Path) -> dict:
    """Every vegetation point for Unreal (export/vegetation.json): the street-tree slots and the
    zones of each placed library build (3d/library/<entry>/instances), in world metres and as
    Unreal transforms (ue_coords: cm, Y mirrored). A PCG graph reads them through the actors
    `ap ue import --vegetation` places (TargetPoint, tags ap:veg=<type>, ap:size=<w>x<d>)."""
    from . import kit_proc, library_review
    from .ue_coords import to_ue
    approved = {a for a, r in library_review.reviews(store).items() if r.get("status") == "approved"}
    pts = []

    def add(kind, source, x, y, z, yaw, w, d, extra=None):
        c, s_ = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
        m = [[c, -s_, 0, x], [s_, c, 0, y], [0, 0, 1, z], [0, 0, 0, 1]]
        pts.append({"type": kind, "source": source, "x": round(x, 3), "y": round(y, 3), "z": round(z, 3),
                    "yaw": round(yaw, 2), "w": round(w, 2), "d": round(d, 2), "ue": to_ue(m)} | (extra or {}))
    for t in (load(store) or {}).get("street_trees", []):
        add(f"street_tree_{t['kind']}", t["id"], t["x"], t["y"], t["z"], 0.0, t["crown_m"], t["crown_m"])
    for idx in sorted((store.root / "3d" / "library").glob("*/instances/index.json")):
        entry = idx.parent.parent.name
        for i in read_json(idx, default=[]) or []:
            zf = kit_proc.zones_path(store.root / i["glb"])
            if not zf.is_file():
                continue
            a = math.radians(i["yaw"])
            c, s_ = math.cos(a), math.sin(a)
            for z in (read_json(zf, default={}) or {}).get("zones", []):
                zx, zy, zz = z["center"]
                add(z["type"], f"{entry}:{i['key']}", i["x"] + c * zx - s_ * zy, i["y"] + s_ * zx + c * zy, i["z"] + zz,
                    i["yaw"] + z["yaw"], z["size"][0], z["size"][1], {"approved": entry in approved})
    rec = {"frame": "world metres (Blender: z up); ue: Unreal cm, Y mirrored", "points": pts,
           "counts": {k: sum(1 for p in pts if p["type"] == k) for k in sorted({p["type"] for p in pts})}}
    write_json(out, rec)
    return rec


def zone_summary(store: ProjectStore) -> dict:
    """Vegetation zones per library build (3d/library/<asset>/procedural.zones.json): counts by type."""
    from . import kit_proc
    out = {}
    for z in sorted((store.root / "3d" / "library").glob("*/procedural.zones.json")):
        rec = read_json(z, default={}) or {}
        types = {}
        for r in rec.get("zones", []):
            types[r["type"]] = types.get(r["type"], 0) + 1
        out[z.parent.name] = types
    del kit_proc
    return out
