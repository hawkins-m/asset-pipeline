"""Image-generation adapter contract. Every stage generates images through this, so the
backend (local ComfyUI, Gemini, ...) is a per-stage choice in project.json."""
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from ..schema import LoraRef, StyleAnchor


class ControlImage(BaseModel):
    """A structure image the result must follow (world mode: rendered from the greybox)."""
    kind: Literal["depth", "canny"]
    image: Path
    strength: float = Field(default=0.6, ge=0, le=2)
    start: float = Field(default=0.0, ge=0, le=1)   # fraction of the sampling steps
    end: float = Field(default=0.6, ge=0, le=1)


class GenRequest(BaseModel):
    prompt: str
    negative: str = ""
    width: int = 1024
    height: int = 1024
    n: int = 1
    seed: int | None = None          # None -> random; seeds the whole batch of n images
    anchor: StyleAnchor | None = None
    lora: LoraRef | None = None
    refs: list[Path] = Field(default_factory=list)  # extra reference images
    steps: int | None = None         # backend default when None
    control: list[ControlImage] = Field(default_factory=list)
    # union: Flux ControlNet Union Pro 2.0 (depth and/or canny); depth_lora: BFL
    # FLUX.1-Depth-dev LoRA (depth only, strict). See CLAUDE.md "Concept frames".
    control_model: Literal["union", "depth_lora"] = "union"


class GenResult(BaseModel):
    path: Path
    seed: int | None
    backend: str
    meta: dict = Field(default_factory=dict)  # includes "ignored": request fields not honoured


def effective_prompt(req: GenRequest) -> str:
    """The prompt every backend sends: the request prompt plus the anchor's style text."""
    style = req.anchor.style_text.strip() if req.anchor else ""
    return f"{req.prompt.rstrip(' ,.')}, {style}" if style else req.prompt


class BackendCapabilityError(RuntimeError):
    """The backend can't honour a request field it is expected to support (yet)."""


class ImageGenBackend(Protocol):
    name: str

    def generate(self, req: GenRequest, out_dir: Path, prefix: str = "img") -> list[GenResult]:
        ...
