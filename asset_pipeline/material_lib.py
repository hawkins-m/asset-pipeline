"""Material library for procedural buildings: the sheet picks a material type and a colour per
storey band, and the surface comes from a fixed library of tileable scanned PBR sets instead of
a patch cut from the sheet (patches carry the sheet's shading, glare and drawing artefacts into
the tile).

Each type maps to
    acg        an ambientCG set (CC0), downloaded once to $AP_ROOT/materials/ambientcg/<id>/:
               Blender renders and the GLB use it, tinted to the band's colour;
    megascans  Megascans surface names (Fab) to look for in the Unreal project: the UE import
               uses the first one it finds there and falls back to the ambientCG maps (Megascans
               come through Fab with the user's Epic login in the editor; some listings are
               paid, so the pipeline never fetches them itself).

Tinting keeps the scan's own detail and moves its average colour to the sheet's: in linear RGB,
albedo x (target / mean albedo). Blender gets a baked texture per band (the GLB carries it, so the
review renders show it); Unreal does the same in the material (TargetColor over the texture's
lowest mip), so the colour stays editable there.
"""
import io
import json
import re
import urllib.request
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from . import config, filestore

TYPES = ("concrete", "limestone", "marble", "render", "brick", "timber")      # what the VLM reads off a sheet

# tile_m: metres one texture repeat covers (width, height): from ambientCG's measured size when
# it has one, else read off the preview (course heights, board widths)
LIBRARY = {
    "concrete":  {"acg": "Concrete034", "tile_m": [1.1, 0.55], "megascans": ["Brushed Concrete Wall", "Concrete Wall"],
                  "words": "cast or precast concrete: smooth grey panels, formwork joints, no visible blocks"},
    "limestone": {"acg": "Tiles143", "tile_m": [2.0, 2.0], "alternatives": {"Bricks008": [1.65, 1.65], "Travertine009": [1.2, 1.2]}, "megascans": ["Roman Limestone Wall", "Limestone Wall"],
                  "words": "dressed stone: ashlar blocks in regular courses, fine joints, pale cream or grey"},
    "marble":    {"acg": "Marble019", "tile_m": [2.0, 2.0], "megascans": ["White Marble", "Marble"],
                  "words": "polished marble: smooth large slabs, soft veining, bright, slight sheen"},
    "render":    {"acg": "Plaster001", "tile_m": [2.0, 2.0], "megascans": ["Painted Wall Plaster", "Plaster"],
                  "words": "rendered or stuccoed wall: one even colour, no joints or blocks, matte"},
    "brick":     {"acg": "Bricks086", "tile_m": [2.6, 1.3], "megascans": ["Red Brick Wall", "Brick Wall"],
                  "words": "fired brick: small units in running courses, mortar lines every ~7 cm"},
    "timber":    {"acg": "WoodSiding009", "tile_m": [2.0, 2.0], "megascans": ["Wooden Planks Wall", "Wooden Wall Panel"],
                  "words": "timber cladding: boards or slats with visible grain"},
    # briefs and guidance only (not a sheet reading): the limestone alternative's scan as its own type
    "travertine": {"acg": "Travertine009", "tile_m": [1.2, 1.2], "megascans": ["Travertine Wall", "Travertine"],
                   "words": "travertine: warm cream stone with small pits and veins, in large blocks"},
}
# material words guidance may use -> the library type, or None when the library has no scan for it
MATERIAL_WORDS = {"concrete": "concrete", "brick": "brick", "marble": "marble", "limestone": "limestone",
                  "travertine": "travertine", "render": "render", "stucco": "render", "plaster": "render",
                  "timber": "timber", "wood": "timber", "wooden": "timber",
                  "granite": None, "sandstone": None, "tuff": None, "basalt": None, "slate": None,
                  "terracotta": None, "tile": None, "steel": None, "copper": None, "zinc": None}
MAPS = {"color": "Color", "normal_gl": "NormalGL", "normal_dx": "NormalDX", "rough": "Roughness"}
RES = "2K-JPG"


def root() -> Path:
    return config.ap_root() / "materials" / "ambientcg"


def _srgb_to_lin(a: np.ndarray) -> np.ndarray:
    a = a / 255.0
    return np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(a: np.ndarray) -> np.ndarray:
    a = np.clip(a, 0.0, 1.0)
    return 255.0 * np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055)


