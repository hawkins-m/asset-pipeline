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
- [ ] 2. Import at city scale: textures stripped, Nanite on, skip unchanged (content hash), time per asset
- [ ] 3. Terrain: SM_OuterRing and the Nanite terrain mesh in the city level; note if no Landscape
- [ ] 4. Lighting: sun / fog / sky from look.run into a shared function; project-default preset on the city level
- [ ] 5. `ap ue build PROJECT [--use-active] [--reset-layout | --only-stale] [--render]`
- [ ] 6. Tests (manifest, mixed-slot rule, skip-unchanged import); then ASK before the live run

## Log
- 2026-10-22 phase 1 done. `asset_pipeline/city_library.py` (plan, mixed-slot rule `choose`,
  `kept_pieces`, `actor_rows` with transforms relative to the slot), `look._candidates(shot=None)`
  for the whole city, `s7_export.export(assets=, use_active=)`, `manifest_components`. Library GLBs
  are hard-linked into export/library/ (phase 2 replaces them with texture-stripped copies).
  Scratch, `--use-active`: 555 builds in 38 slots (16 kit slots whole, 22 typed by footprint), 121
  held (townhouses-x-1y0 41/55, townhouses-x-2y-2 41/47, townhouses-x-3y0 38/39,
  insula-x-3y-2-marble 1/9); 20 s. Without --use-active nothing is placed (nothing Approved yet).
  Tests: tests/test_city_library.py.
  Next: phase 2 in unreal/ap_import.py: import `library_assets` (strip textures first, Nanite,
  skip by content hash, time per asset), materials per entry from `library_entries` (reuse
  ap_look_test.import_static / ap_material_preview.slot_instance), spawn one StaticMeshActor per
  `slots[].library` row attached to its slot actor (relative transform), remove pipeline library
  actors not in the manifest.
