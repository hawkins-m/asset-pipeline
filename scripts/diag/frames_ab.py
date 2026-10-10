"""A/B of the concept-frame structure controls on a few shots of a world-mode project.

    python scripts/diag/frames_ab.py PROJECT SHOT [SHOT ...] [--seed 11 ...] [--set control|above]

Variants (same prompt and seeds per shot):
  control  ControlNet Union Pro 2.0 depth strength x end grid with canny at half the depth
           strength (ending 0.1 earlier), Union depth alone, and the BFL depth LoRA at two
           strengths.
  above    for raised and aerial shots: the default, stronger depth, depth + canny, and
           the relief depth image (sw_shots.depth_relief) at two strengths.
  above2   relief + canny, and relief released at half the steps.
Writes frames/_ab[_<set>]/<shot>/<variant>[_s<seed>]/ in the project, a contact sheet per
shot (<shot>.png: greybox preview first) and results.json with each frame's edge match
against the greybox (how closely it keeps the layout). --no-material-refs: wording only
(faster; the structure control is what's compared).
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
ABOVE = {
    "default_d0.6_e0.6": FrameSettings(),
    "plain_d0.8_e0.8": FrameSettings(depth_strength=0.8, depth_end=0.8),
    "plain_d0.6_e0.6_canny0.3": FrameSettings(canny_strength=0.3, canny_end=0.4),
    "relief_d0.6_e0.6": FrameSettings(depth_image="relief"),
    "relief_d0.75_e0.75": FrameSettings(depth_image="relief", depth_strength=0.75, depth_end=0.75),
}
ABOVE2 = {    # round 2: relief plus canny for raised shots, relief released earlier for aerials
    "relief_d0.75_e0.75_canny0.3": FrameSettings(depth_image="relief", depth_strength=0.75, depth_end=0.75,
                                                 canny_strength=0.3, canny_end=0.4),
    "relief_d0.75_e0.5": FrameSettings(depth_image="relief", depth_strength=0.75, depth_end=0.5),
}
for _fs in [*VARIANTS.values(), *ABOVE.values(), *ABOVE2.values()]:
    _fs.views = {}          # each variant is exactly what it says, whatever the shot's view
SETS = {"control": VARIANTS, "above": ABOVE, "above2": ABOVE2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("shots", nargs="+")
    ap.add_argument("--seed", type=int, nargs="+", default=[11])
    ap.add_argument("--set", choices=SETS, default="control")
    ap.add_argument("--no-material-refs", action="store_true")
    a = ap.parse_args()
    store = ProjectStore.open(a.project)
    root = store.root / s0_frames.FRAMES / ("_ab" if a.set == "control" else f"_ab_{a.set}")
    results = json.loads((root / "results.json").read_text()) if (root / "results.json").exists() else {}
    for shot in a.shots:
        tiles = [("greybox", sw_shots.shot_dir(store, shot) / "preview.png", None)]
        tiles += [None] * (len(a.seed) - 1)          # one row per variant when there are seeds
        for name, fs in SETS[a.set].items():
            for seed in a.seed:
                key = name if len(a.seed) == 1 else f"{name}_s{seed}"
                out = root / shot / key
                if not (out / "frame_000.png").exists():
                    t = time.time()
                    s0_frames.generate(store, shot, n=1, seed=seed, fs=fs, out=out,
                                       material_refs=not a.no_material_refs)
                    print(f"{shot} {key}: {time.time() - t:.0f}s", flush=True)
                rec = s0_frames.edge_match(out / "frame_000.png", sw_shots.shot_dir(store, shot) / "canny.png")
                results.setdefault(shot, {})[key] = rec
                tiles.append((f"{key}  match {rec:.2f}", out / "frame_000.png", rec))
                (root / "results.json").write_text(json.dumps(results, indent=1))
        w, h, cols = 640, 366, len(a.seed) if len(a.seed) > 1 else 4
        sheet = Image.new("RGB", (w * cols, h * ((len(tiles) + cols - 1) // cols)), "white")
        for i, tile in enumerate(tiles):
            if tile is None:
                continue
            label, p, _ = tile
            im = Image.open(p).convert("RGB").resize((w, h))
            ImageDraw.Draw(im).rectangle([0, 0, w, 22], fill="black")
            ImageDraw.Draw(im).text((6, 5), label, fill="white")
            sheet.paste(im, ((i % cols) * w, (i // cols) * h))
        sheet.save(root / f"{shot}.png")
    print(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
