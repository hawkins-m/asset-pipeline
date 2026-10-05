"""Style-anchor A/B on live ComfyUI (scripts/diag/style_anchor_ab.py). Object-on-white
anchor vs scene anchor vs text, fixed seeds. Needs outputs/anchor_ab/anchor (scene set). Run: $AP_ROOT/envs/orchestrator/bin/python scripts/diag/style_anchor_ab.py"""
import time
from pathlib import Path

from PIL import Image

from asset_pipeline.imagegen.base import GenRequest
from asset_pipeline.imagegen.registry import make_backend
from asset_pipeline.schema import StyleAnchor

b = make_backend("comfyui")
D = Path("/mnt/storage/asset-pipeline/outputs/anchor_ab")
OUT = D / "obj_test"
STYLE = ("ukiyo-e woodblock print style, flat colours, bold black outlines, "
         "indigo and vermilion palette, visible paper grain")
PROMPT = "a wooden barrel, isolated on a plain white background, game asset concept"

t = time.time()
obj = b.generate(GenRequest(prompt="a single paper lantern, isolated on a plain white background, "
                            "game asset concept, " + STYLE, n=3, seed=21), OUT / "anchor", prefix="obj")
print(f"object anchors: {time.time() - t:.0f}s", flush=True)

scene_imgs = sorted((D / "anchor").glob("anchor_*.png"))
obj_imgs = [r.path for r in obj]
cases = [
    ("none", None),
    ("text_only", StyleAnchor(images=[], style_text=STYLE)),
    ("scene_r0.04_text", StyleAnchor(images=scene_imgs, strength=0.04, style_text=STYLE)),
    ("obj_r0.08", StyleAnchor(images=obj_imgs, strength=0.08)),
    ("obj_r0.08_text", StyleAnchor(images=obj_imgs, strength=0.08, style_text=STYLE)),
    ("obj_r0.15_text", StyleAnchor(images=obj_imgs, strength=0.15, style_text=STYLE)),
]
paths = []
for label, anchor in cases:
    t = time.time()
    r = b.generate(GenRequest(prompt=PROMPT, seed=7, anchor=anchor), OUT / "barrel", prefix=label)[0]
    paths.append(r.path)
    print(f"{label}: {time.time() - t:.0f}s {r.meta['workflow']}", flush=True)

# Contact sheet: row 1 = the 3 object anchors, row 2 = the 6 barrel cases.
tile = 256
sheet = Image.new("RGB", (6 * tile, 2 * tile), "white")
for i, p in enumerate(obj_imgs):
    sheet.paste(Image.open(p).convert("RGB").resize((tile, tile)), (i * tile, 0))
for i, p in enumerate(paths):
    sheet.paste(Image.open(p).convert("RGB").resize((tile, tile)), (i * tile, tile))
sheet.save(OUT / "contact.png")
print("DONE", OUT / "contact.png", flush=True)
