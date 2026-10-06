"""Persistent data models. Stage-specific models (view-sets, ...) are added with their
stages; see the plan in PLAN.md."""
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, get_args

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
    llm: str = "local"            # vision LLM: local | gemini | claude
    anchor: StyleAnchor | None = None


# --- Stage 1: asset plan ---------------------------------------------------------------

Category = Literal["building", "structure", "prop", "vegetation", "rock", "terrain", "vehicle", "other"]
CATEGORIES = get_args(Category)


class Dimensions(BaseModel):
    """Real-world size in metres (width = left-right, depth = front-back as seen)."""
    width: float = Field(gt=0, le=1000)
    depth: float = Field(gt=0, le=1000)
    height: float = Field(gt=0, le=1000)


class PlanAsset(BaseModel):
    """One thing to model. Identical copies in the scene are one asset with a count."""
    id: str = ""                  # stable slug; later stages name files after it
    name: str
    noun: str = ""                # short generic noun SAM 3.1 is prompted with ("market stall")
    category: Category = "prop"
    description: str = ""         # the object alone (shape, materials, colours): the stage 2 prompt
    count: int = Field(default=1, ge=1)
    dimensions: Dimensions
    kit: str | None = None        # modular set; stage 2 draws a kit's pieces in one sheet
    placement: str = ""           # where it sits in the scene
    bbox: list[float] | None = None  # [x0, y0, x1, y1] as fractions of the scene image
    bbox_source: Literal["llm", "sam"] = "llm"
    bbox_llm: list[float] | None = None  # the LLM's own box, kept when SAM replaces bbox
    sam_found: int | None = None  # how many SAM 3.1 found for the name (vs count); None = not run
    mask: str | None = None       # project-relative SAM mask (.npz) of the chosen copy
    # game: retopo for engines; cine: Blender-only, topology doesn't matter; hero: the
    # multi-view path (orbit video -> multi-view 3D, PLAN.md) once it exists.
    usage: Literal["game", "cine", "hero"] = "game"
    include: bool = True          # unticked assets stay in the plan but aren't generated


class Relation(BaseModel):
    subject: str                  # asset id
    relation: str                 # "on top of", "next to", "attached to", ...
    object: str                   # asset id


class AssetPlan(BaseModel):
    scene: str                    # project-relative scene image
    summary: str = ""
    scale_notes: str = ""         # what the LLM judged real-world size from
    assets: list[PlanAsset] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    llm: str = ""                 # provider that drafted it
    dropped: list[str] = Field(default_factory=list)  # LLM "assets" dropped as backdrop (> 200 m)
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    edited: datetime | None = None  # last save from the editor; re-analysis then needs force
