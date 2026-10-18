"""Look scenes (Unreal): a shot rebuilt with its library builds, light, ground surfaces and a far
backdrop, matched to its locked frame, in a level of its own (the city level is never touched).

Presets, merged key by key (dicts one level down):
    DEFAULTS (here)  <  site/look_tests/defaults.json (the project's)  <  site/look_tests/<name>.json
A shot preset needs only {"shot": id} and what differs for it (sun, exposure box, backdrop...).
Each run appends its result to the preset's "history" (export/look/<name>/ holds the renders).

What comes from the frame (the shot's locked frame and its class map from the reconcile,
site/reconcile/<shot>/classes.png, frame_plan.COLOURS):
    tints   "frame" for paving / water / lawn / gravel / backdrop: the median colour of that
            class (paving: inside the exposure box), as a linear albedo
    ridge   backdrop {"from_frame": true}: the top of the frame's hill class per column (hidden
            stretches interpolated), turned into a crest line at the backdrop distance
    exposure {"box": [x0, y0, x1, y1]}: the render's mean there set to the frame's
Placements: every per-instance library build (3d/library/<entry>/instances/index.json) of a
building still in the plan, plus entries built for kit slots (library.json slot_ids) at their
slot, within radius of the camera and in view.
"""
import copy
import hashlib
import math
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import frame_plan
from .project import ProjectStore, read_json, write_json

DEFAULTS = {
    "radius": 600.0,
    "render_gpu": "auto",                 # auto: GPU 1 when ComfyUI is down (heavy scenes lose GPU 0)
    "gate": True,                         # place only builds Approved in library review
    "greybox": [0.5, 0.5, 0.5],           # the massing left for everything else: neutral matte grey
    "sun": {"elevation": 20.0, "offset": 30.0, "temperature": 5800.0, "lux": 10.0},
    "sky": {"intensity": 1.0, "color": [1.0, 1.0, 1.0]},
    "white_balance": 6500.0,              # the camera's white point (K): 6500 is neutral in Unreal
    "fog": {"density": 0.01, "falloff": 0.06, "start_cm": 20000.0, "color": [0.42, 0.47, 0.56]},
    "exposure": None,                     # {"box": [x0, y0, x1, y1]} in frame pixels
    # ground surfaces, world-aligned (the greybox has no UVs); tint "frame" or linear [r, g, b]
    # (albedo: the luminance a frame tint is scaled to)
    "paving": {"slab_m": [1.2, 0.6], "joint_mm": 8.0, "roughness": 0.3, "variation": 0.06, "tint": "frame",
               "albedo": 0.55},
    "water": {"roughness": 0.03, "ripple_m": 2.0, "normal_strength": 0.35, "tint": "frame", "albedo": 0.08,
              "lit": 1.0},
    "lawn": {"roughness": 0.85, "grain_m": 0.6, "tint": "frame", "albedo": 0.12},
    "gravel": {"roughness": 0.9, "grain_m": 0.25, "tint": "frame", "albedo": 0.3},
    "backdrop_surface": {"roughness": 0.95, "grain_m": 40.0, "tint": "frame", "albedo": 0.06, "normal_strength": 0.6},
    "backdrop": None,                     # {"from_frame": true, "dist": [near, far]} or {"bearing", "spread", ...}
}
FALLBACK_TINT = {"paving": [0.6, 0.58, 0.54], "water": [0.05, 0.14, 0.15], "lawn": [0.1, 0.2, 0.06],
                 "gravel": [0.42, 0.38, 0.32], "backdrop": [0.06, 0.08, 0.05]}
LUMA = np.array([0.2126, 0.7152, 0.0722])
CLASS_OF = {"paving": "paving", "water": "water", "lawn": "grass", "gravel": "gravel", "backdrop": "hill"}
SURFACES = {"paving": "paving", "water": "water", "lawn": "lawn", "gravel": "gravel", "backdrop": "backdrop_surface"}


