"""Summarise scripts/diag/vlm_ab.py runs.

    python scripts/diag/vlm_ab_report.py DIR metrics          # per-arm automatic metrics
    python scripts/diag/vlm_ab_report.py DIR blind --seed N   # shuffled plans for judging
                                                              # (key -> DIR/blind_key.json)
"""
import json
import random
import sys
from collections import defaultdict
from pathlib import Path


def load(root: Path) -> dict[str, list[dict]]:
    arms = defaultdict(list)
    for f in sorted(root.glob("*/*.json")):
        rec = json.loads(f.read_text())
        arms[rec["label"]].append(rec)
    return arms


def metrics(root: Path) -> None:
    print(f"{'arm':<18}{'valid 1st':>10}{'assets':>8}{'s/scene':>9}{'tok/s':>7}"
          f"{'box hit':>9}{'SAM finds':>11}")
    for label, recs in load(root).items():
        ok = [r for r in recs if "plan" in r]
        first = sum(r["attempts"] == 1 for r in ok)
        assets = [len(r["plan"]["assets"]) for r in ok]
        boxes = [b for r in ok for b in r.get("boxes", [])]
        with_box = [b for b in boxes if b["best_iou"] is not None]
        hit = sum(b["best_iou"] >= 0.3 for b in with_box)
        found = sum(b["sam_found"] > 0 for b in boxes)
        tps = [r["usage"]["timings"]["predicted_per_second"] for r in ok
               if r.get("usage", {}).get("timings", {}).get("predicted_per_second")]
        print(f"{label:<18}{f'{first}/{len(recs)}':>10}{sum(assets) / max(len(ok), 1):>8.1f}"
              f"{sum(r['seconds'] for r in recs) / len(recs):>9.0f}"
              f"{(sum(tps) / len(tps)) if tps else float('nan'):>7.0f}"
              f"{f'{hit}/{len(with_box)}':>9}{f'{found}/{len(boxes)}':>11}")


def blind(root: Path, seed: int) -> None:
    arms = load(root)
    by_scene = defaultdict(dict)
    for label, recs in arms.items():
        for r in recs:
            by_scene[r["scene"]][label] = r
    rng = random.Random(seed)
    key = {}
    for scene, recs in sorted(by_scene.items()):
        labels = list(recs)
        rng.shuffle(labels)
        key[scene] = {chr(65 + i): lab for i, lab in enumerate(labels)}
        print(f"\n######## {scene}")
        for i, lab in enumerate(labels):
            r = recs[lab]
            print(f"\n=== Plan {chr(65 + i)}")
            if "plan" not in r:
                print("  (no valid plan)")
                continue
            p = r["plan"]
            print(f"  summary: {p['summary']}\n  scale: {p['scale_notes']}")
            for a in p["assets"]:
                d = a["dimensions"]
                print(f"  - {a['name']} [{a['category']}] x{a['count']} "
                      f"{d['width']:g}x{d['depth']:g}x{d['height']:g}m"
                      + (f" kit={a['kit']}" if a["kit"] else "") + f" | {a['description'][:110]}")
    (root / "blind_key.json").write_text(json.dumps(key, indent=2))


if __name__ == "__main__":
    root, mode = Path(sys.argv[1]), sys.argv[2]
    if mode == "metrics":
        metrics(root)
    else:
        blind(root, int(sys.argv[sys.argv.index("--seed") + 1]) if "--seed" in sys.argv else 0)
