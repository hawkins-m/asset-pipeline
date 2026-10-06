"""Gemini image backend (Google Gemini API, paid). Optional per stage; ComfyUI is default.

Mapping from GenRequest:
- prompt (+ anchor style text) and anchor/ref images go in as one multimodal request;
  anchor images are labelled as style references, not content to copy.
- width/height -> nearest supported aspect ratio (Gemini picks the pixel size).
- negative -> appended as "Avoid: ..." (no negative prompt in the API).
- n -> n requests (one image each), seeds seed, seed+1, ... when a seed is given.
- lora, steps -> not supported; listed in meta["ignored"].
"""
import io
from pathlib import Path

from PIL import Image

from ..paid import require_paid_allowed
from .base import BackendCapabilityError, GenRequest, GenResult, effective_prompt

DEFAULT_MODEL = "gemini-3.1-flash-image-preview"
ASPECTS = {"1:1": 1.0, "3:2": 1.5, "2:3": 2 / 3, "4:3": 4 / 3, "3:4": 0.75, "16:9": 16 / 9,
           "9:16": 9 / 16, "21:9": 21 / 9, "4:5": 0.8, "5:4": 1.25}
STYLE_REF = ("Match the visual style (rendering, palette, linework, lighting) of the style "
             "reference images above. Do not copy their objects or composition.")


def nearest_aspect(width: int, height: int) -> str:
    r = width / height
    return min(ASPECTS, key=lambda k: abs(ASPECTS[k] - r))


class GeminiImageBackend:
    name = "gemini"

    def __init__(self, model: str = "", client=None):
        self.model = model or DEFAULT_MODEL
        self._client = client

    @property
    def client(self):
        if self._client is None:
            from ..llm.gemini import gemini_client
            self._client = gemini_client()
        return self._client

    def _contents(self, req: GenRequest) -> list:
        from google.genai import types
        from ..llm.base import encode_image
        parts = []
        anchor_imgs = list(req.anchor.images) if req.anchor else []
        for p in anchor_imgs + list(req.refs):
            data, mime = encode_image(Path(p))
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        prompt = effective_prompt(req)
        if req.negative:
            prompt += f". Avoid: {req.negative}"
        if anchor_imgs:
            prompt = f"{STYLE_REF}\n\n{prompt}"
        parts.append(prompt)
        return parts

    def generate(self, req: GenRequest, out_dir: Path, prefix: str = "img") -> list[GenResult]:
        from google.genai import types
        if req.control:
            raise BackendCapabilityError("control images (depth/canny) need the ComfyUI backend")
        ignored = [f for f, v in [("lora", req.lora or (req.anchor.lora if req.anchor else None)),
                                  ("steps", req.steps)] if v]
        aspect = nearest_aspect(req.width, req.height)
        contents = self._contents(req)
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for i in range(req.n):
            require_paid_allowed("Gemini API")  # checked per image: n requests = n charges
            seed = None if req.seed is None else req.seed + i
            config = types.GenerateContentConfig(
                response_modalities=["IMAGE"], seed=seed,
                image_config=types.ImageConfig(aspect_ratio=aspect))
            response = self.client.models.generate_content(
                model=self.model, contents=contents, config=config)
            data = _first_image(response)
            path = out_dir / f"{prefix}_{i:03d}.png"
            im = Image.open(io.BytesIO(data))
            im.save(path)
            results.append(GenResult(path=path, seed=seed, backend=self.name, meta={
                "model": self.model, "aspect_ratio": aspect, "size": list(im.size),
                "prompt": contents[-1], "ignored": ignored}))
        return results


def _first_image(response) -> bytes:
    for cand in getattr(response, "candidates", None) or []:
        for part in getattr(cand.content, "parts", None) or []:
            blob = getattr(part, "inline_data", None)
            if blob is not None and blob.data:
                return blob.data
    text = getattr(response, "text", None)
    raise RuntimeError(f"Gemini returned no image (text: {text!r}, "
                       f"feedback: {getattr(response, 'prompt_feedback', None)})")
