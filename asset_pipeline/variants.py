"""Variant sets: several library entries for one prop type, so a market's canopy tables are not
all the same table (Library › Asset review › Manage entry… › Variant set; `ap library variants`).

A set is keyed by its base entry (the prop type's own entry, e.g. canopy-table) and kept in the
library decisions file (sw_library.decisions_path) under "_variants":
    {"canopy-table": {"members": {"canopy-table": 1.0, "stall-b": 2.0, "stall-c": 1.0},
                      "areas": [], "seed": 0}}
members: entry -> weight (the base is always a member; its weight may be 0 to leave it out);
areas: the areas whose prop slots pick from the set (empty: every slot of the prop type).

A prop slot is one place the greybox holds that prop: a canopy table in a market, keyed by its
holding slot and its position there (`market-0@-51.4,3.6`). Each picks one member by weighted
rendezvous hashing of (seed, slot key, member): the same slot always gets the same entry, the
shares follow the weights, and adding a member (or raising its weight) only moves slots to that
member; the others keep theirs. Members of a set join the base's areas (areas.members), so the
area loop makes their sheets and 3D too.
"""
import hashlib
import math

import numpy as np

from .project import ProjectStore, write_json
from .stages import sw_library

KEY = "_variants"
# the piece that stands for each prop instance (one per prop, as sw_library._props_of counts)
MAIN_PIECES = {"market:hut": "market hut", "market:tent": "market tent", "market:canopy": "canopy table",
               "market:stall": "market stall"}


def _entries(store: ProjectStore) -> dict:
    data = sw_library.load(store) or {}
    return {e["id"]: e for e in data.get("entries", []) + data.get("unseen", [])}


def sets(store: ProjectStore) -> dict:
    return sw_library.decisions(store).get(KEY) or {}


def _save(store: ProjectStore, s: dict) -> None:
    d = sw_library.decisions(store)
    if s:
        d[KEY] = s
    else:
        d.pop(KEY, None)
    write_json(sw_library.decisions_path(store), d)


def set_of(store: ProjectStore, entry: str) -> str | None:
    """The base of the set an entry belongs to (as base or member), or None."""
    for base, spec in sets(store).items():
        if entry == base or entry in (spec.get("members") or {}):
            return base
    return None


def noun(store: ProjectStore, entry: str) -> str | None:
    """The prop type (greybox noun) an entry stands for: a plan prop's part, or None."""
    e = _entries(store).get(entry) or {}
    return e.get("part") if e.get("kind") == "prop" else None


def save(store: ProjectStore, base: str, members: dict, areas: list[str] | None = None, seed: int | None = None) -> dict:
    """Create or replace the set of `base`: members {entry: weight >= 0} (the base is kept as a
    member; leave an entry out to remove it), areas (empty: everywhere)."""
    ents = _entries(store)
    if base not in ents:
        raise KeyError(f"no library entry {base}")
    if ents[base].get("kind") != "prop" or ents[base].get("custom"):
        raise ValueError(f"{base}: a variant set is based on a prop type of the library plan (e.g. canopy-table)")
    from . import areas as areas_
    known = set(areas_.load(store))
    bad = [a for a in areas or [] if a not in known]
    if bad:
        raise ValueError(f"no area(s) {', '.join(bad)}")
    clean = {}
    for m, w in {base: (members or {}).get(base, 1.0), **(members or {})}.items():
        if m not in ents:
            raise KeyError(f"no library entry {m}")
        if ents[m].get("kind") != "prop":
            raise ValueError(f"{m} is a {ents[m].get('kind')}, not a prop")
        other = set_of(store, m)
        if other not in (None, base):
            raise ValueError(f"{m} is already in the variant set of {other}")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0:
            raise ValueError(f"weight of {m}: a number 0 or more")
        clean[m] = float(w)
    if not any(w > 0 for w in clean.values()):
        raise ValueError("at least one member needs a weight above 0")
    s = sets(store)
    old = s.get(base) or {}
    s[base] = {"members": clean, "areas": list(areas if areas is not None else old.get("areas") or []),
               "seed": int(seed if seed is not None else old.get("seed", 0))}
    _save(store, s)
    return summary(store, base)


def remove(store: ProjectStore, base: str) -> None:
    s = sets(store)
    if base not in s:
        raise KeyError(f"{base} has no variant set")
    s.pop(base)
    _save(store, s)


def prop_slots(store: ProjectStore, prop: str) -> list[dict]:
    """Every place the greybox holds one `prop` (a noun): [{key, slot, x, y, z, yaw}] in world metres."""
    from .stages import sw_site
    out = []
    for s in sw_site.load_greybox(store)["slots"]:
        M = np.asarray(s["matrix"], float)
        for p in s.get("pieces") or []:
            if MAIN_PIECES.get(p["piece"]) != prop:
                continue
            L = np.asarray(p["matrix_local"], float)
            W = M @ L
            out.append({"key": f"{s['id']}@{L[0, 3]:.1f},{L[1, 3]:.1f}", "slot": s["id"],
                        "x": round(float(W[0, 3]), 3), "y": round(float(W[1, 3]), 3), "z": round(float(W[2, 3]), 3),
                        "yaw": round(math.degrees(math.atan2(W[1, 0], W[0, 0])), 2)})
    return sorted(out, key=lambda r: r["key"])


def pick(seed: int, key: str, members: dict) -> str:
    """The member a prop slot gets: weighted rendezvous hashing (highest -w / ln u wins)."""
    best, score = None, -math.inf
    for m in sorted(members):
        w = members[m]
        if w <= 0:
            continue
        h = hashlib.sha256(f"{seed}|{key}|{m}".encode()).digest()
        u = (int.from_bytes(h[:8], "big") + 0.5) / 2 ** 64
        sc = -w / math.log(u)
        if sc > score:
            best, score = m, sc
    return best


def assign(store: ProjectStore, base: str, area: str | None = None) -> list[dict]:
    """The prop slots of the set's prop type, each with the entry it picks: [{key, slot, x, y, z,
    yaw, entry}]. Slots outside the set's areas keep the base entry; area: only that area's slots."""
    from . import areas as areas_
    spec = sets(store).get(base)
    if spec is None:
        raise KeyError(f"{base} has no variant set")
    prop = noun(store, base)
    if not prop:
        raise ValueError(f"{base} is not a prop type")
    scope = None
    if spec.get("areas"):
        scope = set().union(*(areas_.slots_in(store, a) for a in spec["areas"]))
    only = areas_.slots_in(store, area) if area else None
    out = []
    for r in prop_slots(store, prop):
        if only is not None and r["slot"] not in only:
            continue
        inside = scope is None or r["slot"] in scope
        out.append(r | {"entry": pick(spec.get("seed", 0), r["key"], spec["members"]) if inside else base})
    return out


def summary(store: ProjectStore, base: str) -> dict:
    """The set with its members' weights, their share of the weights and how many slots each gets."""
    spec = sets(store)[base]
    rows = assign(store, base)
    total = sum(spec["members"].values()) or 1.0
    counts = {m: 0 for m in spec["members"]}
    for r in rows:
        counts[r["entry"]] = counts.get(r["entry"], 0) + 1
    return {"base": base, "prop": noun(store, base), "areas": spec.get("areas") or [], "seed": spec.get("seed", 0),
            "slots": len(rows), "members": [{"entry": m, "weight": w, "share": round(w / total, 3), "slots": counts.get(m, 0)}
                                            for m, w in spec["members"].items()]}
