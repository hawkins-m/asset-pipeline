"""Persistent data models. Stage-specific models (AssetPlan, view-sets, ...) are added
with their stages; see the plan in PLAN.md."""
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field


class LoraRef(BaseModel):
    """A LoRA file in ComfyUI's models/loras, applied to the diffusion model."""
    file: str
    strength: float = 1.0


class StyleAnchor(BaseModel):
    """Curated stage-0 images that condition every later generation."""
    images: list[Path] = Field(default_factory=list)
    strength: float = 0.5
    lora: LoraRef | None = None


class Project(BaseModel):
    slug: str
    name: str = ""
    brief: str = ""
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    backends: dict[str, str] = Field(default_factory=dict)  # stage -> image backend
    llm: str = "gemini"
    anchor: StyleAnchor | None = None
