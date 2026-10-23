"""Ground height under a point or a footprint, for any project.

`Ground.from_project` reads the terrain the project's greybox stands on: site/terrain.png with
site/terrain.json (extent, z range, rows north -> south; any size or resolution), or computes it
from the layout when they haven't been written yet. A project without a site layout gets flat
ground at the plan's base height (0 m).

`under(x, y, w, d, yaw, rule)` samples the footprint (a 5 x 5 grid, corners included):
    lowest   the lowest ground under it (default: nothing floats on a slope; the uphill side
             sits in the ground, as a building on a hillside does)
    centre   the ground at its centre
    mean     the mean over the footprint
    highest  the highest ground under it (nothing is buried; the downhill side floats)
The rule is a project setting (project.json "unreal": {"ground_rule": ...}, UnrealSettings).
"""
import math

import numpy as np

RULES = ("lowest", "centre", "mean", "highest")


class Ground:
    """heights: a square grid of metres, row 0 = north (+Y), centred on the origin over extent_m
    (None: flat at base)."""

    def __init__(self, heights: np.ndarray | None, extent_m: float = 0.0, base: float = 0.0):
        from .greybox import Heights
        self.sample = None if heights is None else Heights(np.asarray(heights, float), float(extent_m))
        self.base = float(base)

    @classmethod
    def from_project(cls, store) -> "Ground":
        from PIL import Image

        from . import greybox
        from .project import read_json
        from .stages import sw_site
        site = sw_site.site_dir(store)
        meta, png = read_json(site / "terrain.json", default=None), site / "terrain.png"
        if meta and png.is_file():
            h = greybox.from_png16(np.array(Image.open(png)), tuple(meta["z_range"]))
            return cls(h, meta["extent_m"])
        try:
            layout = sw_site.load_layout(store)
        except (FileNotFoundError, ValueError):
            return cls(None)
        return cls(sw_site.terrain(store, layout, write=False), layout.terrain.extent_m)

    def height(self, x: float, y: float) -> float:
        """Bilinear height at (x, y) (clamped to the grid's edge; greybox.Heights)."""
        return self.base if self.sample is None else self.sample(x, y)

    def under(self, x: float, y: float, w: float, d: float, yaw: float = 0.0, rule: str = "lowest",
              n: int = 5) -> float:
        """The ground height a footprint (centre x, y; size w along its own x, d; yaw degrees) stands
        on, by rule."""
        if rule not in RULES:
            raise ValueError(f"ground rule must be one of {RULES}, not {rule!r}")
        if rule == "centre":
            return self.height(x, y)
        a = math.radians(yaw)
        ca, sa = math.cos(a), math.sin(a)
        u, v = np.meshgrid(np.linspace(-w / 2, w / 2, n), np.linspace(-d / 2, d / 2, n))
        px, py = x + u * ca - v * sa, y + u * sa + v * ca
        zs = np.full(px.shape, self.base) if self.sample is None else self.sample.many(px, py)
        return float(zs.min() if rule == "lowest" else zs.max() if rule == "highest" else zs.mean())
