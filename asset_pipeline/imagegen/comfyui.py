"""Local ComfyUI image backend (default for every stage)."""
import random
from pathlib import Path

from ..comfy import workflow
from ..comfy.client import ComfyClient
from .base import BackendCapabilityError, GenRequest, GenResult, effective_prompt


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

    def _control_values(self, req: GenRequest) -> dict:
        """Upload control images; one binding per kind (None removes that control)."""
        kinds = [c.kind for c in req.control]
        if len(set(kinds)) != len(kinds):
            raise BackendCapabilityError(f"one control image per kind, got {kinds}")
        if req.control_model == "depth_lora" and kinds != ["depth"]:
            raise BackendCapabilityError("the depth LoRA takes exactly one depth image (no canny)")
        out = {} if req.control_model == "depth_lora" else {"depth": None, "canny": None}
        for c in req.control:
            if not Path(c.image).is_file():
                raise FileNotFoundError(f"control image missing: {c.image}")
            v = {"image": self.client.upload_image(Path(c.image)), "strength": c.strength}
            if req.control_model == "union":
                v |= {"start": c.start, "end": c.end}
            out[c.kind] = v
        return out

    def generate(self, req: GenRequest, out_dir: Path, prefix: str = "img") -> list[GenResult]:
        if req.refs:
            raise BackendCapabilityError("reference images (refs) aren't supported by the "
                                         "ComfyUI backend yet")
        lora = req.lora or (req.anchor.lora if req.anchor else None)
        anchor = self._anchor_values(req)
        control = self._control_values(req) if req.control else {}
        if control:
            name = self.workflows["control_union" if req.control_model == "union" else "depth_lora"]
        else:
            name = self.workflows["t2i_anchor"] if (anchor or lora) else self.workflows["t2i"]
        graph, manifest = workflow.load_template(name)
        seed = req.seed if req.seed is not None else random.randrange(2**32)
        prompt = effective_prompt(req)
        values = {"prompt": prompt, "negative": req.negative, "width": req.width,
                  "height": req.height, "n": req.n, "seed": seed}
        if req.steps is not None:
            values["steps"] = req.steps
        if name != self.workflows["t2i"]:
            values["anchor"] = anchor
            values["lora"] = lora.model_dump() if lora else None
        values |= control
        # the depth LoRA's latent takes its size from the control image
        values = {k: v for k, v in values.items() if k in manifest["bindings"] or k not in ("width", "height")}
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
                                           "prompt": prompt,
                                           "anchor_images": [str(p) for p in req.anchor.images]
                                           if req.anchor else [],
                                           "lora": lora.model_dump() if lora else None,
                                           "control": [c.model_dump(mode="json") for c in req.control],
                                           "ignored": []}))
        return results
