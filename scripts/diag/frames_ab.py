"""A/B of the concept-frame structure controls on a few shots of a world-mode project.

    python scripts/diag/frames_ab.py PROJECT SHOT [SHOT ...] [--seed 11]

Variants (same prompt and seed per shot): ControlNet Union Pro 2.0 depth strength x end
grid with canny at half the depth strength (ending 0.1 earlier), Union depth alone, and
the BFL depth LoRA at two strengths. Writes frames/_ab/<shot>/<variant>/ in the project,
a contact sheet per shot (frames/_ab/<shot>.png: greybox preview first) and results.json
with each frame's edge match against the greybox (how closely it keeps the layout).
"""
import argparse
import json
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_pipeline.project import ProjectStore  # noqa: E402
from asset_pipeline.schema import FrameSettings  # noqa: E402
from asset_pipeline.stages import s0_frames, sw_shots  # noqa: E402

VARIANTS = {f"union_d{s:g}_e{e:g}": FrameSettings(model="union", depth_strength=s, depth_end=e,
                                                   canny_strength=s / 2, canny_end=max(e - 0.1, 0.1))
            for s in (0.4, 0.6, 0.8) for e in (0.4, 0.6, 0.8)}
VARIANTS["union_depthonly_d0.6_e0.6"] = FrameSettings(model="union", depth_strength=0.6, depth_end=0.6, canny_strength=0)
VARIANTS["lora_0.65"] = FrameSettings(model="depth_lora", depth_strength=0.65)
VARIANTS["lora_0.85"] = FrameSettings(model="depth_lora", depth_strength=0.85)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("shots", nargs="+")
    ap.add_argument("--seed", type=int, default=11)
    a = ap.parse_args()
    store = ProjectStore.open(a.project)
    root = store.root / s0_frames.FRAMES / "_ab"
    results = json.loads((root / "results.json").read_text()) if (root / "results.json").exists() else {}
    for shot in a.shots:
        tiles = [("greybox", sw_shots.shot_dir(store, shot) / "preview.png", None)]
        for name, fs in VARIANTS.items():
            out = root / shot / name
            if not (out / "frame_000.png").exists():
                t = time.time()
                s0_frames.generate(store, shot, n=1, seed=a.seed, fs=fs, out=out)
                print(f"{shot} {name}: {time.time() - t:.0f}s", flush=True)
            meta = json.loads((out / "meta.json").read_text())
            rec = meta["frames"][0]["edge_match"]
            results.setdefault(shot, {})[name] = rec
            tiles.append((f"{name}  match {rec:.2f}", out / "frame_000.png", rec))
            (root / "results.json").write_text(json.dumps(results, indent=1))
        w, h, cols = 640, 366, 4
        sheet = Image.new("RGB", (w * cols, h * ((len(tiles) + cols - 1) // cols)), "white")
        for i, (label, p, _) in enumerate(tiles):
            im = Image.open(p).convert("RGB").resize((w, h))
            ImageDraw.Draw(im).rectangle([0, 0, w, 22], fill="black")
            ImageDraw.Draw(im).text((6, 5), label, fill="white")
            sheet.paste(im, ((i % cols) * w, (i // cols) * h))
        sheet.save(root / f"{shot}.png")
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
