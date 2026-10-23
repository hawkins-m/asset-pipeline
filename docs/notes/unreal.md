# Unreal

Unreal Engine 5.8.3 delivery: headless runs, axes, import, renders, look scenes. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## Unreal Engine 5.8.3 (world mode delivery) findings (2026-10-06)
- Editor: `/mnt/storage/UnrealEngine/5.8.3/Engine/Binaries/Linux/UnrealEditor(-Cmd)`
  (config `[unreal] editor_cmd`). Python 3.11.8 inside.
- UE projects live in `~/Projects/Unreal/<slug>/` (`[unreal] projects_dir`, the system
  NVMe). Backups go to `/mnt/storage/backups/unreal/<slug>/<timestamp>/`
  (`[unreal] backup_dir`, the other drive): `ap ue backup`, rsync with `--link-dest`,
  10 kept.
- **rsync needs `--checksum` for backups.** Its default size + mtime check skipped a file
  re-saved within the same second at the same size, and the new snapshot hard-linked the
  old content.
- Headless runs: `UnrealEditor-Cmd <uproject> -run=pythonscript -script="<file> <args.json>"
  -unattended -nullrhi`. No GPU is used, and an import takes about 1 min fresh and 10 s
  unchanged.
  - The commandlet exits 1 whenever anything logged an error, so judge by the script's
    JSON report.
  - `EditorAssetLibrary.load_asset` can't load engine content in the commandlet (the
    asset registry isn't scanned); `unreal.load_asset` can.
- **Axes, verified live:** Interchange imports a Blender glTF as Blender +X → UE +X,
  +Y → −Y, +Z → +Z, metres → cm. So every placement converts by C·M·C, C = diag(1,−1,1),
  with translation ×100 (`asset_pipeline/ue_coords.py`).
  - `unreal.Rotator(roll, pitch, yaw)` is positional.
  - The import self-check recomputes 155 instance world positions from the greybox
    independently: all within 1 cm.
- Interchange puts a mesh at `<dest>/<file>/StaticMeshes/<file>`, so the importer moves
  it to `/Game/AP/Library/<id>/SM_<id>`. A changed GLB replaces the mesh with
  `consolidate_assets`, which keeps references. In the commandlet the old asset stays at
  the target path after consolidating, so the importer deletes it before the rename, and
  the rename leaves redirectors in `_import` that are deleted directly (UE 5.8's Python
  AssetTools has no `fixup_referencers`). Verified 2026-10-06 on a full relayout.
