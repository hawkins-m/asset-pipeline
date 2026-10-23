# `ap ue build` progress

One headless command that places the whole planned city in the Unreal city level: library
builds where they exist (Approved, or Active with `--use-active`), greybox stand-ins elsewhere.
This file tracks the work so a stopped session can resume from it. Update it after each step
and commit with the step.

Design facts (verified 2026-10-22, before phase 1):
- The city level gets only greybox stand-ins today (`ap export` -> `ap ue import`); library
  builds reach Unreal only in per-shot look levels (`ap ue look`, `look.placements`).
- Placements come in two kinds (`look._candidates`): typed buildings (per-building instance
  builds, `3d/library/<entry>/instances/index.json`, no slot id) and kit slots (the entry's
  build at the slot's pose, `site/library.json` slot_ids). Attached pieces:
  `assembly.child_placements`.
- A typed greybox slot can hold several buildings (an insula block slot: 8). Look scenes drop
  the greybox pieces standing on a placed footprint (+1 m); kit slots go whole.
- Mixed-slot rule (no partial slots): a kit slot is replaced whole when its build is placed; a
  typed slot is replaced only when every planned building in it has a placed build (then the
  pieces on those footprints go, the rest of the slot stays); otherwise the whole slot stays
  greybox. Attached pieces come with their placed parent.

## Phases
- [x] 1. City-wide asset manifest: `ap export --assets [--use-active]`
- [x] 2. Import at city scale: textures stripped, Nanite on, skip unchanged (content hash), time per asset
- [x] 3. Terrain: SM_OuterRing and the Nanite terrain mesh in the city level; note if no Landscape
- [x] 4. Lighting: sun / fog / sky from look.run into a shared function; project-default preset on the city level
- [x] 5. `ap ue build PROJECT [--use-active] [--reset-layout | --only-stale] [--render]`
- [x] 6. Tests (manifest, mixed-slot rule, skip-unchanged import) -- [x] live run: ASK Mason first, then
  `ap ue build <project> --use-active --only-stale` and report

## Log
- 2026-10-22 phase 1 done. `asset_pipeline/city_library.py` (plan, mixed-slot rule `choose`,
  `kept_pieces`, `actor_rows` with transforms relative to the slot), `look._candidates(shot=None)`
  for the whole city, `s7_export.export(assets=, use_active=)`, `manifest_components`. Library GLBs
  are hard-linked into export/library/ (phase 2 replaces them with texture-stripped copies).
  Scratch project, `--use-active`: 555 builds in 38 slots (16 kit slots whole, 22 typed by
  footprint), 121 held in 4 typed slots that are only partly built (e.g. 38 of 39 buildings);
  20 s. Without --use-active nothing is placed (nothing Approved yet).
  Tests: tests/test_city_library.py.
  Next: phase 2 in unreal/ap_import.py: import `library_assets` (strip textures first, Nanite,
  skip by content hash, time per asset), materials per entry from `library_entries` (reuse
  ap_look_test.import_static / ap_material_preview.slot_instance), spawn one StaticMeshActor per
  `slots[].library` row attached to its slot actor (relative transform), remove pipeline library
  actors not in the manifest.
- 2026-10-22 phases 2-5 written, under test on a COPY of the UE project (never the real city
  level until Mason says go): `AP_ROOT=/mnt/storage/ap-scratch AP_UE_PROJECTS=/mnt/storage/ap-scratch/unreal
  AP_UE_BACKUPS=/mnt/storage/ap-scratch/unreal-backups` (copy of Config, Content, .uproject).
  - Phase 2: `city_library.strip_textures` (deterministic, so re-imports skip by content hash;
    an insula build 4 MB -> 1 MB); `ap_import.import_library` imports changed library GLBs in
    batches of 24 (one-at-a-time took ~11 s each, Unreal waiting on every mesh build), Nanite on,
    entry material instances (ap_material_preview.slot_instance), `place_library`: AP_Lib_<key>
    actors attached to their slot actor, removed when the manifest drops them. Per-asset seconds
    in export/ue/import_seconds.json (a batch's share).
  - Phase 3: manifest "terrain" (city_library.terrain_section); `ap_import.place_terrain`: the
    outer ring in place of the terrain-outer slot's greybox, MI_AP_TerrainAuto on the terrain
    slots, report["landscape"]; `ue_build` runs `terrain_build.ue_assets` when outer.glb changed.
  - Phase 4: `look.sun_direction` (moved out of ue.look_scene) and `look.city_lighting` (DEFAULTS <
    site/look_tests/defaults.json < its "city" key; sun set against the first locked shot);
    `unreal/ap_lighting.py` shared by look scenes and the city import (city: ownership tags
    ap:lit:<name>, a setting changed in Unreal is kept).
  - Phase 5: `asset_pipeline/ue_build.py`, `ap ue build PROJECT [--use-active] [--reset-layout |
    --only-stale] [--render]`. One layout read-back gives the stale slots and the prune count.
    Renders: GPU 1 only, refused while ComfyUI is up.
  - Tests: tests/test_ue_build.py (prune count, render GPU rule, report, stripping).
- 2026-10-22 verified on the UE copy (scratch env above): first import 555 library builds + 9
  changed stand-ins in 14.8 min (batched, ~1.5 s per asset), all attached, Nanite on, no missing
  materials, outer ring placed, the level already has a Landscape. `ap ue build --use-active` re-run:
  0 imported, 908 unchanged, 55 s. Light: 10 settings written, AP_Env_post and AP_Env_clouds
  created. `--render` with ComfyUI up refuses with the reason. 593 tests pass.
  Waiting for Mason's go for the live run on the real project (it writes the city level).
- 2026-10-22 live run on the real project (`--use-active --only-stale`): 555 builds in 8.8 min
  (first import), then with the duplicate-spot fix 593 builds in 39 slots, 1.3 min. 3 typed slots stay
  greybox, partly built: their missing buildings' entries have briefs but no instance builds at
  those footprints (instance builds were made for other areas). Report now counts untouched slots.
