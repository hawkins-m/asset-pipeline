"""Vision-LLM adapter contract (scene analysis, labelling views, prompt drafting).

Backends turn (system, prompt, images, JSON schema) into JSON text. structured() then
validates it against a pydantic model and, when it doesn't validate, asks again with
the validation errors (at most `retries` times) before giving up with the raw text.
"""
import base64
import io
import json
from pathlib import Path
from typing import Protocol, TypeVar

from PIL import Image
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    def __init__(self, msg: str, raw: str | None = None):
        super().__init__(msg)
        self.raw = raw


class LLMRefusal(LLMError):
    """The provider's safety system declined the request (not retried)."""


class VisionLLM(Protocol):
    name: str

    def json_text(self, system: str, prompt: str, images: list[Path], schema: dict) -> str:
        """One call; returns the model's JSON text (may be invalid)."""
        ...


def structured(llm: VisionLLM, prompt: str, images: list[Path], model: type[T],
               system: str = "", retries: int = 2) -> T:
    if hasattr(llm, "parse"):  # provider-side schema-constrained decoding (Claude)
        return llm.parse(system, prompt, images, model)
    schema = model.model_json_schema()
    ask, raw = prompt, None
    for attempt in range(retries + 1):
        raw = llm.json_text(system, ask, images, schema)
        try:
            return model.model_validate_json(_strip_fences(raw))
        except ValidationError as e:
            problems = e.errors(include_url=False, include_input=False)
        ask = (f"{prompt}\n\nYour previous answer did not match the required JSON schema. "
               f"Problems: {json.dumps(problems, default=str)[:2000]}\n"
               f"Previous answer:\n{raw[:4000]}\n\nReply again with only corrected JSON.")
    raise LLMError(f"{llm.name}: no valid {model.__name__} after {retries + 1} attempts", raw=raw)


def _strip_fences(text: str) -> str:
    """Local models often wrap JSON in ```json fences; providers with schema mode don't."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


def encode_image(path: Path, max_side: int = 1568) -> tuple[bytes, str]:
    """PNG/JPEG bytes no larger than max_side (providers downscale anyway; this keeps
    uploads small). Returns (data, mime_type)."""
    im = Image.open(path)
    im.load()
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    if im.mode in ("RGBA", "LA", "P"):
        im.convert("RGBA").save(buf, "PNG")
        return buf.getvalue(), "image/png"
    im.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
