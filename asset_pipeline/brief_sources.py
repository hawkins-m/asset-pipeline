"""Where each field of a building brief comes from: the sheet, the artist's edit, or a default.

    sheet    measured on the brief's view of its sheet (storey height, bay rhythm, storeys,
             palette and band colours, band patches, band materials read by the vision model)
    edit     set by the artist: the brief form, accepted guidance, Pick on view, the material
             type / scale / surface choices. Features count as edits where they differ from the
             form's preset (Brief & build's form choice), which counts as a default
    default  the generator's defaults for the form (building_gen.form_features), with the preset

Re-briefing from a sheet measures the sheet again and keeps the edits (`carried_edits`), except
the ones the artist drops; "from scratch" drops them all (`library_entries.brief_from_sheet`).
"""
from . import building_gen, kit_proc
from .project import ProjectStore, read_json

BANDS = ("top", "wall", "base")
# brief keys that only the artist sets (whole values per band / key)
EDIT_KEYS = ("params", "colors", "patches", "material_types", "material_scale")


def preset_of(brief: dict) -> dict:
    """The feature values the form choice wrote (not the artist's): the recorded preset, else the
    form choice's features for briefs from before the record."""
    if brief.get("preset") is not None:
        return brief.get("preset") or {}
    choice = building_gen.FORM_CHOICE.get(brief.get("form_choice") or "") or {}
    return (choice.get("features") or {}) if choice.get("form") == brief.get("form") else {}


def _drop_equal(mine: dict, ref: dict) -> dict:
    out = {}
    for k, v in (mine or {}).items():
        p = (ref or {}).get(k)
        if isinstance(v, dict) and isinstance(p, dict):
            sub = _drop_equal(v, p)
            if sub:
                out[k] = sub
        elif v != p:
            out[k] = v
    return out


def _leaves(d: dict, prefix: str):
    """(path, value) of a nested dict's leaves; lists and None are leaves."""
    for k, v in (d or {}).items():
        p = f"{prefix}.{k}"
        if isinstance(v, dict) and v:
            yield from _leaves(v, p)
        else:
            yield p, v


def feature_edits(brief: dict) -> dict:
    """The artist's feature edits: the brief's features where they differ from the preset."""
    return _drop_equal(brief.get("features") or {}, preset_of(brief))


def carried_edits(brief: dict) -> list[dict]:
    """Every edit a re-brief would keep, one row per field: {path, value, label}."""
    rows = [{"path": p, "value": v} for p, v in _leaves(feature_edits(brief), "features")]
    for key in EDIT_KEYS:
        for k, v in (brief.get(key) or {}).items():
            if key == "material_scale" and v == 1:
                continue
            rows.append({"path": f"{key}.{k}", "value": v})
    if brief.get("surface") not in (None, "", "auto"):
        rows.append({"path": "surface", "value": brief["surface"]})
    if (brief.get("guidance") or "").strip():
        rows.append({"path": "guidance", "value": brief["guidance"].strip()})
    for r in rows:
        r["label"] = label(r["path"])
    return rows


def label(path: str) -> str:
    head, _, rest = path.partition(".")
    words = {"params": "", "features": "", "colors": "colour", "patches": "patch", "material_types": "material",
             "material_scale": "texture scale", "surface": "surfaces", "guidance": "guidance"}[head] if head in (
        "params", "features", "colors", "patches", "material_types", "material_scale", "surface", "guidance") else head
    tail = rest.replace(".", " › ").replace("_", " ")
    if head in ("colors", "patches", "material_types", "material_scale"):
        return f"{tail} band {words}"
    return (tail or words).strip()


def without(brief: dict, drop: list[str]) -> dict:
    """The edits to keep after dropping some: {features (nested), params, colors, patches,
    material_types, material_scale, surface, guidance} with only the kept fields."""
    drop = set(drop or ())
    kept = {"features": {}}
    for r in carried_edits(brief):
        if r["path"] in drop:
            continue
        keys = r["path"].split(".")
        if keys[0] in ("surface", "guidance"):
            kept[keys[0]] = r["value"]
            continue
        o = kept.setdefault(keys[0], {})
        for k in keys[1:-1]:
            o = o.setdefault(k, {})
        o[keys[-1]] = r["value"]
    return kept


