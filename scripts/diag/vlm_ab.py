"""A/B stage-1 scene analysis across vision LLMs on the same scenes.

Runs the exact stage-1 prompt and schema (s1_plan) against one already-running backend
and writes, per scene, the plan plus timing, attempts, tokens and a box check: the best
IoU between each LLM box and any SAM 3.1 detection of the asset's noun (needs ComfyUI).
Nothing is written into the project.

    python scripts/diag/vlm_ab.py --label 8b-bf16 --backend local PROJECT SCENE... --out DIR
    python scripts/diag/vlm_ab.py --label 32b-q5 --backend llamacpp --url http://127.0.0.1:8711 ...
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from asset_pipeline import config, segment  # noqa: E402
from asset_pipeline.comfy.client import ComfyClient  # noqa: E402
from asset_pipeline.llm.base import LLMError, structured  # noqa: E402
from asset_pipeline.llm.llamacpp import LlamaCppVision  # noqa: E402
from asset_pipeline.llm.local import LocalVision  # noqa: E402
from asset_pipeline.project import ProjectStore  # noqa: E402
from asset_pipeline.stages import s1_plan  # noqa: E402


# Qwen3-VL Instruct model card: temperature 0.7, top_p 0.8, top_k 20, presence_penalty 1.5.
QWEN_SAMPLING = {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5, "seed": 0}


class Counting:
    """Wraps a backend to count attempts (structured() retries on invalid JSON)."""
    def __init__(self, llm):
        self.llm, self.name, self.calls = llm, llm.name, 0

    def json_text(self, *a):
        self.calls += 1
        return self.llm.json_text(*a)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project")
    ap.add_argument("scenes", nargs="+", help="project-relative scene paths")
    ap.add_argument("--label", required=True)
    ap.add_argument("--backend", choices=["local", "llamacpp"], required=True)
    ap.add_argument("--url", default="")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--no-sam", action="store_true")
    ap.add_argument("--sampling", choices=["greedy", "qwen"], default="greedy",
                    help="qwen: Qwen3-VL Instruct's recommended settings (llamacpp only)")
    a = ap.parse_args()

    store = ProjectStore.open(a.project)
    sampling = QWEN_SAMPLING if a.sampling == "qwen" else None
    llm = LocalVision(a.url) if a.backend == "local" else LlamaCppVision(a.url, sampling=sampling)
    comfy = None if a.no_sam else ComfyClient(config.backends()["comfyui"]["url"])
    brief = store.load().brief.strip()
    prompt = s1_plan.PROMPT.format(brief=f"\nThe artist's brief for this scene: {brief}" if brief else "",
                                   max_assets=s1_plan.MAX_ASSETS)
    out = a.out / a.label
    out.mkdir(parents=True, exist_ok=True)
    for key in a.scenes:
        wrapped = Counting(llm)
        t = time.time()
        rec = {"label": a.label, "scene": key}
        try:
            analysis = structured(wrapped, prompt, [store.root / key], s1_plan.SceneAnalysis,
                                  system=s1_plan.SYSTEM)
            plan = s1_plan.to_plan(analysis, key, a.label)
            rec["plan"] = plan.model_dump(mode="json")
        except LLMError as e:
            rec["error"], rec["raw"] = str(e), e.raw
            plan = None
        rec |= {"seconds": round(time.time() - t, 1), "attempts": wrapped.calls,
                "usage": getattr(llm, "last_usage", {})}
        if plan and comfy:
            from PIL import Image
            w, h = Image.open(store.root / key).size
            boxes = []
            for x in plan.assets:
                dets = segment.detect(comfy, store.root / key, x.noun or x.name)
                best = max((s1_plan._box_iou(x.bbox, [d.bbox[0] / w, d.bbox[1] / h,
                                                      d.bbox[2] / w, d.bbox[3] / h])
                            for d in dets), default=0.0) if x.bbox else None
                boxes.append({"id": x.id, "noun": x.noun, "sam_found": len(dets),
                              "best_iou": None if best is None else round(best, 3)})
            rec["boxes"] = boxes
        (out / f"{s1_plan.plan_name(key)}.json").write_text(json.dumps(rec, indent=2))
        n = len(plan.assets) if plan else 0
        print(f"{a.label} {key}: {n} assets, {rec['attempts']} attempt(s), {rec['seconds']}s"
              + (f"  ERROR {rec['error']}" if "error" in rec else ""), flush=True)


if __name__ == "__main__":
    main()
