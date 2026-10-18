"""Guidance for the next build of a procedural asset (Library › Reference sheets › Brief & build,
and Asset review).

The artist writes what is wrong in plain words ("taller windows, the ground floor is an arcade").
The local vision model reads the sheet, the current brief and the brief form's fields and proposes
brief edits [{path, before, after, why}]. Only existing fields and valid values survive
(`library_review.validate`); the rest come back in `dropped` with a reason. The artist accepts
rows, `apply` saves them through `library_review.save_brief` and writes "guidance" into the brief.

Paths: features.<part>.<field> (nested, as the Asset review form shows them), dims (width, depth,
height m), params.storey_h / params.bay_m, material_types.<band>, material_scale.<band>, surface.
"""
import copy
import json
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

Mapping from the sheet to fields: window size and position -> facade window w, h, sill; ground storey
-> ground kind, door; pointed or segmental heads -> window kind; balconies -> balcony; attic -> top;
wall materials -> material_types per band; overall size -> dims, params.

Current brief fields (path = value [choices]):
{catalogue}
"""


class Edit(BaseModel):
    path: str
    after: str | float | bool | list[str | float]
    why: str = ""


class Edits(BaseModel):
    edits: list[Edit] = Field(default_factory=list, max_length=12)


# --- the field catalogue ---------------------------------------------------------------------

def _enum(path: str) -> list[str] | None:
    parts = path.split(".")
    key = parts[-1]
    parent = parts[-2] if len(parts) > 1 else ""
    return building_gen.ENUMS.get(f"{parent}.{key}") or building_gen.ENUMS.get(key)


def _leaves(node, path: str, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _leaves(v, f"{path}.{k}", out)
    elif isinstance(node, (bool, int, float, str)) or (isinstance(node, list) and all(
            isinstance(x, (int, float, str)) and not isinstance(x, bool) for x in node)):
        out[path] = node


def values(brief: dict) -> dict:
    """Every editable field of a brief as {path: current value}."""
    out: dict = {}
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
    _leaves(building_gen.form_features(form, brief.get("features")), "features", out)
    return out


def catalogue_text(vals: dict) -> str:
    from .material_lib import LIBRARY
    rows = []
    for p, v in vals.items():
        opts = ""
        if p.startswith("material_types."):
            opts = " [" + ", ".join(sorted(LIBRARY)) + "]"
        elif p == "surface":
            opts = " [auto, library, patch]"
        elif _enum(p):
            opts = " [" + ", ".join(_enum(p)) + "]"
        rows.append(f"{p} = {json.dumps(v)}{opts}")
    return "\n".join(rows)


# --- applying edits --------------------------------------------------------------------------

def _set(d: dict, path: str, value) -> None:
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def apply_edits(brief: dict, edits: list[dict]) -> dict:
    """The brief's editable keys with the edits applied (a new dict, as save_brief takes it)."""
    new = {"dims": list(brief.get("dims") or []), "params": copy.deepcopy(brief.get("params") or {}),
           "features": building_gen.form_features(brief["form"], copy.deepcopy(brief.get("features"))),
           "material_types": dict(brief.get("material_types") or {}),
           "material_scale": dict(brief.get("material_scale") or {}),
           "surface": brief.get("surface")}
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
    if isinstance(before, (int, float)):
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
    vals = values(brief)
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
        except ValueError as ex:
            dropped.append(row | {"reason": str(ex)})
            continue
        if after == before:
            dropped.append(row | {"reason": "already that value"})
            continue
        cand = brief | apply_edits(brief, [{"path": path, "after": after}])
        errs = [e for e in lr.validate(store, asset, cand)["errors"] if e not in base_errs]
        if errs:
            dropped.append(row | {"reason": "; ".join(errs)})
            continue
        seen.add(path)
        kept.append({"path": path, "before": before, "after": after, "why": why})
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
    r = structured(llm or llm_for(None),
                   PROMPT.format(asset=asset, form=brief.get("form"), guidance=guidance, max_edits=max_edits,
                                 catalogue=catalogue_text(values(brief))),
                   [_image(store, brief, sheet)], Edits, system=SYSTEM)
    kept, dropped = check(store, asset, brief, [e.model_dump() for e in r.edits])
    extra = [{"path": e["path"], "after": e["after"], "why": e["why"], "reason": f"more than {max_edits} edits"}
             for e in kept[max_edits:]]
    return {"edits": kept[:max_edits], "dropped": dropped + extra, "brief_made": made}


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
