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

    def _template(self, req: GenRequest) -> str:
        if req.anchor or req.lora or req.refs:
            # flux_t2i_anchor (Flux Redux + optional LoRA) arrives with stage 0.
            raise BackendCapabilityError(
                "style anchor / LoRA / reference images need the anchor workflow "
                "(build step 2); only plain text-to-image is available so far")
        return self.workflows["t2i"]

    def generate(self, req: GenRequest, out_dir: Path, prefix: str = "img") -> list[GenResult]:
        name = self._template(req)
        graph, manifest = workflow.load_template(name)
        seed = req.seed if req.seed is not None else random.randrange(2**32)
        values = {"prompt": req.prompt, "negative": req.negative, "width": req.width,
                  "height": req.height, "n": req.n, "seed": seed}
        if req.steps is not None:
            values["steps"] = req.steps
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
                                           "prompt": req.prompt, "ignored": []}))
        return results