def merged(base: dict, over: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = ({**out[k], **v} if isinstance(out.get(k), dict) and isinstance(v, dict) else copy.deepcopy(v))
    return out


def preset_dir(store: ProjectStore) -> Path:
    return store.root / "site" / "look_tests"


def preset(store: ProjectStore, name: str) -> dict:
    """The resolved preset of look scene `name` (DEFAULTS < defaults.json < <name>.json)."""
    own = read_json(preset_dir(store) / f"{name}.json", default=None)
    if not own or "shot" not in own:
        raise FileNotFoundError(f"{preset_dir(store) / (name + '.json')}: no look preset with a \"shot\"")
    proj = read_json(preset_dir(store) / "defaults.json", default={}) or {}
    p = merged(merged(DEFAULTS, proj), {k: v for k, v in own.items() if k != "history"})
    return p


def srgb_to_linear(c):
    c = np.asarray(c, float) / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


# --- the frame ------------------------------------------------------------------------------

def frame_of(store: ProjectStore, shot: dict) -> Path | None:
    return store.root / shot["locked"] if shot.get("locked") else None


def frame_classes(store: ProjectStore, shot_id: str, size: tuple[int, int]) -> np.ndarray | None:
    """The locked frame's class map (frame_plan.CLASSES indices) at size, or None."""
    p = store.root / "site" / "reconcile" / shot_id / "classes.png"
    if not p.exists():
        return None
    im = Image.open(p).convert("RGB").resize(size, Image.NEAREST)
    return frame_plan.decode(np.asarray(im))


def frame_tint(img: np.ndarray, cls: np.ndarray | None, kind: str, box=None, albedo: float | None = None,
               lit: float = 0.4) -> list[float] | None:
    """A class's colour in the frame as a linear [r, g, b] (None when the frame has too little of
    it): the median of its sunlit pixels (the brightest `lit` share: shadows carry the sky's blue;
    1 for water, whose brightest pixels are reflections), scaled
    to luminance `albedo` when given (the frame's brightness is light, not surface). box limits it
    to a frame region (x0, y0, x1, y1)."""
    if cls is None:
        return None
    m = cls == frame_plan.CLASSES.index(CLASS_OF[kind])
    if box:
        b = np.zeros_like(m)
        x0, y0, x1, y1 = box
        b[y0:y1, x0:x1] = True
        m = m & b if (m & b).sum() >= 200 else m
    if m.sum() < 200:
        return None
    lin = srgb_to_linear(img[m])
    lum = lin @ LUMA
    lin = np.median(lin[lum >= np.percentile(lum, 100 * (1 - lit))], axis=0)
    if albedo:
        lin = lin * albedo / max(float(lin @ LUMA), 1e-4)
    return [round(float(min(v, 0.9)), 4) for v in lin]


def ridge_from_frame(cls: np.ndarray, matrix, lens_mm: float, sensor_mm: float, dist: float,
                     cam_z: float, step: int = 8) -> list[list[float]]:
    """The frame's hill crest as [[bearing (deg, Blender: atan2(y, x)), height z (m)], ...] at
    distance `dist` from the camera: per column, the first non-sky row where it is hill; columns
    where something nearer hides the crest take the line between their visible neighbours, and
    the ends hold their last value."""
    h, w = cls.shape
    sky, hill = frame_plan.CLASSES.index("sky"), frame_plan.CLASSES.index("hill")
    m = np.asarray(matrix, float)
    f = lens_mm / sensor_mm * w
    xs = np.arange(0, w, step)
    rows = np.full(len(xs), np.nan)
    for i, x in enumerate(xs):
        col = cls[:, x]
        top = int(np.argmax(col != sky)) if (col != sky).any() else h
        if top < h and col[top] == hill:
            rows[i] = top
    ok = ~np.isnan(rows)
    if ok.sum() < 3:
        return []
    rows = np.interp(np.arange(len(xs)), np.nonzero(ok)[0], rows[ok])
    out = []
    for x, r in zip(xs, rows):
        d = m[:3, :3] @ np.array([(x + 0.5 - w / 2) / f, (h / 2 - r) / f, -1.0])
        d /= np.linalg.norm(d)
        hz = math.hypot(d[0], d[1])
        out.append([round(math.degrees(math.atan2(d[1], d[0])), 3), round(cam_z + dist * d[2] / hz, 1)])
    return out


# --- placements -----------------------------------------------------------------------------

def _yaw(matrix) -> float:
    m = np.asarray(matrix, float)
    return math.degrees(math.atan2(m[1, 0], m[0, 0]))


def in_view(shot: dict, pts: np.ndarray, margin: float = 0.15) -> bool:
    """Whether any of the world points projects into the shot's frame (with a margin)."""
    m = np.asarray(shot["matrix"], float)
    w, h = shot["resolution"]
    f = shot["lens_mm"] / shot["sensor_mm"] * w
    c = (pts - m[:3, 3]) @ m[:3, :3]
    z = -c[:, 2]
    ok = z > 0.5
    if not ok.any():
        return False
    u = w / 2 + f * c[ok, 0] / z[ok]
    v = h / 2 - f * c[ok, 1] / z[ok]
    return bool(((u > -margin * w) & (u < (1 + margin) * w) & (v > -margin * h) & (v < (1 + margin) * h)).any())


def _box_pts(x, y, z, w, d, yaw, hgt=20.0) -> np.ndarray:
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    pts = []
    for u, v in ((-1, -1), (1, -1), (1, 1), (-1, 1), (0, 0)):
        px, py = x + u * w / 2 * c - v * d / 2 * s, y + u * w / 2 * s + v * d / 2 * c
        pts += [(px, py, z), (px, py, z + hgt)]
    return np.array(pts)


def approved(store: ProjectStore) -> set[str]:
    """Assets whose build is Approved in library review (the vetting gate for look scenes)."""
    from . import library_review
    return {a for a, r in library_review.reviews(store).items() if r.get("status") == "approved"}


def placements(store: ProjectStore, shot: dict, radius: float, gate: bool = True,
               only: set[str] | None = None) -> tuple[list[dict], list[str], dict, list[dict]]:
    """The library builds to place for a shot: (placements, kit slots they replace, entries
    {entry: its procedural job.json}, held). With gate, only builds whose asset is Approved in
    library review are placed; the others come back as `held` and their greybox stays. only:
    just these entries (the silhouette check)."""
    p, d, e = _candidates(store, shot, radius, only)
    if not gate:
        return p, d, e, []
    ok = approved(store)
    keep = [x for x in p if x["entry"] in ok]
    return (keep, [x["slot"] for x in keep if x.get("slot")], {k: v for k, v in e.items() if k in ok},
            [x for x in p if x["entry"] not in ok])


def _candidates(store: ProjectStore, shot: dict, radius: float, only=None) -> tuple[list[dict], list[str], dict]:
    from . import city, library_instances
    from .stages import sw_site
    lib = store.root / "3d" / "library"
    cam = np.asarray(shot["matrix"], float)[:3, 3]
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.plan_terrain(store, layout))
    live = {}
    for b in P.typed:          # buildings still in the plan: (typology) -> centres
        x, y, *_ = library_instances.footprint(b)
        live.setdefault(b.typology, []).append((x, y))
    out, entries = [], {}
    for idx in sorted(lib.glob("*/instances/index.json")):
        e = idx.parent.parent.name
        if only is not None and e not in only:
            continue
        job = idx.parent.parent / "procedural" / "job.json"
        if not job.exists():
            continue
        for i in read_json(idx, default=[]) or []:
            if math.hypot(i["x"] - cam[0], i["y"] - cam[1]) > radius or not (store.root / i["glb"]).exists():
                continue
            if not any(math.hypot(i["x"] - x, i["y"] - y) < 1.0 for x, y in live.get(i["typology"], [])):
                continue                          # an old build of a building the plan no longer has
            if not in_view(shot, _box_pts(i["x"], i["y"], i["z"], i["w"], i["d"], i["yaw"], 3.4 * i["storeys"])):
                continue
            out.append({k: i[k] for k in ("key", "glb", "x", "y", "z", "yaw", "w", "d")} | {"entry": e})
            entries[e] = job.relative_to(store.root).as_posix()
    # kit slots (no typed building): the entry's representative build at the slot's pose
    gb = sw_site.load_greybox(store)
    slots = {s["id"]: s for s in gb["slots"]}
    drop_slots = []
    for ent in (read_json(store.root / "site" / "library.json", default={}) or {}).get("entries", []):
        e = ent["id"]
        if only is not None and e not in only:
            continue
        glb, job = lib / e / "procedural.glb", lib / e / "procedural" / "job.json"
        brief = read_json(store.root / "library" / "kits" / f"{e}.json", default=None)
        if not (ent.get("slot_ids") and glb.exists() and job.exists() and brief and brief.get("kind") == "building"):
            continue
        for sid in ent["slot_ids"]:
            s = slots.get(sid)
            if s is None or s.get("typology"):              # typed slots go through instances
                continue
            m = np.asarray(s["matrix"], float)
            x, y, z = m[:3, 3]
            w, d, hgt = brief["dims"]
            if math.hypot(x - cam[0], y - cam[1]) > radius or not in_view(shot, _box_pts(x, y, z, w, d, _yaw(m), hgt)):
                continue
            out.append({"key": f"{e}_{sid}".replace("-", "m"), "entry": e, "glb": glb.relative_to(store.root).as_posix(),
                        "x": float(x), "y": float(y), "z": float(z), "yaw": _yaw(m), "w": w, "d": d, "slot": sid})
            entries[e] = job.relative_to(store.root).as_posix()
            drop_slots.append(sid)
    return out, drop_slots, entries


