"""`ap ue build`: the whole planned city in the Unreal city level, headless, in one command.

    refuse if the editor has the project open -> backup -> export with library builds
    (city_library; Approved, or Active with use_active) -> terrain assets when the outer ring
    changed -> import (layout kept: only stale slots move back, unless reset_layout; prune with
    the expected count from the level's own read-back) with the project-default light
    (look.city_lighting) -> optional shot renders -> one short report, ending with the
    placement check (placement_check: sunk, floating, moved, off-slot or overlapping library
    actors, or "placement OK").

Renders go on the GPU ComfyUI doesn't use. Heavy Nanite scenes have lost the device on GPU 0
(docs/notes/unreal.md), so the city renders on GPU 1, and only while ComfyUI is down there;
otherwise the render step refuses with the reason (the rest of the build stands).
"""
import time

from . import layout_history, ue
from .project import ProjectStore
from .stages import s7_export, sw_site

def _vision_gpu() -> int:
    from . import config
    return int(config.backends().get("jobs", {}).get("gpu0_gpu", 0))


def render_gpu_index() -> int:
    """The city renders on ComfyUI's GPU ([jobs] comfy_gpu), only while ComfyUI is down."""
    from . import config
    return int(config.backends().get("jobs", {}).get("comfy_gpu", 1))


def _level_actors(store: ProjectStore, map_: str) -> tuple[dict[str, dict], set[str]]:
    """The level's pipeline slot actors {id: UE transform} and shot ids (a level not built yet:
    none)."""
    if not (ue.project_dir(store) / "Content" / map_.removeprefix("/Game/")).with_suffix(".umap").is_file():
        return {}, set()
    rep = ue.run_script(store, "ap_pull_layout.py", {"map": map_}, "UE layout read")
    if "error" in rep:
        raise RuntimeError(f"UE layout read failed: {rep['error']}")
    mine = [a for a in rep["actors"] if a["tags"].get("ap:source") == "pipeline"]
    return ({a["tags"]["ap:id"]: a["transform"] for a in mine if "ap:id" in a["tags"]},
            {a["tags"]["ap:shot"] for a in mine if "ap:shot" in a["tags"]})


def expected_prune(ue_slots, ue_shots, man: dict) -> int:
    """How many pipeline actors the import will find missing from the manifest: the count the
    prune check needs to delete them headless."""
    return len(set(ue_slots) - {s["id"] for s in man["slots"]}) + len(ue_shots - {s["id"] for s in man["shots"]})


def render_gpu(comfy_up: bool, vram: dict[int, float]) -> tuple[int | None, str]:
    """(gpu, why): ComfyUI's GPU when ComfyUI is down and its VRAM is free; None with the reason otherwise."""
    RENDER_GPU = render_gpu_index()
    if comfy_up:
        return None, (f"render skipped: ComfyUI is running on GPU {RENDER_GPU}, the only GPU safe for the city "
                      f"(heavy Nanite scenes lose the device on GPU {_vision_gpu()}). Run `ap ue build --render` again once ComfyUI is down.")
    used = vram.get(RENDER_GPU, 99.0)
    if used > ue.VRAM_LIMIT_GB:
        return None, f"render skipped: GPU {RENDER_GPU} has {used:.1f} GB of other work in use"
    return RENDER_GPU, f"rendering on GPU {RENDER_GPU} (VRAM in use: " + ", ".join(
        f"GPU {k} {v:.1f} GB" for k, v in sorted(vram.items())) + ")"


