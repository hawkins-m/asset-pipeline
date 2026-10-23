"""Guidance for the next build of a procedural asset (Library › Reference sheets › Brief & build,
and Asset review).

The artist writes what is wrong in plain words ("taller windows, the ground floor is an arcade").
The local vision model reads the sheet, the current brief and the brief form's fields and proposes
brief edits [{path, before, after, why}]. Only existing fields and valid values survive
(`library_review.validate`); the rest come back in `dropped` with a reason. The artist accepts
rows, `apply` saves them through `library_review.save_brief` and writes "guidance" into the brief.

Paths: features.<part>.<field> (nested, as the Asset review form shows them), dims (width, depth,
height m), params.storey_h / params.bay_m, material_types.<band>, material_scale.<band>, surface,
shading (auto | smooth | flat, building_proc). What the guidance asks for that no field can say
comes back in `unsupported` (short phrases), so a reroll can say "No setting for: ...".
Parts in SWITCHABLE that are off (None) are listed with their template's values: an edit to one of
their fields switches the part on from the template (an arcade on a colonnade range, its gallery).
"""
import copy
import json
import re
from pathlib import Path

from pydantic import BaseModel, Field

from . import building_gen, kit_proc
from . import library_review as lr
from .project import ProjectStore, read_json

BANDS = ("base", "wall", "top")
SYSTEM = "You adjust the parameters of a procedural building generator so it matches a reference drawing."
PROMPT = """The image is the reference sheet view of the building "{asset}" (form: {form}).
The artist's guidance for the next build:
\"\"\"{guidance}\"\"\"

Propose the smallest set of edits to the brief fields below that carries out the guidance.
Rules:
- Use only the paths listed, copied exactly. Never invent a field.
- "after" has the same kind as the field's current value: a number, true/false, a text from the
  listed choices, or a list. Shares (w, h, sill, ...) stay in (0, 1]; sizes are metres.
- Change only what the guidance asks for, and a field at most once. At most {max_edits} edits.
- "why" is one short sentence naming what you saw on the sheet or read in the guidance.
- If nothing can be expressed with these fields, return an empty list.
- List in "unsupported" each thing the guidance asks for that no field can express, as a short
  phrase (2-4 words, e.g. "rounded corners"); leave it empty when everything is covered.

Mapping from the sheet to fields: window size and position -> facade window w, h, sill; ground storey
-> ground kind, door; pointed or segmental heads -> window kind; balconies -> balcony; attic -> top;
wall materials -> material_types per band; overall size -> dims, params.
Arches on square piers (an arcade) -> arcade: bay_m (pier centre to centre, m), pier_w, arch_h (floor to
crown, m), arch (round | seg), h (the arcade storey, m), impost (the moulding at the springing);
a storey or open gallery over the arcade -> arcade.gallery: kind (loggia: open arches with a balustrade;
windowed: glazed windows), storey_h, storeys, w / h (shares of the bay and storey), arch,
balustrade, balustrade_h. On the arcade form the windows over the arcade are arcade.gallery's (w, h,
sill), not side.window (side is only the faces without an arcade); raised stone frames round the windows (pilasters on the pier lines, a band at
each floor) -> arcade.gallery.grid (w, proj); a set-back top storey -> attic (storey_h, setback;
side_setback 0 when the sides are party walls). A face with no openings (a party wall) -> its pattern
["solid"] and its ground kind solid. Smooth shading, soft curves, no faceted look -> shading smooth;
hard faceted surfaces -> shading flat. A part marked "off" is switched on by editing any of its fields.

Current brief fields (path = value [choices]):
{catalogue}
"""


class Edit(BaseModel):
    path: str
    after: str | float | bool | list[str | float]
    why: str = ""


class Edits(BaseModel):
    edits: list[Edit] = Field(default_factory=list, max_length=12)
    unsupported: list[str] = Field(default_factory=list, max_length=8)


# guidance about shading, also when the vision model misses it: "shade smooth", "less faceted"
SMOOTH_WORDS = re.compile(r"\bsmooth(?:er|ly)?\s+shad|\bshade[sd]?\s+smooth|facet", re.I)


# --- the field catalogue ---------------------------------------------------------------------

def _enum(path: str) -> list[str] | None:
    parts = path.split(".")
    key = parts[-1]
    parent = parts[-2] if len(parts) > 1 else ""
    return building_gen.ENUMS.get(f"{parent}.{key}") or building_gen.ENUMS.get(key)


# off parts (None) the guidance may switch on: their fields are offered with the template's values
SWITCHABLE = ("arcade", "gallery", "impost", "grid", "attic", "hip_roof")


