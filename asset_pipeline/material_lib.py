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

from . import config

TYPES = ("concrete", "limestone", "marble", "render", "brick", "timber")

# tile_m: metres one texture repeat covers (width, height): from ambientCG's measured size when
# it has one, else read off the preview (course heights, board widths)
LIBRARY = {
    "concrete":  {"acg": "Concrete034", "tile_m": [1.1, 0.55], "megascans": ["Brushed Concrete Wall", "Concrete Wall"],
                  "words": "cast or precast concrete: smooth grey panels, formwork joints, no visible blocks"},
    "limestone": {"acg": "Bricks008", "tile_m": [1.65, 1.65], "megascans": ["Roman Limestone Wall", "Limestone Wall"],
                  "words": "dressed stone: ashlar blocks in regular courses, fine joints, pale cream or grey"},
    "marble":    {"acg": "Marble019", "tile_m": [2.0, 2.0], "megascans": ["White Marble", "Marble"],
                  "words": "polished marble: smooth large slabs, soft veining, bright, slight sheen"},
    "render":    {"acg": "Plaster001", "tile_m": [2.0, 2.0], "megascans": ["Painted Wall Plaster", "Plaster"],
                  "words": "rendered or stuccoed wall: one even colour, no joints or blocks, matte"},
    "brick":     {"acg": "Bricks086", "tile_m": [2.6, 1.3], "megascans": ["Red Brick Wall", "Brick Wall"],
                  "words": "fired brick: small units in running courses, mortar lines every ~7 cm"},
    "timber":    {"acg": "WoodSiding009", "tile_m": [2.0, 2.0], "megascans": ["Wooden Planks Wall", "Wooden Wall Panel"],
                  "words": "timber cladding: boards or slats with visible grain"},
}
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


def fetch(kind: str, log=print) -> dict:
    """The type's ambientCG maps on disk (downloaded on first use): {color, normal_gl, normal_dx,
    rough, mean_lin, tile_m, acg}. Only the expected map files are taken out of the zip."""
    lib = LIBRARY[kind]
    aid = lib["acg"]
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
            "tile_m": lib["tile_m"], "acg": aid}


def tint_ratio(mean_lin, rgb) -> list[float]:
    """Per-channel factor that moves the texture's mean (linear) to the sheet colour (sRGB 0-255)."""
    t = _srgb_to_lin(np.array(rgb, np.float64))
    return [round(float(v), 4) for v in t / np.maximum(np.array(mean_lin), 1e-3)]


def tinted(kind: str, rgb, out: Path, size: int = 2048, log=print) -> dict:
    """Blender maps for one band: the type's albedo tinted to rgb (baked to out/<name>), its own
    normal (OpenGL) and roughness. Detail keeps its contrast; the average becomes rgb."""
    src = fetch(kind, log=log)
    a = np.asarray(Image.open(src["color"]).convert("RGB")).astype(np.float64)
    lin = _srgb_to_lin(a) * np.array(tint_ratio(src["mean_lin"], rgb))[None, None]
    img = Image.fromarray(_lin_to_srgb(lin).round().astype(np.uint8))
    if max(img.size) > size:
        img = img.resize((size, size * img.height // img.width), Image.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=92)
    return {"base": str(out), "normal": src["normal_gl"], "rough": src["rough"], "uv": "tile",
            "tile_m": src["tile_m"][0], "tile_v_m": src["tile_m"][1], "colour": [int(c) for c in rgb],
            "type": kind, "acg": src["acg"]}


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
        if c.width < 768:
            c = c.resize((768, max(1, 768 * c.height // c.width)), Image.LANCZOS)
        res[name] = out / f"band_{name}.png"
        c.save(res[name])
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
