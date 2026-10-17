"""Per-instance library builds (city mode): a procedural entry built at each of its buildings'
own footprint and storeys, as the city plan places them, instead of one representative build
stretched over every slot. Each build lands in 3d/library/<entry>/instances/<key>.glb with an
index (index.json) of where it stands: centre, base height and yaw in Blender world metres.
"""
import math

import numpy as np

from . import building_proc, kit_proc
from .building_gen import features
from .project import ProjectStore, read_json, write_json

SKIP_ROLES = {"roof", "cornice", "roof-garden", "tree", "vault", "crown"}


def entry_of(b, entries: set[str]) -> str | None:
    """The library entry a typed building belongs to: <typology>-<material>, else <typology>."""
    t = b.typology.replace("_", "-")
    for k in ((f"{t}-{b.material}" if b.material else None), t):
        if k and k in entries:
            return k
    return None


def footprint(b) -> tuple[float, float, float, float, float]:
    """A typed building's centre (x, y), base z and size (w along its own x, d) from its massing
    parts, in its own frame (yaw b.rot)."""
    ca, sa = math.cos(math.radians(b.rot)), math.sin(math.radians(b.rot))
    pts, zs = [], []
    for p in b.parts:
        if not isinstance(p.prim, str) or p.role in SKIP_ROLES:
            continue
        c, s = math.cos(math.radians(p.rot)), math.sin(math.radians(p.rot))
        for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            x = p.x + u * p.w / 2 * c - v * p.d / 2 * s
            y = p.y + u * p.w / 2 * s + v * p.d / 2 * c
            pts.append(((x - b.x) * ca + (y - b.y) * sa, -(x - b.x) * sa + (y - b.y) * ca))
        zs.append(p.z)
    if not pts:
        return b.x, b.y, 0.0, 10.0, 10.0
    a = np.array(pts)
    lo, hi = a.min(0), a.max(0)
    mx, my = (lo + hi) / 2
    return (b.x + mx * ca - my * sa, b.y + mx * sa + my * ca, float(min(zs)), float(hi[0] - lo[0]), float(hi[1] - lo[1]))


def storey_h(store: ProjectStore, entry: str) -> float:
    brief = read_json(kit_proc.spec_path(store, entry))
    return float((brief.get("params") or {}).get("storey_h") or features(brief["form"], brief.get("features")).get("storey_h", 3.4))


def build(store: ProjectStore, entry: str, buildings: list, storeys_range: tuple[int, int] | None = None,
          log=print, skip_fresh: bool = False) -> list[dict]:
    """Build entry at each building's own footprint; storeys clamped to storeys_range. Returns
    the index (also written to instances/index.json, after each build: a crash loses one).
    skip_fresh: keep instances built from the same brief at the same size (a resumed batch)."""
    from .areas import brief_sha
    d = kit_proc.out_dir(store, entry) / "instances"
    d.mkdir(parents=True, exist_ok=True)
    sh = storey_h(store, entry)
    sha = brief_sha(store, entry)
    old = {i["key"]: i for i in read_json(d / "index.json", default=[]) or []}
    index = []
    for b in buildings:
        x, y, z0, w, dd = footprint(b)
        st = b.storeys if storeys_range is None else int(np.clip(b.storeys, *storeys_range))
        key = f"{entry}_{round(x)}_{round(y)}".replace("-", "m")
        glb = d / f"{key}.glb"
        dims = [round(w, 2), round(dd, 2), round(st * sh, 2)]
        if not (skip_fresh and glb.is_file() and old.get(key, {}).get("brief_sha") == sha and old[key].get("dims") == dims):
            building_proc.build(store, entry, log=lambda *a: None, out=glb, dims=dims, maps=d / "maps")
        index.append({"key": key, "glb": glb.relative_to(store.root).as_posix(), "x": round(x, 3), "y": round(y, 3),
                      "z": round(z0, 3), "yaw": round(b.rot, 3), "w": round(w, 2), "d": round(dd, 2), "storeys": st,
                      "storeys_plan": b.storeys, "typology": b.typology, "dims": dims, "brief_sha": sha})
        write_json(d / "index.json", list((old | {i["key"]: i for i in index}).values()))      # resumable
        log(f"{entry}: {key} {w:.0f} x {dd:.0f} m, {st} storeys")
    return index


def build_entries(store: ProjectStore, entries: list[str], near: tuple[float, float] | None = None,
                  radius: float = 600.0, log=print, skip_fresh: bool = False) -> dict[str, list[dict]]:
    """Per-instance builds of entries from the current city plan: every typed building that
    belongs to each entry, or only those within radius of near (x, y)."""
    from . import city
    from .stages import sw_site
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.plan_terrain(store, layout))
    have = {p.stem for p in (store.root / "library/kits").glob("*.json")}
    out = {}
    for e in entries:
        if e not in have:
            raise FileNotFoundError(f"{e}: no brief in library/kits/")
        bs = [b for b in P.typed if entry_of(b, have) == e
              and (near is None or math.hypot(b.x - near[0], b.y - near[1]) <= radius)]
        out[e] = build(store, e, bs, log=log, skip_fresh=skip_fresh)
    return out