def _leaves(node, path: str, out: dict, off: set | None = None) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            tmpl = building_gen.TEMPLATES.get(k)
            if v is None and off is not None and k in SWITCHABLE and isinstance(tmpl, dict):
                off.add(f"{path}.{k}")
                _leaves(tmpl, f"{path}.{k}", out, off)
            else:
                _leaves(v, f"{path}.{k}", out, off)
    elif isinstance(node, (bool, int, float, str)) or (isinstance(node, list) and all(
            isinstance(x, (int, float, str)) and not isinstance(x, bool) for x in node)):
        out[path] = node


def values(brief: dict, off: set | None = None) -> dict:
    """Every editable field of a brief as {path: current value}; off parts in SWITCHABLE with
    their template's values (their paths go into `off` when given)."""
    out: dict = {}
    off = set() if off is None else off
    form = brief.get("form")
    if brief.get("kind") != "building" or form not in building_gen.FORMS:
        return out
    out["dims"] = list(brief.get("dims") or [])
    for k in ("storey_h", "bay_m"):
        out[f"params.{k}"] = (brief.get("params") or {}).get(k)
    for b in BANDS:
        out[f"material_types.{b}"] = (brief.get("material_types") or {}).get(b)
        out[f"material_scale.{b}"] = (brief.get("material_scale") or {}).get(b, 1)
    out["surface"] = brief.get("surface") or "auto"
    out["shading"] = brief.get("shading") or "auto"
    _leaves(building_gen.form_features(form, brief.get("features")), "features", out, off)
    return out


def catalogue_text(vals: dict, off: set | frozenset = frozenset()) -> str:
    from .material_lib import LIBRARY
    rows, said = [], set()
    for p, v in vals.items():
        part = next((o for o in sorted(off, key=len) if p.startswith(o + ".")), None)
        if part and part not in said:
            said.add(part)
            rows.append(f"# {part} is off: editing any of its fields switches it on with these values")
        opts = ""
        if p.startswith("material_types."):
            opts = " [" + ", ".join(sorted(LIBRARY)) + "]"
        elif p == "surface":
            opts = " [auto, library, patch]"
        elif p == "shading":
            opts = " [auto, smooth, flat]"
        elif _enum(p):
            opts = " [" + ", ".join(_enum(p)) + "]"
        rows.append(f"{p} = {json.dumps(v)}{opts}")
    return "\n".join(rows)


# --- applying edits --------------------------------------------------------------------------

def _set(d: dict, path: str, value) -> None:
    """Set a nested value; a part that is off (None) is switched on from its template first."""
    keys = path.split(".")
    for k in keys[:-1]:
        if d.get(k) is None:
            tmpl = building_gen.TEMPLATES.get(k)
            d[k] = copy.deepcopy(tmpl) if isinstance(tmpl, dict) else {}
        d = d[k]
    d[keys[-1]] = value


def apply_edits(brief: dict, edits: list[dict]) -> dict:
    """The brief's editable keys with the edits applied (a new dict, as save_brief takes it)."""
    new = {"dims": list(brief.get("dims") or []), "params": copy.deepcopy(brief.get("params") or {}),
           "features": building_gen.form_features(brief["form"], copy.deepcopy(brief.get("features"))),
           "material_types": dict(brief.get("material_types") or {}),
           "material_scale": dict(brief.get("material_scale") or {}),
           "surface": brief.get("surface"), "shading": brief.get("shading")}
    for e in edits:
        _set(new, e["path"], e["after"])
    return new


def _number(after) -> float:
    if isinstance(after, (bool, list, dict)):
        raise ValueError("must be a number")
    try:
        return float(after)
    except (TypeError, ValueError):
        raise ValueError("must be a number") from None


def _coerce(before, after):
    """`after` in the kind of `before`; ValueError when it can't be."""
    if isinstance(before, bool):
        if isinstance(after, bool):
            return after
        if isinstance(after, str) and after.lower() in ("true", "false"):
            return after.lower() == "true"
        raise ValueError("must be true or false")
    if isinstance(before, int):                     # counts stay whole numbers (3.0 -> 3)
        x = _number(after)
        return int(x) if x.is_integer() else x
    if isinstance(before, float):
        return _number(after)
    if isinstance(before, list):
        if not isinstance(after, list):
            raise ValueError("must be a list")
        return after
    if isinstance(after, (list, dict)):
        raise ValueError("must be text")
    return str(after)


