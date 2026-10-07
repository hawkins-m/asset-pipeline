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


class FrameSettings(BaseModel):
    """World mode concept frames: which structure control and how hard it holds.
    Defaults from the A/B in CLAUDE.md "Concept frames"."""
    model: Literal["union", "depth_lora"] = "union"
    depth_strength: float = Field(default=0.6, ge=0, le=2)
    depth_end: float = Field(default=0.6, ge=0, le=1)
    canny_strength: float = Field(default=0.0, ge=0, le=2)    # union only; 0 = depth alone (best, A/B)
    canny_end: float = Field(default=0.5, ge=0, le=1)
    steps: int = Field(default=28, ge=1, le=100)


class Project(BaseModel):
    slug: str
    name: str = ""
    brief: str = ""
    created: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    backends: dict[str, str] = Field(default_factory=dict)  # stage -> image backend
    llm: str = "local"            # vision LLM: local | gemini | claude
    anchor: StyleAnchor | None = None
    mode: Literal["scenes", "world"] = "scenes"  # world: one site, greybox, shots (PLAN.md)
    frames: "FrameSettings | None" = None        # world mode concept frames; None = defaults


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


# --- World mode: site layout, greybox slots, shots (PLAN.md "World mode") ---------------
# Coordinates are metres, Z up, Blender's right-handed frame; angles are degrees counter-
# clockwise from +X. A building's front faces its local -Y (as in stage 6).

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
BuildingType = Literal["temple", "block", "villa", "rotunda", "stoa"]


class District(BaseModel):
    """A part of the city with its own material notes (fed to the concept-frame prompt)."""
    id: str
    name: str = ""
    notes: str = ""                       # materials, palette, character
    radius: Vec2 = (0.0, 1e9)             # ring band from the centre
    sector: Vec2 = (0.0, 360.0)           # angle range (may wrap: (300, 60))


class Material(BaseModel):
    """A surface material, named precisely in concept-frame prompts. Vague words drift:
    "honed pale stone" came out as etched wood. `ref` (a project image, e.g. a moodboard
    swatch) is applied as Redux masked to this material's slots (the shot's id pass), so
    it doesn't restyle everything else."""
    id: str
    words: str                            # "polished white marble, fine grey veining, ..."
    types: list[str] = Field(default_factory=list)      # greybox slot types it covers
    districts: list[str] = Field(default_factory=list)  # only these districts (empty = all)
    ref: str | None = None                # project-relative reference image
    ref_strength: float = Field(default=0.15, ge=0, le=2)


class Kit(BaseModel):
    """A classical modular kit: pieces sit on a grid of `module_m` bays."""
    id: str
    module_m: float = Field(default=4.0, gt=0)       # bay width (column spacing)
    storey_m: float = Field(default=6.0, gt=0)       # column / pier height
    column_d_m: float = Field(default=0.9, gt=0)     # column diameter
    ring_radii: list[float] = Field(default_factory=list)  # radii curved pieces are made for


class Ring(BaseModel):
    id: str
    radius: float = Field(gt=0)           # centre line
    width: float = Field(gt=0)
    role: Literal["avenue", "canal", "garden", "terrace"] = "avenue"


class Radial(BaseModel):
    id: str
    angle: float
    width: float = Field(gt=0)
    r_from: float = 0.0
    r_to: float = Field(gt=0)


class Plaza(BaseModel):
    id: str
    center: Vec2 = (0.0, 0.0)
    radius: float = Field(gt=0)
    type: Literal["paved", "pool"] = "paved"
    district: str | None = None


class Plot(BaseModel):
    """One building. Straight types use center/rot/size; a stoa follows an arc."""
    id: str = ""
    type: BuildingType
    center: Vec2 = (0.0, 0.0)
    rot: float = 0.0                      # degrees; the front (-Y) faces rot - 90
    size: Vec3 = (20.0, 20.0, 12.0)       # width, depth, height (rotunda: drum diameter, -, total)
    arc: tuple[float, float, float] | None = None  # stoa: (radius, angle_from, angle_to)
    kit: str | None = None
    district: str | None = None


