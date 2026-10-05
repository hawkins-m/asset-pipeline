"""Local ComfyUI image backend (default for every stage)."""
import random
from pathlib import Path

from ..comfy import workflow
from ..comfy.client import ComfyClient
from .base import BackendCapabilityError, GenRequest, GenResult


class ComfyUIBackend:
    name = "comfyui"

    def __init__(self, client: ComfyClient, workflows: dict[str, str]):
        self.client = client
        self.workflows = workflows

    def _anchor_values(self, req: GenRequest) -> list[dict]:
        """Upload anchor images; split the strength so k images pull as hard as one
        (attn_bias adds log(strength) per image's token block)."""
        images = req.anchor.images if req.anchor else []
        if not images:
            return []
        missing = [p for p in images if not Path(p).is_file()]
        if missing:
            raise FileNotFoundError(f"anchor image(s) missing: {missing}")
        per_image = req.anchor.strength / len(images)
        return [{"image": self.client.upload_image(Path(p)), "strength": per_image}
                for p in images]

    def generate(self, req: GenRequest, out_dir: Path, prefix: str = "img") -> list[GenResult]:
        if req.refs:
            raise BackendCapabilityError("reference images (refs) aren't supported by the "
                                         "ComfyUI backend yet")
        lora = req.lora or (req.anchor.lora if req.anchor else None)
        anchor = self._anchor_values(req)
        name = self.workflows["t2i_anchor"] if (anchor or lora) else self.workflows["t2i"]
        graph, manifest = workflow.load_template(name)
        seed = req.seed if req.seed is not None else random.randrange(2**32)
        values = {"prompt": req.prompt, "negative": req.negative, "width": req.width,
                  "height": req.height, "n": req.n, "seed": seed}
        if req.steps is not None:
            values["steps"] = req.steps
        if name == self.workflows["t2i_anchor"]:
            values["anchor"] = anchor
            values["lora"] = lora.model_dump() if lora else None
        images = self.client.run(workflow.fill(graph, manifest, values), manifest["outputs"])

        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for i, img in enumerate(images):
            path = out_dir / f"{prefix}_{i:03d}.png"
            path.write_bytes(img.data)
            # One batched latent: all images share the sampler seed (ComfyUI derives
            # per-image noise from it), so record the batch seed and index.
            results.append(GenResult(path=path, seed=seed, backend=self.name,
                                     meta={"workflow": name, "batch_index": i,
                                           "prompt": req.prompt,
                                           "anchor_images": [str(p) for p in req.anchor.images]
                                           if req.anchor else [],
                                           "lora": lora.model_dump() if lora else None,
                                           "ignored": []}))
        return results
