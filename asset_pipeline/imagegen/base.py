"""Image-generation adapter contract. Every stage generates images through this, so the
backend (local ComfyUI, Gemini, ...) is a per-stage choice in project.json."""
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from ..schema import LoraRef, StyleAnchor


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


class GenResult(BaseModel):
    path: Path
    seed: int | None
    backend: str
    meta: dict = Field(default_factory=dict)  # includes "ignored": request fields not honoured


class BackendCapabilityError(RuntimeError):
    """The backend can't honour a request field it is expected to support (yet)."""


class ImageGenBackend(Protocol):
    name: str

    def generate(self, req: GenRequest, out_dir: Path, prefix: str = "img") -> list[GenResult]:
        ...
