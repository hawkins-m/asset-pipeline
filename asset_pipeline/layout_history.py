"""Which slot actors in the UE level are stale, and which were moved by hand?

The importer sets a slot actor's transform only when it creates it (or with --reset-layout),
so after a relayout an untouched actor still sits where the greybox *of its first import* put
it. An actor is:
    current  where the current greybox (site/greybox.json) puts its slot;
    stale    where some committed version of site/greybox.blend put it: safe to reset;
    hand     anywhere else: moved in Unreal by hand, keep.
Only transforms are compared. A slot's instances are rewritten on every import, so they always
follow the current greybox (the import's sample check verifies them).

Versions come from the project repo's history of site/greybox.blend, each read with the
greybox extract (Blender, a few s per version) and cached in site/build/history/.
"""
import json
import subprocess
from pathlib import Path

import numpy as np

from . import blender, ue_coords
from .project import ProjectStore
from .stages import sw_site

TOLERANCE_M = 0.01      # 1 cm
ROT_TOLERANCE = 2e-4    # on the rotation-scale matrix


def same_place(a: np.ndarray, b: np.ndarray) -> bool:
    """Two Blender-space 4x4 slot matrices within 1 cm and ~0.01 deg (scale included)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    return (float(np.abs(a[:3, 3] - b[:3, 3]).max()) <= TOLERANCE_M
            and float(np.abs(a[:3, :3] - b[:3, :3]).max()) <= ROT_TOLERANCE)


def _git(store: ProjectStore, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(store.root), *args], capture_output=True)


def greybox_versions(store: ProjectStore, log=print) -> dict[str, dict[str, list]]:
    """{commit: {slot id: Blender matrix}} for every committed site/greybox.blend, newest first.
    {} if the project isn't a git repo."""
    r = _git(store, "log", "--format=%h", "--", "site/greybox.blend")
    if r.returncode:
        return {}
    cache = sw_site.site_dir(store) / "build" / "history"
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for h in r.stdout.decode().split():
        js = cache / f"{h}.json"
        if not js.is_file():
            b = cache / f"{h}.blend"
            b.write_bytes(_git(store, "show", f"{h}:site/greybox.blend").stdout)
            log(f"greybox {h}: extracting")
            blender.run(sw_site.SCRIPT, ["--extract", js], cache / f"{h}.log", f"greybox extract {h}", blend=b)
            b.unlink()
        out[h] = {s["id"]: s["matrix"] for s in json.loads(js.read_text())["slots"]}
    return out


def classify(ue_actors: dict[str, dict], current: dict[str, list], versions: dict[str, dict[str, list]]) -> dict:
    """ue_actors: {slot id: UE transform} of the slot actors in the level; current: {slot id:
    Blender matrix} of the current greybox. Returns {"current": [ids], "stale": {id: [commits
    whose placement it matches]}, "hand": [ids]}; slots not in the current greybox are skipped
    (they're orphans: --prune)."""
    res = {"current": [], "stale": {}, "hand": []}
    for sid, t in sorted(ue_actors.items()):
        if sid not in current:
            continue
        m = ue_coords.from_ue(t)
        if same_place(m, current[sid]):
            res["current"].append(sid)
            continue
        hits = [h for h, slots in versions.items() if sid in slots and same_place(m, slots[sid])]
        if hits:
            res["stale"][sid] = hits
        else:
            res["hand"].append(sid)
    return res


def ue_slot_transforms(store: ProjectStore) -> dict[str, dict]:
    """{slot id: UE transform} of the level's pipeline slot actors (headless read, no GPU;
    unlike `ap ue pull-layout` it doesn't write site/ue_layout.json)."""
    from . import ue
    from .stages import s7_export
    rep = ue.run_script(store, "ap_pull_layout.py", {"map": s7_export.load_manifest(store)["map"]}, "UE layout read")
    if "error" in rep:
        raise RuntimeError(f"UE layout read failed: {rep['error']}")
    return {a["tags"]["ap:id"]: a["transform"] for a in rep["actors"]
            if a["tags"].get("ap:source") == "pipeline" and "ap:id" in a["tags"]}


def check(store: ProjectStore, log=print) -> dict:
    """classify() for the project's level against its greybox history."""
    current = {s["id"]: s["matrix"] for s in sw_site.load_greybox(store)["slots"]}
    return classify(ue_slot_transforms(store), current, greybox_versions(store, log=log))
