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
BuildingType = Literal["temple", "block", "villa", "rotunda", "stoa", "colonnade"]


DensityBand = Literal["core", "middle", "edge"]


class District(BaseModel):
    """A part of the city: its identity line (fed to the concept-frame prompt) and, in city
    mode, its typology mix, material palette, column policy and height bias."""
    id: str
    name: str = ""
    notes: str = ""                       # identity: character, materials, palette (one line)
    radius: Vec2 = (0.0, 1e9)             # ring band from the centre
    sector: Vec2 = (0.0, 360.0)           # angle range (may wrap: (300, 60))
    # city mode: typology -> weight per density band (core >= 0.6, middle >= 0.4, edge)
    mix: dict[DensityBand, dict[str, float]] = Field(default_factory=dict)
    palette: dict[str, float] = Field(default_factory=dict)   # material id -> weight
    columns: Literal["none", "rare", "accent", "accent_on_civic_only"] = "accent"
    height_bias: int = 0                  # storeys added to every building here
    merge_chance: float = Field(default=1.0, ge=0)   # x each typology's merge_chance here (0 = never)
    user_fields: list[str] = Field(default_factory=list)      # fields edited in the UI


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
    user_fields: list[str] = Field(default_factory=list)


Family = Literal["civic", "housing", "mixed", "infrastructure", "public_realm"]
Form = Literal["perimeter", "courtyard", "bar", "l_shape", "u_shape", "stepped", "tower", "podium_tower",
               "crescent", "row", "hall", "drum", "cavea", "cube", "arcade_line", "pavilion", "open"]
Place = Literal["core", "avenue", "interior", "crossing", "waterfront", "hillside", "corridor", "edge",
                "node_ring"]


class Typology(BaseModel):
    """A building type of the city-mode catalog. `form` is the greybox massing (what the
    depth pass hands to the frames); `prompt` the few words a frame prompt uses when it is
    on screen; `desc` the asset description that drives its reference sheets. Each
    building picks a roof, facade and ground-floor use from the pools (seeded)."""
    id: str
    name: str = ""
    family: Family = "housing"
    form: Form = "perimeter"
    width: Vec2 | None = None             # footprint range, m (per house for `row`)
    depth: Vec2 | None = None
    storeys: tuple[int, int] = (1, 1)
    place: list[Place] = Field(default_factory=list)
    prompt: str = ""
    desc: str = ""
    roofs: list[str] = Field(default_factory=list)
    facades: list[str] = Field(default_factory=list)
    ground: list[str] = Field(default_factory=list)
    columns: Literal["none", "accent", "order"] = "none"
    max_share: float | None = None        # of a district's buildings
    max_count: int | None = None          # city-wide
    material: str | None = None           # pinned material (else the district palette)
    ref: str | None = None                # landmark reference image (Redux masked to its slots)
    ref_strength: float = Field(default=0.12, ge=0, le=2)
    # chance (0-1) that a block given this type merges with 1-3 neighbouring blocks into one
    # large building (single-building forms only), times the district's merge_chance
    merge_chance: float = Field(default=0.0, ge=0, le=1)
    # where the generator puts it: auto (from form / place), block (assigned to city blocks),
    # corridor (across a green valley or along the promenade), shore (steps into the sea),
    # pier_end (the harbour's pier head), node_gate (where an avenue leaves a civic core),
    # kit (a kit monument the civic cores place: rotunda, temple, stoa)
    site: Literal["auto", "block", "corridor", "shore", "pier_end", "node_gate", "kit"] = "auto"
    user_fields: list[str] = Field(default_factory=list)


class Overlay(BaseModel):
    """Extra typologies wherever a zone applies (waterfront, hillside, corridor...)."""
    adds: dict[str, float] = Field(default_factory=dict)
    replaces_housing_with: str | None = None


class Variation(BaseModel):
    storeys_jitter: int = 1               # +- storeys around the typology's pick
    avenue_bonus: int = 1                 # +1 on avenue frontage
    step_back_top: float = 0.35           # share of 4+ storey buildings with a set-back top storey
    # a smooth height field drifting across each district (organized randomness, not rings)
    height_noise: float = Field(default=0.0, ge=0)        # +- storeys at the field's extremes
    height_noise_scale_m: float = Field(default=700.0, gt=0)  # distance between its highs and lows
    accent_chance: float = Field(default=0.0, ge=0, le=1)  # housing at an avenue crossing that rises
    accent_storeys: int = Field(default=3, ge=0)           # ... by this many storeys


