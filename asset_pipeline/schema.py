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
    # Total Flux Redux strength (attn_bias), split across images. Measured 2026-10-04:
    # <= 0.06 barely styles, ~0.08 styles while keeping the prompt's subject, >= 0.12
    # replaces the subject with the anchor's content. See CLAUDE.md.
    strength: float = 0.08
    # Editable style descriptor (medium, palette, linework...) appended to every prompt.
    # It carries most of the style, since Redux alone can't without leaking content.
    style_text: str = ""
    lora: LoraRef | None = None


class Project(BaseModel):
    slug: str
    name: str = ""
    brief: str = ""
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    backends: dict[str, str] = Field(default_factory=dict)  # stage -> image backend
    llm: str = "gemini"
    anchor: StyleAnchor | None = None
