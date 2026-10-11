"""One library building three ways, before switching the library's surfaces over: the current
sheet-patch tiles (Blender), the material library in Blender (ambientCG, tinted) and the material
library in Unreal (Megascans when the project has the surface, else the same ambientCG maps).

    python scripts/diag/material_demo.py PROJECT ASSET [--no-ue]

Writes 3d/library/<asset>/material_demo/{patch,library}/ (GLB, maps, Blender Cycles renders),
library/unreal/threeq.png and compare.png (sheet view | patch | library Blender | library Unreal).
The asset's own procedural.glb is not touched.
"""
import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_pipeline import building_proc, kit_proc, library_review, ue  # noqa: E402
from asset_pipeline.project import ProjectStore, read_json  # noqa: E402


def panel(path: Path, label: str, h: int = 420) -> Image.Image:
    im = Image.open(path).convert("RGB") if path.is_file() else Image.new("RGB", (600, h), (60, 60, 60))
    im = im.resize((max(1, im.width * h // im.height), h), Image.LANCZOS)
    out = Image.new("RGB", (im.width, h + 30), "white")
    out.paste(im, (0, 30))
    ImageDraw.Draw(out).text((8, 8), label if path.is_file() else label + " (missing)", fill="black")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("asset")
    ap.add_argument("--no-ue", action="store_true")
    a = ap.parse_args()
    store = ProjectStore.open(a.project)
    brief = read_json(kit_proc.spec_path(store, a.asset))
    d = kit_proc.out_dir(store, a.asset) / "material_demo"
    for s in ("patch", "library"):
        t = time.time()
        building_proc.build(store, a.asset, out=d / s / "building.glb", surface=s, maps=d / s / "maps")
        library_review.render(store, a.asset, d / s / "building.glb", d / s / "blender", "cycles", brief["form"])
        print(f"{s}: built and rendered in {time.time() - t:.0f} s")
    if not a.no_ue:
        rep = ue.material_preview(store, a.asset, d / "library" / "building.glb", d / "library" / "maps" / "job.json",
                                  d / "library" / "unreal")
        print(json.dumps(rep["slots"], indent=1))
    brief = read_json(kit_proc.spec_path(store, a.asset))
    bands = brief["measured"].get("materials", {})
    words = ", ".join(f"{b} {bands[b]['type']}" for b in ("base", "wall", "top") if b in bands)
    ps = [panel(store.root / brief["view"], "sheet view"),
          panel(d / "patch" / "blender" / "threeq.png", "now: sheet patch (Blender)"),
          panel(d / "library" / "blender" / "threeq.png", f"library, ambientCG (Blender): {words}"),
          panel(d / "library" / "unreal" / "threeq.png", "library (Unreal)")]
    sheet = Image.new("RGB", (sum(p.width for p in ps) + 10 * (len(ps) - 1), ps[0].height), "white")
    x = 0
    for p in ps:
        sheet.paste(p, (x, 0))
        x += p.width + 10
    sheet.save(d / "compare.png")
    print(d / "compare.png")


if __name__ == "__main__":
    main()