def check(store: ProjectStore, asset: str, brief: dict, raw: list[dict]) -> tuple[list[dict], list[dict]]:
    """(edits kept, dropped with reasons). raw rows have path, after, why; `before` is filled in."""
    off: set = set()
    vals = values(brief, off)
    base_errs = set(lr.validate(store, asset, brief)["errors"])
    kept, dropped, seen = [], [], set()
    for r in raw:
        path, why = r["path"], r.get("why", "")
        row = {"path": path, "after": r["after"], "why": why}
        if path not in vals:
            dropped.append(row | {"reason": "not a field of this brief"})
            continue
        if path in seen:
            dropped.append(row | {"reason": "the same field twice"})
            continue
        before = vals[path]
        try:
            if path.startswith("material_types."):
                after = str(r["after"]).lower()
            elif path.startswith(("params.", "material_scale.")) or before is None:
                after = _number(r["after"])
            elif path == "dims":
                after = [_number(x) for x in _coerce([], r["after"])]
                if len(after) != 3:
                    raise ValueError("needs width, depth and height")
            else:
                after = _coerce(before, r["after"])
            opts = _enum(path) if path.startswith("features.") and isinstance(after, str) else None
            if opts and after not in opts:
                raise ValueError(f"must be one of {', '.join(opts)}")
            if path == "surface" and after not in ("auto", "library", "patch"):
                raise ValueError("must be auto, library or patch")
            if path == "shading" and after not in ("auto", "smooth", "flat"):
                raise ValueError("must be auto, smooth or flat")
        except ValueError as ex:
            dropped.append(row | {"reason": str(ex)})
            continue
        part = next((o for o in sorted(off, key=len) if path.startswith(o + ".")), None)
        if after == before and not part:            # an off part's field switches the part on, even unchanged
            dropped.append(row | {"reason": "already that value"})
            continue
        cand = brief | apply_edits(brief, [{"path": path, "after": after}])
        errs = [e for e in lr.validate(store, asset, cand)["errors"] if e not in base_errs]
        if errs:
            dropped.append(row | {"reason": "; ".join(errs)})
            continue
        seen.add(path)
        kept.append({"path": path, "before": before, "after": after, "why": why} | ({"switch_on": part} if part else {}))
    return kept, dropped


# --- the proposal ----------------------------------------------------------------------------

def _image(store: ProjectStore, brief: dict, sheet: str) -> Path:
    v = brief.get("view")
    if v and (store.root / v).is_file():
        return store.root / v
    return store.root / sheet


def propose(store: ProjectStore, asset: str, sheet: str, guidance: str, view="front", llm=None,
            max_edits: int = 8) -> dict:
    """{edits, dropped, brief_made}. Makes the brief from the sheet first when there is none."""
    from .llm.base import structured
    from .llm.registry import llm_for
    guidance = (guidance or "").strip()
    if not guidance:
        raise ValueError("write what the next build should change")
    made = False
    sp = kit_proc.spec_path(store, asset)
    if not sp.is_file():
        from . import library_entries
        library_entries.brief_from_sheet(store, asset, sheet, view)
        made = True
    brief = read_json(sp)
    if brief.get("kind") != "building":
        raise ValueError(f"{asset} is a kit piece: edit its measurements in Asset review")
    off: set = set()
    vals = values(brief, off)
    r = structured(llm or llm_for(None),
                   PROMPT.format(asset=asset, form=brief.get("form"), guidance=guidance, max_edits=max_edits,
                                 catalogue=catalogue_text(vals, off)),
                   [_image(store, brief, sheet)], Edits, system=SYSTEM)
    raw = [e.model_dump() for e in r.edits]
    unsupported = [u.strip() for u in r.unsupported if u.strip()]
    if SMOOTH_WORDS.search(guidance):
        if not any(e["path"] == "shading" for e in raw):
            raw.append({"path": "shading", "after": "smooth", "why": "the guidance asks for smooth shading"})
        unsupported = [u for u in unsupported if not re.search(r"smooth|facet|shad", u, re.I)]
    kept, dropped = check(store, asset, brief, raw)
    extra = [{"path": e["path"], "after": e["after"], "why": e["why"], "reason": f"more than {max_edits} edits"}
             for e in kept[max_edits:]]
    unsupported += [d["path"] for d in dropped if d["reason"] == "not a field of this brief"]
    return {"edits": kept[:max_edits], "dropped": dropped + extra, "brief_made": made,
            "unsupported": list(dict.fromkeys(unsupported))}


def apply(store: ProjectStore, asset: str, edits: list[dict], guidance: str) -> dict:
    """Save the accepted edits and the guidance (save_brief: validation, Approved -> Needs review)."""
    sp = kit_proc.spec_path(store, asset)
    old = read_json(sp, default=None)
    if old is None:
        raise ValueError(f"no brief for {asset}")
    kept, dropped = check(store, asset, old, [{"path": e["path"], "after": e["after"], "why": e.get("why", "")}
                                              for e in edits])
    if dropped:
        raise ValueError("; ".join(f"{d['path']}: {d['reason']}" for d in dropped))
    new = apply_edits(old, kept) if kept else {}
    new["guidance"] = (guidance or "").strip()
    return lr.save_brief(store, asset, new)
