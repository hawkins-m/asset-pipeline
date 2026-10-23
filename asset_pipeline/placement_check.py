"""The placement check at the end of every `ap ue build`, for any project: each library actor as
Unreal has it, against the manifest, the ground and the greybox left around it.

    moved     the actor is away from its manifest place (place_m, place_deg)
    sunk      its base is below the ground under its outline (the project's ground rule) by more
              than sunk_m
    floating  ... above it by more than floating_m
    off_slot  its outline's centre is outside its slot's bounds by more than slot_margin_m
              (attached pieces are exempt: they may stand in front of their parent's slot)
    overlap   a greybox instance left in its slot stands inside its outline (shrunk by
              overlap_margin_m) below its top

Tolerances and the ground rule: project.json "unreal" (schema.UnrealSettings).
"""
import math
from collections import Counter, defaultdict

import numpy as np

from .city_library import footprint_of

KINDS = ("moved", "sunk", "floating", "off_slot", "overlap")


def ue_to_blender(loc, rot, scale) -> np.ndarray:
    """A UE actor transform (cm, yaw about Z) -> the Blender world 4x4 it was placed from (m)."""
    a = math.radians(-rot[2])                     # Y is mirrored: a turn flips its sign
    W = np.eye(4)
    W[:2, :2] = [[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]]
    W[:3, :3] *= float(scale[0])
    W[:3, 3] = [loc[0] / 100, -loc[1] / 100, loc[2] / 100]
    return W


def check(man: dict, actors: list[dict], slot_instances: dict, slot_bounds: dict, ground, cfg) -> dict:
    """{ok, checked, issues {kind: [key]}, per_entry {entry: {kind: n}}}. slot_bounds: {slot id:
    [[x0, y0, z0], [x1, y1, z1]]} (greybox.json); cfg: schema.UnrealSettings."""
    rows = {r["key"]: (s["id"], r) for s in man["slots"] for r in s.get("library") or []}
    bounds = {a["id"]: a["bounds"] for a in man.get("library_assets", [])}
    issues: dict[str, list] = defaultdict(list)
    per_entry: dict[str, Counter] = defaultdict(Counter)
    for a in actors:
        sid, row = rows.get(a["key"], (None, None))
        if row is None or row["asset"] not in bounds:
            continue
        lo, hi = bounds[row["asset"]]
        W = ue_to_blender(a["loc"], a["rot"], a["scale"])
        f = footprint_of(W, lo, hi)
        found = []
        w = row["world"]
        if (np.linalg.norm(np.subtract(a["loc"], w["loc"])) / 100 > cfg.place_m
                or abs((a["rot"][2] - w["rot"][2] + 180) % 360 - 180) > cfg.place_deg):
            found.append("moved")
        g = ground.under(f["x"], f["y"], f["w"], f["d"], f["yaw"], cfg.ground_rule)
        if f["base"] < g - cfg.sunk_m:
            found.append("sunk")
        elif f["base"] > g + cfg.floating_m:
            found.append("floating")
        (x0, y0, _), (x1, y1, _) = slot_bounds.get(sid, [[-math.inf] * 3, [math.inf] * 3])
        m = cfg.slot_margin_m
        if not row.get("parent") and not (x0 - m <= f["x"] <= x1 + m and y0 - m <= f["y"] <= y1 + m):
            found.append("off_slot")
        inv, k = np.linalg.inv(W), float(np.linalg.norm(W[:3, 0])) or 1.0
        e = cfg.overlap_margin_m / k
        for p in slot_instances.get(sid, []):
            q = inv @ np.array([p[0] / 100, -p[1] / 100, p[2] / 100, 1.0])
            if lo[0] + e < q[0] < hi[0] - e and lo[1] + e < q[1] < hi[1] - e and q[2] < hi[2]:
                found.append("overlap")
                break
        for kind in found:
            issues[kind].append(a["key"])
            per_entry[row["entry"]][kind] += 1
    return {"ok": not issues, "checked": len(actors), "issues": dict(issues),
            "per_entry": {e: dict(c) for e, c in sorted(per_entry.items())}}


def lines(res: dict) -> list[str]:
    """The check's part of the build report: "placement OK", or what is off, per entry."""
    if res["ok"]:
        return [f"placement OK ({res['checked']} library actors)"]
    out = ["placement: " + ", ".join(f"{len(res['issues'][k])} {k.replace('_', ' ')}" for k in KINDS if k in res["issues"])
           + f" of {res['checked']} library actors"]
    out += [f"  {e}: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in c.items()) for e, c in res["per_entry"].items()]
    return out