def build(store: ProjectStore, use_active: bool = False, reset_layout: bool = False, render: bool = False,
          log=print) -> dict:
    from . import look, terrain_build
    t0 = time.time()
    up = ue.uproject(store)
    if ue.editor_running(up):
        raise RuntimeError(f"the Unreal editor has {up.name} open: close it first (ap ue build writes the city level)")
    warnings: list[str] = []
    steps: dict[str, float] = {}

    def step(name, fn):
        t = time.time()
        log(f"{name}…")
        out = fn()
        steps[name] = round(time.time() - t, 1)
        return out

    if ue.project_dir(store).is_dir():
        step("backup", lambda: ue.backup(store))
    c = step("export", lambda: s7_export.export(store, assets=True, use_active=use_active, log=log))
    man = s7_export.load_manifest(store)
    glb = store.root / terrain_build.FAR_DIR / "outer.glb"
    done = store.root / terrain_build.FAR_DIR / "ue_assets.json"
    ring = ue.project_dir(store) / "Content/AP/Terrain/SM_OuterRing.uasset"
    if glb.is_file() and (not ring.is_file() or not done.is_file() or glb.stat().st_mtime > done.stat().st_mtime):
        step("terrain assets", lambda: terrain_build.ue_assets(store))
    elif not glb.is_file():
        warnings.append("no outer ring built (ap terrain outer): the terrain-outer greybox stays")
    ue_slots, ue_shots = step("level read", lambda: _level_actors(store, man["map"]))
    reset_only = None
    if not reset_layout and ue_slots:            # default: only stale slots move back; hand moves stay
        current = {s["id"]: s["matrix"] for s in sw_site.load_greybox(store)["slots"]}
        cls = layout_history.classify(ue_slots, current, layout_history.greybox_versions(store, log=lambda *a: None))
        reset_only = sorted(cls["stale"])
        if cls["hand"]:
            log(f"kept {len(cls['hand'])} slot(s) moved by hand in Unreal")
    expect = expected_prune(ue_slots, ue_shots, man)
    rep = step("import", lambda: ue.import_(store, reset_layout=reset_layout or reset_only is not None, prune=True,
                                            expect=expect, reset_only=reset_only, lighting=look.city_lighting(store)))
    warnings += rep.get("warnings", [])
    if rep.get("prune_refused") is not None:
        warnings.append(f"prune refused: {rep['prune_refused']} orphans, expected {expect}")
    if rep.get("lighting_kept"):
        warnings.append(f"light settings changed in Unreal and kept: {', '.join(rep['lighting_kept'])}")
    if not rep.get("landscape"):
        warnings.append("no Landscape in the level (optional, manual: USAGE.md 'Terrain beyond the city'); "
                        "the terrain is the Nanite mesh")
    if rep.get("sample_errors"):
        warnings += [f"placement check: {e}" for e in rep["sample_errors"][:5]]
    placement = None
    if rep.get("library_actors") is not None:     # the placement check, every build
        from . import city_library, placement_check
        from .ground import Ground
        gb = sw_site.load_greybox(store)
        placement = placement_check.check(s7_export.load_manifest(store), rep["library_actors"],
                                          rep.get("slot_instances") or {}, {s["id"]: s["bbox"] for s in gb["slots"]},
                                          Ground.from_project(store), city_library.settings(store))
    shots = None
    if render:
        gpu, why = render_gpu(look.comfy_running(), ue.gpu_vram())
        log(why)
        if gpu is None:
            warnings.append(why)
        else:
            shots = step("render", lambda: ue._render(store, man["map"], [{"id": s["id"], "resolution": s["resolution"]}
                                                                          for s in man["shots"]],
                                                       ue.work_dir(store) / "shots", "ap_shot_render", 5400, adapter=gpu))
    lib = man.get("library") or {}
    greybox_slots = sum(1 for s in man["slots"] if not s.get("replaced"))      # untouched: no build in them
    out = {"ok": bool(rep.get("ok")), "use_active": use_active, "placed": (rep.get("library") or {}).get("placed", 0),
           "props": lib.get("props", 0), "attached": lib.get("placed", 0) - lib.get("parents", 0) - lib.get("props", 0),
           "slots_replaced": lib.get("slots_replaced", 0), "no_model": lib.get("no_model", []),
           "greybox_slots": greybox_slots, "greybox_instances": c["instances"], "held": lib.get("held", 0),
           "held_entries": lib.get("held_entries", []), "imported": len(rep["assets"]["imported"]),
           "skipped": len(rep["assets"]["skipped"]), "reset": len(rep["actors"]["reset"]),
           "pruned": len(rep.get("pruned", [])), "terrain_outer": bool(rep.get("terrain_outer")),
           "shots": None if shots is None else len(shots.get("shots", [])), "warnings": warnings,
           "placement": placement,
           "steps": steps, "seconds": round(time.time() - t0, 1)}
    store.log_run({"stage": "ue.build", **{k: v for k, v in out.items() if k not in ("warnings", "held_entries", "placement")},
                   "placement_ok": None if placement is None else placement["ok"]})
    return out


def report_lines(r: dict) -> list[str]:
    """The short report `ap ue build` prints."""
    extra = [f"{r.get('props', 0)} props"] * bool(r.get("props")) + [f"{r['attached']} attached pieces"] * bool(r["attached"])
    lines = [f"{'OK' if r['ok'] else 'PROBLEMS'}: {r['placed']} library actors placed"
             + (f" ({r['placed'] - r.get('props', 0) - r['attached']} builds, {', '.join(extra)})" if extra else "")
             + f" in {r['slots_replaced']} slots; {r['greybox_slots']} slots still greybox"
             + (f", {r['held']} builds held back (slot not fully built or not approved)" if r["held"] else ""),
             f"assets: {r['imported']} imported, {r['skipped']} unchanged; {r['reset']} stale slots moved back; "
             f"{r['pruned']} orphans pruned; outer ring {'placed' if r['terrain_outer'] else 'not placed'}"
             + (f"; {r['shots']} shots rendered" if r["shots"] is not None else ""),
             f"time: {r['seconds'] / 60:.1f} min (" + ", ".join(f"{k} {v:.0f} s" for k, v in r["steps"].items()) + ")"]
    if r.get("placement") is not None:
        from .placement_check import lines as check_lines
        lines += check_lines(r["placement"])
    lines += [f"warning: {w}" for w in r["warnings"]]
    return lines