class RepetitionChecks(BaseModel):
    """Thresholds of the anti-repetition report (warnings, like the housing share)."""
    max_typology_share_city: float = 0.22
    max_typology_share_district: float = 0.40
    min_types_per_urban_tile: int = 4
    max_identical_run: int = 3
    min_height_cv_block: float = 0.15
    max_column_share: float = 0.10
    district_diversity_min: float = 1.6


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
    center: Vec2 = (0.0, 0.0)
    district: str | None = None


class Radial(BaseModel):
    id: str
    angle: float
    width: float = Field(gt=0)
    r_from: float = 0.0
    r_to: float = Field(gt=0)
    center: Vec2 = (0.0, 0.0)


class Plaza(BaseModel):
    id: str
    center: Vec2 = (0.0, 0.0)
    radius: float = Field(gt=0)
    type: Literal["paved", "pool", "lagoon"] = "paved"   # lagoon: water nearly to the edge
    district: str | None = None


class Plot(BaseModel):
    """One building. Straight types use center/rot/size; a stoa follows an arc."""
    id: str = ""
    type: BuildingType
    center: Vec2 = (0.0, 0.0)
    rot: float = 0.0                      # degrees; the front (-Y) faces rot - 90
    size: Vec3 = (20.0, 20.0, 12.0)       # width, depth, height (rotunda: drum diameter, -, total)
    arc: tuple[float, float, float] | None = None  # stoa / colonnade: (radius, angle_from, angle_to)
    kit: str | None = None
    district: str | None = None
    material: str | None = None           # tags the slot (else layout materials match by type)
    typology: str | None = None           # catalog entry it belongs to (a landmark ensemble)


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
    center: Vec2 = (0.0, 0.0)


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
    hill_from_m: float = 150.0            # hills rise between these distances inland
    hill_to_m: float = 900.0
    coast_amp_m: float = 0.0              # shoreline irregularity (bays and points), +-
    coast_wavelength_m: float = 1500.0
    city_radius_m: float = 470.0          # flattened plateau (0: none; city mode uses pads)
    city_z: float = 18.0
    heightmap: str | None = None          # project-relative PNG instead of generating
    z_range: Vec2 = (-40.0, 160.0)        # PNG 0..65535 <-> metres


class CivicNode(BaseModel):
    """A circular civic centre of a city (city mode): plaza, rings and civic buildings,
    avenues radiating out. Monuments are rare: at most one per node, and most have none."""
    id: str
    center: Vec2
    radius: float = Field(default=200.0, gt=40)   # civic core (plaza + rings + civic blocks)
    role: Literal["forum", "harbour", "market", "hill", "local"] = "local"
    monument: Literal["rotunda", "temple"] | None = None
    radials: int = Field(default=6, ge=0, le=16)
    rot: float = 0.0                      # angle of the first radial
    spiral_deg_per_100m: float = 0.0      # avenues curve (a spiral "galaxy" arm); 0 = straight
    avenue_m: float = 1200.0              # how far its avenues reach into the city
    weight: float = Field(default=1.0, gt=0)      # pull on the density field (size of its core)
    notes: str = ""                       # character, fed to frame prompts as a district