class RingRow(BaseModel):
    """`count` plots spread evenly around a circle, facing the centre, skipping radials."""
    id: str
    radius: float = Field(gt=0)
    count: int = Field(ge=1)
    type: BuildingType
    size: Vec3
    height_jitter: float = 0.0            # +- fraction of the height, seeded per plot
    angle_offset: float = 0.0
    kit: str | None = None
    district: str | None = None


class VegZone(BaseModel):
    """Where vegetation is scattered (phase 3+); `ring` names a garden ring."""
    id: str
    ring: str | None = None
    polygon: list[Vec2] = Field(default_factory=list)
    species: dict[str, float] = Field(default_factory=dict)   # name -> share
    density_per_100m2: float = 1.0


class TerrainSpec(BaseModel):
    """Fictional coastal terrain (generated), or a 16-bit heightmap PNG in the project."""
    extent_m: float = Field(default=2400.0, gt=0)  # square, centred on the origin
    resolution: int = Field(default=1009, ge=65)   # samples per side (UE-legal: 505, 1009, 2017)
    seed: int = 7
    sea_dir: float = -90.0                # direction from the city towards the sea
    shore_m: float = 520.0                # centre -> shoreline along sea_dir
    headland_m: float = 80.0              # the shore bulges out by this much in front of the city
    headland_width_m: float = 500.0
    hill_height_m: float = 90.0
    city_radius_m: float = 470.0          # flattened plateau
    city_z: float = 18.0
    heightmap: str | None = None          # project-relative PNG instead of generating
    z_range: Vec2 = (-40.0, 160.0)        # PNG 0..65535 <-> metres


class ShotSpec(BaseModel):
    """A camera as authored in the layout; the .blend's cameras are the truth after build."""
    id: str
    tier: Literal["wide", "medium", "tight"] = "medium"
    pos: Vec3
    look_at: Vec3
    lens_mm: float = 35.0
    sensor_mm: float = 36.0
    resolution: tuple[int, int] = (1344, 768)
    district: str | None = None
    notes: str = ""


class SiteLayout(BaseModel):
    name: str = ""
    sea_level: float = 0.0
    terrain: TerrainSpec = Field(default_factory=TerrainSpec)
    districts: list[District] = Field(default_factory=list)
    materials: list[Material] = Field(default_factory=list)
    kits: list[Kit] = Field(default_factory=list)
    rings: list[Ring] = Field(default_factory=list)
    radials: list[Radial] = Field(default_factory=list)
    plazas: list[Plaza] = Field(default_factory=list)
    plots: list[Plot] = Field(default_factory=list)
    rows: list[RingRow] = Field(default_factory=list)
    veg_zones: list[VegZone] = Field(default_factory=list)
    shots: list[ShotSpec] = Field(default_factory=list)


class Piece(BaseModel):
    """One mesh of a slot: a kit piece (shared mesh, instanced) or unique massing."""
    name: str
    piece: str                            # kit piece type ("column-shaft") or "mass"
    mesh: str                             # mesh datablock; identical pieces share one
    matrix_local: list[list[float]]       # 4x4 relative to the slot


class Slot(BaseModel):
    """A tagged greybox object (building, plaza, ring, terrain...) read from the .blend."""
    id: str
    type: str
    category: Category
    kit: str | None = None
    district: str | None = None
    matrix: list[list[float]]             # 4x4 world
    bbox: list[list[float]]               # [[x, y, z] min, [x, y, z] max], world
    pieces: list[Piece] = Field(default_factory=list)


class Shot(BaseModel):
    """A shot camera read from the .blend."""
    id: str
    tier: Literal["wide", "medium", "tight"] = "medium"
    matrix: list[list[float]]             # 4x4 world (Blender camera: looks down local -Z)
    lens_mm: float
    sensor_mm: float
    resolution: tuple[int, int]
    district: str | None = None
    notes: str = ""