# --- ground textures (tileable, generated) ------------------------------------------------------

def _save(a: np.ndarray, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.fromarray(np.clip(a * 255 + 0.5, 0, 255).astype(np.uint8))
    im.save(path)
    return path


def _normal_dx(hmap: np.ndarray, strength: float) -> np.ndarray:
    """A DirectX normal map (Unreal's) from a tileable height map."""
    gx = (np.roll(hmap, -1, 1) - np.roll(hmap, 1, 1)) * strength
    gy = (np.roll(hmap, -1, 0) - np.roll(hmap, 1, 0)) * strength
    n = np.stack([-gx, gy, np.ones_like(hmap)], -1)       # rows run down the image: DirectX green
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return n * 0.5 + 0.5


def _noise(n: int, rng, octaves=((8, 1.0), (32, 0.5), (128, 0.25))) -> np.ndarray:
    """Tileable value noise in [0, 1] (bilinear upsampling of wrapped random grids)."""
    out = np.zeros((n, n))
    for cells, amp in octaves:
        g = rng.random((cells, cells))
        t = np.arange(n) * cells / n
        i0 = np.floor(t).astype(int)
        fr = t - i0
        fr = fr * fr * (3 - 2 * fr)
        a = g[np.ix_(i0 % cells, i0 % cells)]
        b = g[np.ix_(i0 % cells, (i0 + 1) % cells)]
        c = g[np.ix_((i0 + 1) % cells, i0 % cells)]
        d = g[np.ix_((i0 + 1) % cells, (i0 + 1) % cells)]
        fx, fy = fr[None, :], fr[:, None]
        out += amp * ((a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy)
    out -= out.min()
    return out / max(out.max(), 1e-6)


def paving_maps(cfg: dict, out: Path, px_per_m: int = 200) -> dict:
    """Slab paving as a tileable set: running bond of slab_m slabs (long side along U, each row
    offset by half a slab), 2 x 4 slabs per tile; per-slab tone variation, fine grain, darker
    rough joints. Returns {"color", "rough", "normal", "tile_m": [u, v]}."""
    sw, sh = cfg["slab_m"]
    cols, rows = 2, 4
    W, H = int(round(cols * sw * px_per_m)), int(round(rows * sh * px_per_m))
    rng = np.random.default_rng(11)
    yy, xx = (np.mgrid[0:H, 0:W] + 0.5) / px_per_m       # pixel centres: joints symmetric across the wrap
    r = (yy // sh).astype(int)
    xo = xx + (r % 2) * sw / 2
    c = (xo // sw).astype(int) % cols
    tone = 1.0 + cfg.get("variation", 0.06) * (rng.random((rows, cols)) * 2 - 1)
    grain = _noise(max(W, H), rng)[:H, :W]
    jw = cfg["joint_mm"] / 1000 / 2
    du = np.minimum(xo % sw, sw - xo % sw)
    dv = np.minimum(yy % sh, sh - yy % sh)
    joint = np.clip(1 - (np.minimum(du, dv) - jw) / (jw * 0.8 + 1e-6), 0, 1)
    alb = tone[r, c] * (0.94 + 0.08 * grain)
    alb = alb * (1 - 0.55 * joint)
    alb = alb / alb[joint < 0.01].mean()                 # the slabs average 1: the tint is their colour
    rough = np.clip(cfg["roughness"] * (0.85 + 0.3 * grain) + 0.5 * joint, 0, 1)
    height = -joint * 1.0 + 0.04 * grain
    key = hashlib.sha1(repr(sorted(cfg.items())).encode()).hexdigest()[:8]
    return {"color": str(_save(np.repeat(np.clip(alb / 1.2, 0, 1)[..., None], 3, -1), out / f"paving_c_{key}.png")),
            "color_scale": 1.2,
            "rough": str(_save(np.repeat(rough[..., None], 3, -1), out / f"paving_r_{key}.png")),
            "normal": str(_save(_normal_dx(height, 6.0), out / f"paving_n_{key}.png")),
            "tile_m": [cols * sw, rows * sh]}


def water_maps(cfg: dict, out: Path, n: int = 512) -> dict:
    """Tileable ripples: a sum of sines with whole wave counts per tile. {"normal", "tile_m"}."""
    rng = np.random.default_rng(5)
    t = np.arange(n) / n * 2 * np.pi
    X, Y = np.meshgrid(t, t)
    h = np.zeros((n, n))
    for _ in range(14):
        kx, ky = rng.integers(-6, 7, 2)
        if kx == 0 and ky == 0:
            continue
        h += np.sin(kx * X + ky * Y + rng.random() * 6.28) / math.hypot(kx, ky)
    h /= np.abs(h).max()
    key = hashlib.sha1(repr(sorted(cfg.items())).encode()).hexdigest()[:8]
    return {"normal": str(_save(_normal_dx(h, 40.0), out / f"water_n_{key}.png")), "tile_m": [cfg["ripple_m"]] * 2}


def grain_maps(kind: str, cfg: dict, out: Path, n: int = 512) -> dict:
    """A tileable tone grain for lawn and gravel. {"color", "normal", "tile_m"}."""
    rng = np.random.default_rng(3 if kind == "lawn" else 9)
    g = _noise(n, rng, ((16, 1.0), (64, 0.7), (256, 0.6)))
    alb = 0.8 + 0.4 * g
    key = hashlib.sha1((kind + repr(sorted(cfg.items()))).encode()).hexdigest()[:8]
    return {"color": str(_save(np.repeat(np.clip(alb / 1.2, 0, 1)[..., None], 3, -1), out / f"{kind}_c_{key}.png")),
            "color_scale": 1.2,
            "normal": str(_save(_normal_dx(g, 8.0), out / f"{kind}_n_{key}.png")), "tile_m": [cfg["grain_m"] * 8] * 2}


def ground(store: ProjectStore, p: dict, shot: dict, out: Path) -> dict:
    """The ground surfaces' material specs for the UE script, tints read off the frame."""
    fr = frame_of(store, shot)
    w, h = shot["resolution"]
    img = np.asarray(Image.open(fr).convert("RGB").resize((w, h)), float) if fr and fr.exists() else None
    cls = frame_classes(store, shot["id"], (w, h)) if img is not None else None
    box = (p.get("exposure") or {}).get("box")
    spec, tints = {}, {}
    for kind, key in SURFACES.items():
        cfg = p[key]
        tint = cfg.get("tint", "frame")
        if tint == "frame":
            tint = frame_tint(img, cls, kind, box if kind == "paving" else None, cfg.get("albedo"), cfg.get("lit", 0.4)) \
                if img is not None else None
            src = "frame" if tint else "fallback"
            tint = tint or FALLBACK_TINT[kind]
        else:
            src = "preset"
        tints[kind] = {"tint": tint, "from": src}
        maps = paving_maps(cfg, out) if kind == "paving" else water_maps(cfg, out) if kind == "water" \
            else grain_maps(kind, cfg, out)
        spec[kind] = maps | {"tint": tint, "roughness": cfg["roughness"], "normal_strength": cfg.get("normal_strength", 1.0)}
    return {"materials": spec, "tints": tints, "classes": cls is not None}


def backdrop(store: ProjectStore, p: dict, shot: dict) -> tuple[dict | None, dict]:
    """The backdrop job for blender_look_context (or None) and what was read for it."""
    bd = p.get("backdrop")
    if not bd:
        return None, {}
    bd = dict(bd)
    if not bd.pop("from_frame", False):
        return bd, {"mode": "preset"}
    w, h = shot["resolution"]
    cls = frame_classes(store, shot["id"], (w, h))
    if cls is None:
        raise FileNotFoundError(f"backdrop from_frame: no class map site/reconcile/{shot['id']}/classes.png")
    near, far = bd.get("dist", [2500.0, 4500.0])
    crest = ridge_from_frame(cls, shot["matrix"], shot["lens_mm"], shot["sensor_mm"], near,
                             float(np.asarray(shot["matrix"])[2, 3]))
    if not crest:
        return None, {"mode": "from_frame", "error": "no hill class in the frame"}
    return ({"crest": crest, "dist": [near, far], "pad_deg": bd.get("pad_deg", 25.0)},
            {"mode": "from_frame", "points": len(crest), "z": [min(c[1] for c in crest), max(c[1] for c in crest)]})


def comfy_running() -> bool:
    import socket
    for port in (8188, 8189):
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
    return False


def run(store: ProjectStore, name: str, log=print) -> dict:
    """Build and render look scene `name` from its preset; append the result to its history."""
    from . import ue
    from .stages import sw_site
    p = preset(store, name)
    gb = sw_site.load_greybox(store)
    shot = next(s for s in gb["shots"] if s["id"] == p["shot"])
    layout_shot = next(s for s in sw_site.load_layout(store).shots if s.id == p["shot"])
    shot = shot | {"locked": layout_shot.locked}
    out = store.root / "export" / "look" / name
    pls, drop_slots, entries, held = placements(store, shot, p["radius"], gate=p.get("gate", True))
    log(f"{name}: {len(pls)} approved library builds placed ({len(drop_slots)} at kit slots); "
        f"{len(held)} held back as greybox ({len(set(h['entry'] for h in held))} entries not approved)")
    from . import assembly                       # attached pieces go with their parent
    kids, kid_entries, no_model = assembly.child_placements(store, pls)
    if kids or no_model:
        log(f"{name}: {len(kids)} attached piece(s) placed with their parents"
            + (f"; left out (no 3D model yet): {', '.join(no_model)}" if no_model else ""))
    parents = pls
    pls = pls + kids
    entries = {**kid_entries, **entries}
    gr = ground(store, p, shot, out / "textures")
    log(f"{name}: ground tints " + ", ".join(f"{k} {v['tint']} ({v['from']})" for k, v in gr["tints"].items()))
    bd, bd_info = backdrop(store, p, shot)
    if bd_info:
        log(f"{name}: backdrop {bd_info}")
    gpu = p["render_gpu"]
    if gpu == "auto":
        gpu = 0 if comfy_running() else 1
    exp = p.get("exposure")
    rep = ue.look_scene(store, name, p["shot"], pls, entries, sun=p["sun"], fog=p["fog"], backdrop=bd,
                        drop_slots=drop_slots, radius=p["radius"],
                        exposure=exp | {"frame": str(shot["locked"])} if exp and shot.get("locked") else None,
                        sky=p["sky"], white_balance=p["white_balance"], ground=gr["materials"], adapter=int(gpu),
                        greybox_rgb=p.get("greybox"), held=held, held_slots=[h["slot"] for h in held if h.get("slot")])
    sil = {}
    if parents and shot.get("locked"):
        if comfy_running():
            from . import silhouette
            sil = silhouette.check(store, shot, parents, log=log)
        else:   # the frame masks need SAM 3.1; the render doesn't
            log(f"{name}: silhouette check skipped: ComfyUI is down (ap library silhouette later)")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    render = out / f"look_{name}.png"
    keep = out / f"look_{name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    if render.exists():
        keep.write_bytes(render.read_bytes())
    rec = {"at": stamp, "placed": len(pls), "render": keep.relative_to(store.root).as_posix(),
           "render_gpu": rep.get("render", {}).get("adapter", gpu),
           "sun": p["sun"], "sky": p["sky"], "white_balance": p["white_balance"], "fog": p["fog"],
           "exposure": rep.get("exposure"), "tints": gr["tints"], "backdrop": bd_info,
           "held": sorted({h["entry"] for h in held}), "silhouettes": {k: {x: v.get(x) for x in ("entry", "iou", "aspect_frame", "aspect_build", "score", "error")} for k, v in sil.items()},
           "ok": bool(rep.get("ok") and rep.get("render", {}).get("ok"))}
    own = read_json(preset_dir(store) / f"{name}.json", default={})
    own["history"] = (own.get("history") or []) + [rec]
    write_json(preset_dir(store) / f"{name}.json", own)
    return rep | {"record": rec}


def shot_of(store: ProjectStore, shot_id: str) -> dict:
    """The greybox shot with its locked frame."""
    from .stages import sw_site
    gb = sw_site.load_greybox(store)
    shot = next(s for s in gb["shots"] if s["id"] == shot_id)
    return shot | {"locked": next(s for s in sw_site.load_layout(store).shots if s.id == shot_id).locked}


def silhouette_of(store: ProjectStore, entry: str, shot_id: str, radius: float = 600.0, log=print) -> dict:
    """The silhouette check of one entry's builds in view of a shot, approved or not."""
    from . import silhouette
    shot = shot_of(store, shot_id)
    pls = placements(store, shot, radius, gate=False, only={entry})[0]
    if not pls:
        raise ValueError(f"no build of {entry} in view of {shot_id}")
    return silhouette.check(store, shot, pls, log=log)


DETAIL_SHARE = 0.02         # slots at or above this share of the frame are reviewed in detail
TIERS = {"detail": "massing, colour, opening rhythm, and the details the frame shows (reveals, rails, piers, doors)",
         "massing": "massing, colour and opening rhythm only"}
TILE_TYPES = ("townhouses", "wall", "cohousing_court", "insula", "garden_slab", "perimeter", "row")
WALL_RUN = 12               # wall bays per "building" when a wall slot is split


def split_tiles(store: ProjectStore, shot: dict, slot_ids: list[str], out_dir: Path, min_share: float = 0.0002) -> dict:
    """Tile slots split into their buildings through an object-level id pass from the shot camera
    (scripts/blender_instance_ids.py): each greybox piece is assigned to the typed building whose
    footprint holds it (walls: runs of WALL_RUN bays), and each building gets its visible pixels'
    frame share and bounding box. {slot: [{"key", "share", "bbox", "pieces"}]} (largest first)."""
    import re

    from . import blender, city, config, library_instances
    from .stages import sw_site
    from .stages.sw_shots import _key
    job = {"shot": shot["id"], "slots": slot_ids, "out_png": str(out_dir / "instance_ids.png"),
           "out_json": str(out_dir / "instance_ids.json")}
    write_json(out_dir / "instance_ids_job.json", job)
    blender.run(config.REPO_ROOT / "scripts/blender_instance_ids.py", [out_dir / "instance_ids_job.json"],
                out_dir / "instance_ids.log", "instance ids", blend=sw_site.blend_path(store))
    objs = {int(k): v for k, v in read_json(out_dir / "instance_ids.json")["objects"].items()}
    idx = _key(np.asarray(Image.open(out_dir / "instance_ids.png").convert("RGB")))
    total = idx.size
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.plan_terrain(store, layout))
    foot = []
    for b in P.typed:
        x, y, z, w, d = library_instances.footprint(b)
        foot.append((b, x, y, w, d, math.radians(b.rot)))
    groups = {}
    for i, o in objs.items():
        cx, cy = o["center"][:2]
        if o["piece"].startswith("wall") or o["slot"].startswith("wall"):
            m = re.search(r"\.(\d+)$", o["name"])
            key = f"{o['slot']}:run{int(m.group(1)) // WALL_RUN if m else 0}"
        else:
            key = None
            for b, x, y, w, d, a in foot:
                u = (cx - x) * math.cos(a) + (cy - y) * math.sin(a)
                v = -(cx - x) * math.sin(a) + (cy - y) * math.cos(a)
                if abs(u) <= w / 2 + 1.0 and abs(v) <= d / 2 + 1.0:
                    key = f"{o['slot']}:{b.typology}@{round(x)},{round(y)}"
                    break
            key = key or f"{o['slot']}:other"
        groups.setdefault(o["slot"], {}).setdefault(key, []).append(i)
    out = {}
    for slot, g in groups.items():
        rows = []
        for key, ids in g.items():
            m = np.isin(idx, ids)
            n = int(m.sum())
            if n < min_share * total:
                continue
            ys, xs = np.nonzero(m)
            rows.append({"key": key, "share": round(n / total, 5), "pieces": len(ids),
                         "bbox": [int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1]})
        out[slot] = sorted(rows, key=lambda r: -r["share"])
    return out


SLOT_SKIP = ("pool", "plaza", "radial", "avenue", "street", "stair", "lane", "ring", "terrain", "park", "sea", "garden")
DRAFT_PROMPT = """This crop is from a concept frame of a city. Describe the main building in it (the one
filling most of the crop) as a short design brief for a procedural building generator. Judge only
what you can see. Pick the closest form: {forms}, or "other". Give its storeys, its front's
length over its height as you see it, the ground storey (colonnade, arcade, shopfronts, plain wall,
doors), the upper storeys' facade (window rhythm, loggias, balconies, blank wall), the roof (flat,
parapet, hipped, pitched, dome, terrace), the materials and colours, and any distinctive features.
Keep each field to a phrase."""


def pending_slots(store: ProjectStore, shot_id: str, draft: bool = True, log=print) -> list[dict]:
    """Every building or structure slot a shot shows that has no approved build, ranked by how
    much of the frame it covers (its id pass), with its frame crop, its state, its review tier
    (detail at DETAIL_SHARE of the frame or more, else massing) and (draft) a brief drafted from
    the crop by the vision model. Tile slots (townhouses, walls...) are split into their
    buildings (split_tiles): one crop and draft per building. Written to
    export/look/<shot>/slots/slots.json."""
    import re

    from pydantic import BaseModel
    from typing import Literal

    from . import building_gen, library_review
    from .stages import sw_shots, sw_site
    shot = shot_of(store, shot_id)
    ids = read_json(sw_shots.shot_dir(store, shot_id) / "ids.json")
    slots = {s["id"]: s for s in sw_site.load_greybox(store)["slots"]}
    lib = (read_json(store.root / "site" / "library.json", default={}) or {}).get("entries", [])
    entry_of = {}
    for e in lib:
        for sid in e.get("slot_ids") or []:
            entry_of.setdefault(sid, []).append(e["id"])
    ok, revs = approved(store), library_review.reviews(store)
    out_dir = store.root / "export" / "look" / shot_id / "slots"
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = Image.open(store.root / shot["locked"]).convert("RGB") if shot.get("locked") else None
    rows = []
    for sid, v in sorted(ids["slots"].items(), key=lambda kv: -kv[1]["frac"]):
        s = slots.get(sid) or {}
        if s.get("category") == "terrain" or (s.get("type") or "") in SLOT_SKIP or v["frac"] < 0.0002:
            continue
        ents = entry_of.get(sid, [])
        if not ents and s.get("typology"):     # entries newer than library.json: by typology (+ material)
            t = s["typology"].replace("_", "-")
            ents = [k for k in ((f"{t}-{s['material']}" if s.get("material") else None), t)
                    if k and (store.root / "library" / "kits" / f"{k}.json").exists()][:1]
        if ents and all(e in ok for e in ents):
            continue
        states = []
        for e in ents or [None]:
            d = store.root / "3d" / "library" / (e or "_")
            if e is None:
                states.append("greybox (no library entry)")
            elif (d / "procedural.glb").exists() or (d / "instances").exists():
                st = (revs.get(e) or {}).get("status")
                states.append(f"{e}: built, {library_review.STATUSES.get(st, 'not reviewed').lower()}")
            elif (store.root / "library" / "kits" / f"{e}.json").exists():
                states.append(f"{e}: brief, not built")
            else:
                states.append(f"{e}: library entry, no brief")
        tier = "detail" if v["frac"] >= DETAIL_SHARE else "massing"
        row = {"slot": sid, "type": s.get("type"), "typology": s.get("typology"), "material": s.get("material"),
               "frame_share": round(v["frac"], 4), "entries": ents, "state": states, "tier": tier,
               "tier_needs": TIERS[tier]}
        if frame is not None:
            w, h = frame.size
            x0, y0, x1, y1 = v["bbox"]
            px, py = 0.1 * (x1 - x0), 0.1 * (y1 - y0)
            box = (int(max(0, x0 - px) * w), int(max(0, y0 - py) * h), int(min(1, x1 + px) * w), int(min(1, y1 + py) * h))
            crop = out_dir / f"{sid}.png"
            frame.crop(box).save(crop)
            row |= {"crop": crop.relative_to(store.root).as_posix(), "crop_box": list(box)}
        rows.append(row)
    tiles = [r["slot"] for r in rows if (r["type"] or "") in TILE_TYPES]
    if tiles and frame is not None:              # tile slots: one crop (and draft) per building
        split = split_tiles(store, shot, tiles, out_dir)
        w, h = frame.size
        for r in rows:
            if r["slot"] not in split:
                continue
            r["buildings"] = []
            for b in split[r["slot"]]:
                x0, y0, x1, y1 = b["bbox"]
                px, py = max(8, int(0.15 * (x1 - x0))), max(8, int(0.15 * (y1 - y0)))
                box = (max(0, x0 - px), max(0, y0 - py), min(w, x1 + px), min(h, y1 + py))
                crop = out_dir / f"{r['slot']}__{re.sub(r'[^A-Za-z0-9_.-]+', '_', b['key'].split(':', 1)[1])}.png"
                frame.crop(box).save(crop)
                r["buildings"].append(b | {"crop": crop.relative_to(store.root).as_posix(), "crop_box": list(box),
                                           "tier": "detail" if b["share"] >= DETAIL_SHARE else "massing"})
    if draft and frame is not None:
        from .llm.base import structured
        from .llm.registry import llm_for

        class Draft(BaseModel):
            form: Literal[tuple(list(building_gen.FORMS) + ["other"])]
            storeys: int
            length_over_height: float
            ground: str
            upper: str
            roof: str
            materials: str
            features: str
        llm = llm_for(None)
        prompt = DRAFT_PROMPT.format(forms=", ".join(building_gen.FORMS))
        def drafted(target, crop):
            try:
                target["draft"] = structured(llm, prompt, [store.root / crop], Draft).model_dump()
            except Exception as e:          # a failed draft leaves the row, says why
                target["draft_error"] = f"{type(e).__name__}: {e}"
        for r in rows:
            t0 = time.time()
            if r.get("buildings"):           # tile slots: each building drafts from its own crop
                for b in r["buildings"]:
                    drafted(b, b["crop"])
            else:
                drafted(r, r["crop"])
            log(f"{r['slot']}: {r['frame_share'] * 100:.2f}% of the frame, {'; '.join(r['state'])}"
                f" ({time.time() - t0:.0f} s)")
    write_json(out_dir / "slots.json", {"shot": shot_id, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "slots": rows})
    return rows