def fetch(kind: str, log=print, acg: str | None = None) -> dict:
    """The type's ambientCG maps on disk (downloaded on first use): {color, normal_gl, normal_dx,
    rough, mean_lin, tile_m, acg}. Only the expected map files are taken out of the zip. acg: one
    of the type's `alternatives` instead of its set."""
    lib = LIBRARY[kind]
    aid = acg or lib["acg"]
    tile = lib["alternatives"][aid] if acg and acg != lib["acg"] else lib["tile_m"]
    d = root() / aid
    meta = d / "meta.json"
    if not meta.is_file():
        url = f"https://ambientcg.com/get?file={aid}_{RES}.zip"
        log(f"downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "asset-pipeline"})
        data = urllib.request.urlopen(req, timeout=300).read()
        d.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for key, suffix in MAPS.items():
                name = f"{aid}_{RES}_{suffix}.jpg"
                if name not in z.namelist():
                    raise RuntimeError(f"{aid}: no {name} in the ambientCG zip")
                (d / name).write_bytes(z.read(name))
        a = np.asarray(Image.open(d / f"{aid}_{RES}_Color.jpg").convert("RGB")).astype(np.float64)
        mean = _srgb_to_lin(a).reshape(-1, 3).mean(0)
        meta.write_text(json.dumps({"acg": aid, "mean_lin": [round(float(v), 5) for v in mean],
                                    "size_px": [a.shape[1], a.shape[0]], "license": "CC0 (ambientcg.com)"}))
    m = json.loads(meta.read_text())
    return {**{k: str(d / f"{aid}_{RES}_{s}.jpg") for k, s in MAPS.items()}, "mean_lin": m["mean_lin"],
            "tile_m": tile, "acg": aid}


def missing_materials(text: str) -> list[str]:
    """Materials `text` names that the library has no scan for ("granite"): a reroll says so
    instead of quietly keeping the old surface."""
    out = []
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        k = w if w in MATERIAL_WORDS else w.rstrip("s")              # "tiles" -> "tile"
        if k in MATERIAL_WORDS and MATERIAL_WORDS[k] is None and k not in out:
            out.append(k)
    return out


def natural_rgb(kind: str) -> list[int]:
    """The type's scan's own average colour (sRGB 0-255): tinting to it leaves the scan as it is."""
    return [int(round(float(v))) for v in _lin_to_srgb(np.array(fetch(kind)["mean_lin"], np.float64))]


def scaled_tile(tile_m, scale: float = 1.0) -> list[float]:
    """The metres one texture repeat covers (width, height) at a texture scale: a multiplier on
    the material's real-world size (0.5 on a 2 m stone set gives 1 m blocks and joints)."""
    t = [tile_m, tile_m] if isinstance(tile_m, (int, float)) else list(tile_m)
    return [round(float(v) * float(scale), 4) for v in t[:2]]


def tint_ratio(mean_lin, rgb) -> list[float]:
    """Per-channel factor that moves the texture's mean (linear) to the sheet colour (sRGB 0-255)."""
    t = _srgb_to_lin(np.array(rgb, np.float64))
    return [round(float(v), 4) for v in t / np.maximum(np.array(mean_lin), 1e-3)]


LUM_LIN = np.array([0.2126, 0.7152, 0.0722])
GLB_PX = 1024          # maps embedded in the GLBs; Unreal gets the full-resolution shared ones


def lum_std(rgb_px: np.ndarray) -> float:
    """Luminance spread (linear) of sRGB pixels [..., 3]: how speckled a surface reads."""
    return float((_srgb_to_lin(np.asarray(rgb_px, np.float64)).reshape(-1, 3) @ LUM_LIN).std())


def contrast_factor(kind: str, target_std: float | None, lo: float = 0.2, acg: str | None = None) -> float:
    """How much to scale the tinted scan's deviation from its mean so its luminance spread
    matches the sheet band's (never more than the scan's own, never below lo)."""
    if target_std is None:
        return 1.0
    src = fetch(kind, acg=acg)
    a = np.asarray(Image.open(src["color"]).convert("RGB").resize((256, 256))).astype(np.float64)
    s = lum_std(a) / max(float(np.array(src["mean_lin"]) @ LUM_LIN), 1e-3)   # relative spread of the scan
    return round(float(np.clip(target_std / max(s, 1e-4), lo, 1.0)), 3)


def _small(path: str, size: int = GLB_PX) -> str:
    """A shared 1K copy of a scan map (next to it, in <id>/<size>/)."""
    p = Path(path)
    out = p.parent / str(size) / p.name
    if not out.is_file():
        out.parent.mkdir(exist_ok=True)
        im = Image.open(p)
        im.resize((size, size * im.height // im.width), Image.LANCZOS).save(out, quality=92)
    return str(out)


def tinted(kind: str, rgb, out: Path, contrast_std: float | None = None, size: int = GLB_PX, log=print,
           acg: str | None = None, scale: float = 1.0) -> dict:
    """Blender maps for one band: the type's albedo tinted to rgb, its luminance spread scaled
    towards contrast_std (the sheet band's relative spread, see building_proc.band_contrast),
    baked to out at `size`; its own normal (OpenGL) and roughness, shared 1K copies. In linear
    RGB: target + k * (albedo x target / mean - target), the same as M_AP_Surface in Unreal.
    scale multiplies the tile size (see scaled_tile): larger = coarser joints and veining."""
    src = fetch(kind, log=log, acg=acg)
    k = contrast_factor(kind, contrast_std, acg=acg)
    im = Image.open(src["color"]).convert("RGB")
    im = im.resize((size, size * im.height // im.width), Image.LANCZOS)
    t = _srgb_to_lin(np.array(rgb, np.float64))
    lin = _srgb_to_lin(np.asarray(im).astype(np.float64)) * np.array(tint_ratio(src["mean_lin"], rgb))[None, None]
    lin = t[None, None] + k * (lin - t[None, None])
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(_lin_to_srgb(lin).round().astype(np.uint8)).save(out, quality=92)
    return {"base": str(out), "normal": _small(src["normal_gl"], size), "rough": _small(src["rough"], size), "uv": "tile",
            "tile_m": scaled_tile(src["tile_m"], scale)[0], "tile_v_m": scaled_tile(src["tile_m"], scale)[1],
            "scale": float(scale), "colour": [int(c) for c in rgb],
            "type": kind, "acg": src["acg"], "contrast": k}


# --- reading the type off the sheet --------------------------------------------------------

class BandMaterial(BaseModel):
    type: str = Field(description="one of: " + ", ".join(TYPES))
    why: str = Field(description="what in the picture shows it, a few words")


class BandMaterials(BaseModel):
    base: BandMaterial
    wall: BandMaterial
    top: BandMaterial


PROMPT = """These three crops are the storey bands of one building, drawn on a reference sheet:
image 1 the ground storey (base), image 2 the middle storeys (wall), image 3 the top storey (top).
For each band, say what its main wall surface is made of (not the windows, glass, rails, planting
or shadow), as one of these types:
{types}
The ground storey is mostly openings (shopfronts, glazing, doors): judge it by the piers between
them and the wall ends, which are often a different material from the storeys above (brick piers
show many thin horizontal courses of small units).
Judge by the surface: joints, block courses, veining, grain. Colour alone doesn't decide it (pale
stone, white render and white concrete look alike: look for block joints or panel lines).
Answer JSON: {{"base": {{"type": ..., "why": ...}}, "wall": {{...}}, "top": {{...}}}}."""


def band_crops(view: Path, meas: dict, out: Path) -> dict[str, Path]:
    """Each storey band of the view as its own image (upscaled when small, for the VLM)."""
    from .building_proc import _band_rows
    im = Image.open(view).convert("RGB")
    x0, _, x1, _ = meas["bbox_px"]
    out.mkdir(parents=True, exist_ok=True)
    res = {}
    for name, (r0, r1) in _band_rows(meas).items():
        c = im.crop((x0, r0, x1 + 1, r1 + 1))
        if c.width < 1536:              # thin bands: piers between openings are only a few px wide
            c = c.resize((1536, max(1, 1536 * c.height // c.width)), Image.LANCZOS)
        res[name] = out / f"band_{name}.png"
        filestore.save_image(None, c, res[name])        # never through a stored (linked) file
    return res


def classify(view: Path, meas: dict, out: Path, llm=None) -> dict:
    """{band: {type, why}} from the project's vision LLM (local by default)."""
    from .llm.base import structured
    from .llm.registry import llm_for
    crops = band_crops(view, meas, out)
    types = "\n".join(f"  {k}: {v['words']}" for k, v in LIBRARY.items())
    r = structured(llm or llm_for(None), PROMPT.format(types=types), [crops[b] for b in ("base", "wall", "top")],
                   BandMaterials, system="You are an architect identifying facade materials.")
    res = {}
    for band in ("base", "wall", "top"):
        bm = getattr(r, band)
        t = re.sub(r"[^a-z]", "", bm.type.lower())
        res[band] = {"type": t if t in TYPES else "render", "why": bm.why}
        if t not in TYPES:
            res[band]["why"] += f" (answered {bm.type!r}: not a library type, render used)"
    return res