class CitySpec(BaseModel):
    """City-scale layout mode: civic nodes, terrain-following avenues, organic blocks and
    parcels between them, and a density field from dense mid-rise cores to low-rise
    outskirts. Housing (everything outside the civic cores) is lightweight instanced
    massing grouped per tile, never a per-building asset."""
    seed: int = 7
    nodes: list[CivicNode] = Field(default_factory=list)
    bounds: tuple[float, float, float, float] = (-2500.0, -1500.0, 2500.0, 1500.0)  # x0, y0, x1, y1
    density_falloff_m: float = 650.0      # node pull: exp(-(d / (falloff * weight)) ^ 2)
    urban_threshold: float = 0.12         # below this density: countryside (fraying edge)
    max_slope: float = 0.3                # steeper than this: no building (rise / run)
    avenue_w: float = 24.0
    arterial_w: float = 18.0
    street_w: float = 9.0
    promenade_w: float = 20.0
    block_m: tuple[float, float] = (90.0, 170.0)   # block size: dense core .. outskirts
    parcel_m: tuple[float, float] = (22.0, 34.0)   # parcel frontage: core .. outskirts
    storeys: tuple[int, int, int, int] = (5, 8, 2, 3)  # core min..max, outskirts min..max
    storey_m: float = 3.4
    green_corridors: int = Field(default=3, ge=0)  # valleys from the hills to the sea
    corridor_w: float = 70.0
    park_share: float = 0.06              # extra blocks left green, mostly at the outskirts
    markets: int = Field(default=4, ge=0) # market squares at avenue crossings
    trees: bool = True                    # street and park trees (stand-ins for the scatter)
    tile_m: float = 400.0                 # housing and streets grouped into slots per tile
    # typology catalog (empty: the legacy three housing types)
    typologies: list[Typology] = Field(default_factory=list)
    overlays: dict[str, Overlay] = Field(default_factory=dict)  # waterfront | hillside | corridor | crossing | node_ring
    variation: Variation = Field(default_factory=Variation)
    checks: RepetitionChecks = Field(default_factory=RepetitionChecks)
    user_fields: list[str] = Field(default_factory=list)   # catalog sections edited in the UI


Tier = Literal["wide", "medium", "tight"]


class ShotSpec(BaseModel):
    """A camera as authored in the layout; the .blend's cameras are the truth after build.
    pos / look_at / lens_mm / tier are the auto (or authored) values; the user's edits are
    kept apart in the fields below, so re-placing the auto shots keeps them and the UI can
    show which is which. `effective()` applies them."""
    id: str
    tier: Tier = "medium"
    pos: Vec3
    look_at: Vec3
    lens_mm: float = 35.0
    sensor_mm: float = 36.0
    resolution: tuple[int, int] = (1344, 768)
    district: str | None = None
    notes: str = ""
    # user edits
    prompt_append: str = ""               # added to the auto prompt
    prompt_override: str | None = None    # replaces the auto prompt entirely
    nudge_pos: Vec3 = (0.0, 0.0, 0.0)     # metres in the camera's frame: right, up, forward
    nudge_target: Vec3 = (0.0, 0.0, 0.0)
    lens_override: float | None = Field(default=None, gt=0)
    tier_override: Tier | None = None

    def camera_edited(self) -> bool:
        return (any(self.nudge_pos) or any(self.nudge_target) or self.lens_override is not None
                or self.tier_override is not None)

    def effective(self) -> "ShotSpec":
        """The camera with the user's nudges, lens and tier applied."""
        import numpy as np
        p, t = np.array(self.pos, float), np.array(self.look_at, float)
        fwd = t - p
        fwd /= max(float(np.linalg.norm(fwd)), 1e-9)
        right = np.cross(fwd, [0.0, 0.0, 1.0])
        if np.linalg.norm(right) < 1e-6:          # looking straight down
            right = np.array([1.0, 0.0, 0.0])
        right /= np.linalg.norm(right)
        up = np.cross(right, fwd)
        move = lambda n: right * n[0] + up * n[1] + fwd * n[2]  # noqa: E731
        return self.model_copy(update={
            "pos": tuple(round(float(v), 3) for v in p + move(self.nudge_pos)),
            "look_at": tuple(round(float(v), 3) for v in t + move(self.nudge_target)),
            "lens_mm": self.lens_override or self.lens_mm, "tier": self.tier_override or self.tier})


class SiteLayout(BaseModel):
    name: str = ""
    sea_level: float = 0.0
    terrain: TerrainSpec = Field(default_factory=TerrainSpec)
    districts: list[District] = Field(default_factory=list)
    city: CitySpec | None = None          # city-scale mode (sw_city): expands into the rest
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
    material: str | None = None           # material id the slot is tagged with (city typologies)
    typology: str | None = None           # catalog typology (city buildings, landmark ensembles)
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
