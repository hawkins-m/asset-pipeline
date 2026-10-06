"""Stage 2: asset plans -> reference sheets on white, conditioned on the style anchor.

Each included asset becomes a "unit":
- object:   one asset, drawn three times side by side in one image. Asked for as
            front/side/back; Flux.1-dev mostly gives three matching three-quarter views
            (tested 2026-10-05), which stage 3 cuts apart and stage 4 picks from.
- kit:      all included assets sharing a `kit` name in one plan, in one sheet, so trim
            and proportions match (a kit of one is just an object).
- material: terrain (the ground the scene stands on) as a top-down texture swatch. Style
            text only: object anchors would paint objects into the texture.

Layout under the project:
    refs/<plan name>/<asset id | kit-<slug>>/sheet_NNN.png + meta.json
"""
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..imagegen.base import GenRequest
from ..imagegen.registry import backend_for
from ..project import ProjectStore, read_json, write_json
from ..schema import PlanAsset, StyleAnchor
from . import s1_plan

REFS = "refs"
PIXELS = 1664 * 704  # ~1.2 MP: what the sheet tests ran at (~44 s per image on GPU 1)

OBJECT_PROMPT = (
    "Three-view orthographic turnaround reference of one {name} for a 3D modeller. "
    "{description}. LEFT: front view, facing the viewer. MIDDLE: side view, the object "
    "rotated 90 degrees, seen exactly in profile. RIGHT: back view, the object rotated 180 "
    "degrees, showing its rear. Same object, same size, standing upright on one shared "
    "baseline, eye-level camera, evenly spaced, isolated on a plain pure white background, "
    "no ground, no shadow, no text")
KIT_PROMPT = (
    "Modular kit sheet for a 3D modeller: {n} matching pieces of one {kit}, drawn side by "
    "side at the same scale, each seen from the front, with identical trim, materials and "
    "proportions so they fit together. {pieces}. Isolated on a plain pure white background, "
    "no ground, no shadow, no text")
MATERIAL_PROMPT = (
    "Top-down view of a square patch of {name}: {description}. Seen straight from above, "
    "flat and evenly lit, filling the whole image, seamless tileable texture, no objects, "
    "no shadows, no text")


@dataclass
class Unit:
    plan: str
    key: str                      # asset id, or kit-<slug>
    kind: str                     # object | kit | material
    assets: list[PlanAsset] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.assets[0].kit if self.kind == "kit" else self.assets[0].name

    def dir(self, store: ProjectStore) -> Path:
        return store.root / REFS / self.plan / self.key


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "kit"


def units(store: ProjectStore, plan: str | None = None) -> list[Unit]:
    out = []
    names = [plan] if plan else [s1_plan.plan_name(k) for k in s1_plan.scenes(store)]
    for name in names:
        p = s1_plan.load(store, name)
        if not p:
            continue
        assets = [a for a in p.assets if a.include]
        kits: dict[str, list[PlanAsset]] = {}
        for a in assets:
            if a.kit:
                kits.setdefault(a.kit.strip().lower(), []).append(a)
        done = set()
        for a in assets:
            group = kits.get((a.kit or "").strip().lower(), [])
            if len(group) > 1:
                key = f"kit-{_slug(a.kit)}"
                if key not in done:
                    done.add(key)
                    out.append(Unit(name, key, "kit", group))
            elif a.category == "terrain":
                out.append(Unit(name, a.id, "material", [a]))
            else:
                out.append(Unit(name, a.id, "object", [a]))
    return out


def _size(cells: int, aspect: float, pixels: int = PIXELS) -> tuple[int, int]:
    """Canvas for `cells` views side by side, each `aspect` wide per unit of height,
    at ~`pixels`, in multiples of 64 (Flux latents)."""
    ratio = min(max(cells * aspect * 1.15, 0.5), 3.2)
    w = math.sqrt(pixels * ratio)
    h = pixels / w
    return int(round(w / 64)) * 64, max(512, int(round(h / 64)) * 64)


def request(unit: Unit, anchor: StyleAnchor | None) -> tuple[str, int, int, StyleAnchor | None]:
    """(prompt, width, height, anchor) for a unit."""
    a = unit.assets[0]
    if unit.kind == "material":
        style = StyleAnchor(images=[], style_text=anchor.style_text) if anchor else None
        return MATERIAL_PROMPT.format(name=a.name, description=a.description.rstrip(".")), 1024, 1024, style
    if unit.kind == "kit":
        pieces = "; ".join(f"{i}. {x.name}: {x.description.rstrip('.')}"
                           for i, x in enumerate(unit.assets, 1))
        aspect = sum(max(x.dimensions.width, x.dimensions.depth) / x.dimensions.height
                     for x in unit.assets) / len(unit.assets)
        w, h = _size(len(unit.assets), min(max(aspect, 0.5), 1.6))
        return KIT_PROMPT.format(n=len(unit.assets), kit=a.kit, pieces=pieces), w, h, anchor
    d = a.dimensions
    w, h = _size(3, min(max(max(d.width, d.depth) / d.height, 0.5), 1.6))
    return OBJECT_PROMPT.format(name=a.name, description=a.description.rstrip(".")), w, h, anchor


def _next_index(d: Path) -> int:
    nums = [int(m.group(1)) for p in d.glob("sheet_*.png") if (m := re.fullmatch(r"sheet_(\d+)", p.stem))]
    return max(nums, default=-1) + 1


def sheets(store: ProjectStore, unit: Unit) -> list[str]:
    d = unit.dir(store)
    return sorted(p.relative_to(store.root).as_posix() for p in d.glob("sheet_*.png"))


def generate(store: ProjectStore, unit: Unit, n: int = 2, seed: int | None = None) -> list[Path]:
    """Add n sheets for a unit (one batch, one seed)."""
    project = store.load()
    backend = backend_for("references", project)
    prompt, w, h, anchor = request(unit, project.anchor)
    d = unit.dir(store)
    d.mkdir(parents=True, exist_ok=True)
    t = time.time()
    req = GenRequest(prompt=prompt, width=w, height=h, n=n, seed=seed, anchor=anchor)
    meta = read_json(d / "meta.json", default=None) or {
        "plan": unit.plan, "unit": unit.key, "kind": unit.kind,
        "assets": [a.id for a in unit.assets], "sheets": []}
    out, i = [], _next_index(d)
    for r in backend.generate(req, d, prefix="tmp"):
        final = d / f"sheet_{i:03d}.png"
        r.path.rename(final)
        out.append(final)
        meta["sheets"].append({"file": final.name, "seed": r.seed, "batch_index": r.meta.get("batch_index"),
                               "prompt": r.meta.get("prompt", prompt), "size": [w, h],
                               "backend": backend.name,
                               "anchor": bool(anchor and anchor.images)})
        i += 1
    write_json(d / "meta.json", meta)
    store.log_run({"stage": "refs.generate", "plan": unit.plan, "unit": unit.key, "kind": unit.kind,
                   "n": len(out), "backend": backend.name, "seconds": round(time.time() - t, 1)})
    return out


def generate_missing(store: ProjectStore, n: int = 2, plan: str | None = None,
                     log=print) -> list[Path]:
    """Sheets for every unit that has none yet."""
    out = []
    for u in units(store, plan):
        if not sheets(store, u):
            log(f"{u.plan}/{u.key} ({u.kind})")
            out += generate(store, u, n=n)
    return out