def sheet_fields(brief: dict) -> list[dict]:
    """What the sheet gives the build (measured on the view): {path, label, value, overridden_by}."""
    m = brief.get("measured") or {}
    params, colors = brief.get("params") or {}, brief.get("colors") or {}
    patches, mtypes = brief.get("patches") or {}, brief.get("material_types") or {}
    rows = [
        {"path": "measured.storey_h", "label": "storey height", "value": m.get("storey_h"),
         "overridden_by": "params.storey_h" if "storey_h" in params else None},
        {"path": "measured.bay_m", "label": "bay rhythm", "value": m.get("bay_m"),
         "overridden_by": "params.bay_m" if "bay_m" in params else None},
        {"path": "measured.storeys", "label": "storeys on the sheet", "value": m.get("storeys"), "overridden_by": None},
        {"path": "measured.palette", "label": "palette", "value": len(m.get("palette") or []) or None, "overridden_by": None},
    ]
    for b in BANDS:
        rows.append({"path": f"measured.colour.{b}", "label": f"{b} band colour", "value": "from the band",
                     "overridden_by": f"colors.{b}" if b in colors else None})
        rows.append({"path": f"measured.patches.{b}", "label": f"{b} band patch",
                     "value": (m.get("patches") or {}).get(b) or m.get("patch_px"),
                     "overridden_by": f"patches.{b}" if b in patches else None})
        rows.append({"path": f"measured.materials.{b}", "label": f"{b} band material",
                     "value": ((m.get("materials") or {}).get(b) or {}).get("type") or "read at build",
                     "overridden_by": f"material_types.{b}" if b in mtypes else None})
    return rows


def sources(store: ProjectStore, asset: str) -> dict:
    """{fields: {path: sheet|edit|default}, edits: carried_edits, sheet: sheet_fields, form, size}
    for the brief form (Asset review) and Brief & build's re-brief step."""
    brief = read_json(kit_proc.spec_path(store, asset), default=None)
    if brief is None:
        raise FileNotFoundError(f"no brief for {asset}")
    out = {"edits": carried_edits(brief), "sheet": sheet_fields(brief) if brief.get("kind") == "building" else [],
           "fields": {}, "sheet_name": (brief.get("sheet") or "").split("/")[-1] or None}
    if brief.get("kind") != "building":
        out["fields"] = {f"params.{k}": "edit" for k in (brief.get("params") or {})}
        return out
    fields = out["fields"]
    full = building_gen.form_features(brief["form"], brief.get("features"))
    edits = feature_edits(brief)
    for p, _ in _leaves(full, "features"):
        fields[p] = "default"
    for p, _ in _leaves(edits, "features"):
        fields[p] = "edit"
    for p, _ in _leaves(edits, "features"):         # a part with an edit inside counts as edited
        keys = p.split(".")
        for i in range(2, len(keys)):
            fields[".".join(keys[:i])] = "edit"
    for r in out["sheet"]:
        key = r["path"].replace("measured.", "")
        fields[{"storey_h": "params.storey_h", "bay_m": "params.bay_m"}.get(key, r["path"])] = \
            "edit" if r["overridden_by"] else "sheet"
    for b in BANDS:
        fields[f"colors.{b}"] = "edit" if b in (brief.get("colors") or {}) else "sheet"
        fields[f"patches.{b}"] = "edit" if b in (brief.get("patches") or {}) else "sheet"
        fields[f"material_types.{b}"] = "edit" if b in (brief.get("material_types") or {}) else "sheet"
        fields[f"material_scale.{b}"] = "edit" if (brief.get("material_scale") or {}).get(b, 1) != 1 else "default"
    fields["surface"] = "edit" if brief.get("surface") not in (None, "", "auto") else "default"
    from . import library_entries                   # form and size: the entry's (yours) or the plan's
    try:
        info = library_entries.form_info(store, asset)
    except Exception:  # noqa: BLE001  (an entry gone from the plan: the brief's own values)
        info = {"source": None, "size_source": None, "size_m": None}
    fields["form"] = "edit" if info.get("source") == "yours" else "default"
    same = info.get("size_m") and [round(x, 3) for x in info["size_m"]] == [round(float(x), 3) for x in brief.get("dims") or []]
    for i in range(3):
        fields[f"dims.{i}"] = "edit" if info.get("size_source") == "yours" or not same else "default"
    return out
