"""City-mode typology catalog: YAML <-> SiteLayout (city.typologies, districts, materials).

The YAML is the review format (comments allowed); once imported, site/layout.json holds the
catalog and the UI edits it there. Shape of the YAML (all sections optional):

    typologies: [{id, family, form, size_m: {width: [a, b], depth: [a, b]}, storeys: [a, b],
                  place: [...], prompt, desc, roofs, facades, ground, columns,
                  cap: {max_share | max_count}, material, ref, ref_strength}, ...]
    variation:  {height: {storeys_jitter, avenue_bonus, step_back_top}}
    checks:     {max_typology_share_city, ...}            (RepetitionChecks)
    materials:  [{id, words, types?, districts?, ref?, ref_strength?}, ...]
    districts:  {<id>: {identity, palette, columns, height_bias, mix: {core|middle|edge: {...}}}}
    overlays:   {<zone>: {adds: {...}, replaces_housing_with}}

Importing merges by id: a material that already exists keeps its slot types, districts and
reference unless the YAML sets them; a district keeps its name and ring/sector.
"""
from pathlib import Path

import yaml

from .schema import District, Material, Overlay, RepetitionChecks, SiteLayout, Typology, Variation


def _typology(d: dict) -> Typology:
    d = dict(d)
    size = d.pop("size_m", None) or {}
    cap = d.pop("cap", None) or {}
    if "width" in size:
        d["width"] = tuple(size["width"])
    if "depth" in size:
        d["depth"] = tuple(size["depth"])
    d.update({k: cap[k] for k in ("max_share", "max_count") if k in cap})
    return Typology.model_validate(d)


def load(path: Path) -> dict:
    data = yaml.safe_load(Path(path).read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    return data


def apply(layout: SiteLayout, data: dict) -> SiteLayout:
    """A copy of the layout with the catalog sections of `data` merged in."""
    if layout.city is None:
        raise ValueError("the layout has no city spec: the catalog is for city mode")
    L = layout.model_copy(deep=True)
    c = L.city
    if "typologies" in data:
        ts = [_typology(t) for t in data["typologies"]]
        ids = [t.id for t in ts]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate typology ids: {', '.join(sorted(dup))}")
        c.typologies = ts
    if "variation" in data:
        v = data["variation"] or {}
        c.variation = Variation.model_validate({k: x for k, x in (v.get("height") or {}).items()
                                                if k in Variation.model_fields})
    if "checks" in data:
        c.checks = RepetitionChecks.model_validate(data["checks"] or {})
    if "overlays" in data:
        c.overlays = {k: Overlay.model_validate(v or {}) for k, v in data["overlays"].items()}
    if "materials" in data:
        old = {m.id: m for m in L.materials}
        out = []
        for m in data["materials"]:
            base = old.pop(m["id"], None)
            merged = (base.model_dump() if base else {}) | {k: v for k, v in m.items() if v is not None}
            out.append(Material.model_validate(merged))
        L.materials = out + list(old.values())     # materials the YAML doesn't name are kept
    if "districts" in data:
        old = {d.id: d for d in L.districts}
        for did, d in (data["districts"] or {}).items():
            d = dict(d or {})
            if "identity" in d:
                d["notes"] = d.pop("identity")
            base = old.get(did)
            merged = (base.model_dump() if base else {"id": did, "name": did}) | d
            old[did] = District.model_validate(merged)
        L.districts = list(old.values())
    check(L)
    return L


def check(L: SiteLayout) -> None:
    """Every name the catalog refers to must exist."""
    c = L.city
    ids = {t.id for t in c.typologies}
    mats = {m.id for m in L.materials}
    bad = []
    for d in L.districts:
        for band, mix in d.mix.items():
            bad += [f"district {d.id} {band}: unknown typology {t}" for t in mix if t not in ids]
        bad += [f"district {d.id}: unknown material {m}" for m in d.palette if m not in mats]
    for z, o in c.overlays.items():
        bad += [f"overlay {z}: unknown typology {t}" for t in o.adds if t not in ids]
        if o.replaces_housing_with and o.replaces_housing_with not in ids:
            bad.append(f"overlay {z}: unknown typology {o.replaces_housing_with}")
    bad += [f"typology {t.id}: unknown material {t.material}" for t in c.typologies
            if t.material and t.material not in mats]
    if bad:
        raise ValueError("catalog: " + "; ".join(bad))


def dump(layout: SiteLayout) -> str:
    """The layout's catalog as YAML (the import format)."""
    c = layout.city
    ts = []
    for t in c.typologies:
        d = t.model_dump(exclude_defaults=True, exclude={"user_fields"})
        size = {k: list(d.pop(k)) for k in ("width", "depth") if k in d}
        if size:
            d["size_m"] = size
        cap = {k: d.pop(k) for k in ("max_share", "max_count") if k in d}
        if cap:
            d["cap"] = cap
        if "storeys" in d:
            d["storeys"] = list(d["storeys"])
        ts.append(d)
    districts = {}
    for d in layout.districts:
        x = d.model_dump(exclude_defaults=True, exclude={"id", "user_fields", "radius", "sector", "name"})
        if "notes" in x:
            x["identity"] = x.pop("notes")
        districts[d.id] = x
    data = {"typologies": ts,
            "variation": {"height": c.variation.model_dump()},
            "checks": c.checks.model_dump(),
            "materials": [m.model_dump(exclude_defaults=True, exclude={"user_fields"}) for m in layout.materials],
            "districts": districts,
            "overlays": {k: o.model_dump(exclude_defaults=True) for k, o in c.overlays.items()}}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=110)


def mark_user_edits(old: list, new: list, fields: set[str] | None = None) -> list:
    """For lists of id'd models saved from the UI: add every field that changed against the
    stored version to that row's `user_fields` (rows new in `new` are entirely the user's)."""
    prev = {o.id: o for o in old}
    for n in new:
        o = prev.get(n.id)
        keep = set(o.user_fields) if o else set()
        names = fields or (set(type(n).model_fields) - {"id", "user_fields"})
        if o is None:   # a row the user added: all of it is theirs
            changed = {"id"} | {f for f in names
                                if getattr(n, f) != type(n).model_fields[f].get_default(call_default_factory=True)}
        else:
            changed = {f for f in names if getattr(n, f) != getattr(o, f)}
        n.user_fields = sorted(keep | changed)
    return new