- The commandlet deletes the importer's redirectors in memory only; their files stayed in
  `Content/AP/_import` and the next import of a changed GLB there failed ("already an asset of
  class ObjectRedirector"). `ue.import_` removes that folder first (editor closed).
- Stale slots (left where an earlier greybox put them) vs hand moves: `ap ue stale`, and
  `ap ue import --reset-layout --only-stale` resets only the stale ones (2026-10-16: 175 stale,
  0 hand-moved, all 489 back in place). It compares against every committed greybox.blend.
- A wholesale relayout needs `ap ue import --reset-layout --prune` (after `ap ue backup`):
  otherwise actors whose ids the new layout reuses keep their old UE transform (UE is the
  layout source of truth) and the old layout's actors stay.
- InstancedStaticMeshComponents added through `SubobjectDataSubsystem.add_new_subobject`
  persist across save and reload, with label, tags, outliner folder and instances.
- **No Python API creates a Landscape from a heightmap.** Only
  `landscape_import_heightmap_from_render_target` exists, into an existing landscape's
  components. Terrain is therefore a Nanite mesh; the manifest carries the values for a
  manual Landscape-mode import.
- **Renaming a project's level (2026-10-22, project slug rename).** A map rename through AssetTools
  (`rename_assets` / `rename_asset`) stops at a modal "config INI may need Find/Replace" dialog that
  `-unattended` answers Cancel, so nothing is renamed. What worked headless: `duplicate_asset` the map
  (every actor, tag, transform and ISM instance identical), drop `LS_<Name>` and let
  `ap_import.sequence()` create it fresh in the new level (a renamed sequence keeps the old map path
  in its name table), then delete the old map file with the editor closed (`delete_asset` in the
  commandlet only removes it in memory). The level has no LevelSequenceActor; only the sequence
  refers to the map. Imported meshes keep their old GLB source path in their import data; that only
  affects the editor's Reimport, since `ap ue import` matches meshes by the `ap:glb_sha` tag.
- Shot renders need the full editor: `UnrealEditor -RenderOffscreen -graphicsadapter=0`
  (Vulkan adapter 0 = GPU 0) with a Slate tick callback. Screenshots taken while
  ShaderCompileWorkers still run show grey default materials, so wait for them to go
  idle.

## Frame reconcile and look scenes (2026-10-11)
- `CitySpec.sites` carve fixed civic ensembles out of the fill *after* it is planned (own random
  stream): the full plan diff showed only the 8 buildings on the footprint gone and the site's 2
  added; trees, blocks, ways identical. A slot that loses buildings keeps its pieces' world
  positions but its pivot recentres (UE keeps actor transforms: re-import with --reset-layout).
- Plaza `basins`: two 12 x 30 m basins either side of a 10 m walk along the node's second radial,
  lawns with 1.8 m gravel edges in two opposite quarters. Greybox pieces without a colour of their
  own take the slot type's colour: a "pool" slot painted its paving blue until `paving` got one.
- `frame_plan` (OneFormer + SAM 3.1 classes read back onto the plan through the shot camera):
  inverse warp, not splatting (far rows leave streaks); at eye level (1.5 m) one pixel row spans
  ~1 m of ground at 40 m and ~5 m at 80 m, so compare within ~90 m. Depth Anything agreed with the
  plane to 1% median. The top-down greybox plan renders its class colours as linear: compare in sRGB.
- The library plan labelled a shot's first usable source frame "locked" when the real lock no
  longer fit the layout (fixed: `locked` is the shot's own lock).
- `library_instances`: an entry built at each typed building's own footprint and storeys (138
  insula instances, ~5 MB each with 1K maps; ~10 s per build).
- **UE offscreen renders of heavy look scenes (~120 Nanite library builds) crash GPU 0 with
  VK_ERROR_DEVICE_LOST** (twice, the desktop GPU); the same level renders fine on GPU 1
  (`-graphicsadapter=1`, `ue._render(adapter=1)`) while ComfyUI is down.
- Interchange imports every GLB's embedded textures even when the pipeline assigns its own
  material instances: ~120 instance GLBs made the import slow. Strip textures for UE imports.
- A 12 deg sun through SkyAtmosphere plus a 4300 K sun and warm fog turned the whole look sepia;
  16 deg at 5000 K with blue-grey haze was closer. Exposure set on the paving converges in one
  correction to ~10% (the tone curve isn't linear). Height fog at 0.025 hides a 3.5-6.5 km ridge.

## Look scenes from presets (2026-10-14)
- `ap ue look PROJECT NAME` (`asset_pipeline/look.py`).
- Exposure on a frame region converges slowly through the tone curve: 0.69 -> 0.55 -> 0.47
  against 0.43 (bias 0, -0.67, -1.02). Use three passes.
- A frame's class median gives a bad albedo:
  - Shadowed pixels carry the sky's blue, so the paving's median was blue-grey. Take the
    hue from the brightest 40% and scale it to an albedo.
  - Water's brightest pixels are reflections: use all of them.
  - The grass and gravel classes picked up warm soil, so fixed tints went into the preset.
- Height fog: 0.025 hid a 2.5-4 km ridge, 0.008 turned it into a pale wall, and 0.0025
  kept it visible and hazy (falloff 0.06).
- A crest-line backdrop needs a front slope steeper than the line of sight, or a nearer
  row becomes the silhouette. Rows at near-1800 (below ground), -1200 (0.3 z), -600
  (0.7 z) and near (z) keep the crest on top.
- World-aligned tiling textures need pixel-centre sampling when generated, or the joint at
  the wrap is half a pixel off.
