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


def placements(store: ProjectStore, shot: dict, radius: float) -> tuple[list[dict], list[str], dict]:
    """The library builds to place for a shot: (placements, kit slots they replace, entries
    {entry: its procedural job.json})."""
    from . import city, library_instances
    from .stages import sw_site
    lib = store.root / "3d" / "library"
    cam = np.asarray(shot["matrix"], float)[:3, 3]
    layout = sw_site.load_layout(store)
    P = city.plan(layout, sw_site.terrain(store, layout))
    live = {}
    for b in P.typed:          # buildings still in the plan: (typology) -> centres
        x, y, *_ = library_instances.footprint(b)
        live.setdefault(b.typology, []).append((x, y))
    out, entries = [], {}
    for idx in sorted(lib.glob("*/instances/index.json")):
        e = idx.parent.parent.name
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
                        "x": float(x), "y": float(y), "z": float(z), "yaw": _yaw(m), "w": w, "d": d})
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
    pls, drop_slots, entries = placements(store, shot, p["radius"])
    log(f"{name}: {len(pls)} library builds in view ({len(drop_slots)} at kit slots), {len(entries)} entries")
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
                        sky=p["sky"], white_balance=p["white_balance"], ground=gr["materials"], adapter=int(gpu))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    render = out / f"look_{name}.png"
    keep = out / f"look_{name}_{time.strftime('%Y%m%d_%H%M%S')}.png"
    if render.exists():
        keep.write_bytes(render.read_bytes())
    rec = {"at": stamp, "placed": len(pls), "render": keep.relative_to(store.root).as_posix(), "render_gpu": gpu,
           "sun": p["sun"], "sky": p["sky"], "white_balance": p["white_balance"], "fog": p["fog"],
           "exposure": rep.get("exposure"), "tints": gr["tints"], "backdrop": bd_info,
           "ok": bool(rep.get("ok") and rep.get("render", {}).get("ok"))}
    own = read_json(preset_dir(store) / f"{name}.json", default={})
    own["history"] = (own.get("history") or []) + [rec]
    write_json(preset_dir(store) / f"{name}.json", own)
    return rep | {"record": rec}
