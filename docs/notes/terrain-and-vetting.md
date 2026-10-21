# Terrain and vetting

The vetting gate, silhouette check, terrain from frames, terrain build and seams. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## Vetting gate, silhouettes, terrain plan (2026-10-14)
- **Silhouette check** (`asset_pipeline/silhouette.py`):
  - The frame's building mask is SAM 3.1 "building" in a box; its trees are left out of the
    IoU (the frame mask has holes there).
  - At eye level the ground line is useless for depth: a pixel there is about 5 m of ground.
    `measure` reads lengths on the slot's front plane instead. It was within 2% on a known
    build.
- **Look-scene GPU hang:** a gated look scene hit `VK_ERROR_DEVICE_LOST` on GPU 1 twice while the
  user's Blender session was using that GPU (see the VRAM rule above); the held greybox is
  exported as a mesh of its own (`held.glb`) since.
- **Terrain from frames** (`asset_pipeline/terrain_plan.py`, analysis only):
  - A painted ridge fixes elevation angles, not distance.
  - ADE20K names come as "mountain, mount": split on the comma.
  - Skyline trees and roofs are mostly the city's own; only mountain / hill labels count as
    far land.
  - Frames disagree: a range that fills one shot's mountains can land in another frame's
    painted sky. `carve` keeps the proposal under every frame-locked shot's lowest sky
    pixel per column; sky through arches counts.
- **Terrain build** (`terrain_build.py`, 2026-10-14):
  - **The city plan reshuffles on any terrain change.** Even 1 cm of PNG rounding moved
    2,307 of 2,317 buildings. `sw_site.plan_terrain` keeps planning on the tile without the
    far build. Check with an in-memory plan diff after any terrain edit.
  - **Sky fit:** count only cells that land inside a frame's first sky run from the top (not
    sky reflected in water, not land above the frame's top edge), with a 1.5 px tolerance.
    Capping every cell at the lowest band squashed the ranges to a tenth of their height.
    Fitting the crest height alone lost the ranges to a few flank and foothill cells:
    narrowing (mean slope ≤ 35°) and foothill factors kept most of the height.
  - Locked frames disagree. Smooth crests (≥ 1 km falloff) filled 35 / 80 / 16% of three
    frames' painted mountains. The flat-topped grid clip filled 79 / 100 / 51%: the
    difference is the notches.
- **Terrain seams (2026-10-16).** The dark horizon line in wide-hills was three things:
  - A 160 m gap between the far field (24 km, 40 m cells) and the outer ring (320 m cells
    didn't line up with the far border). The outer ring now uses 400 m cells on the border
    line (`terrain_build.outer_ring` refuses a cell that doesn't divide the gap).
  - The core tile and the far field disagreed by up to 40 m at the tile border: the core adds the
    relief to its seabed, the far field to max(ground, sea). `match_edge` blends the core into the
    far field over 400 m inside the border, only where the far field is land (no slot is that close).
    Doing it with the relief everywhere instead made 9 km² of the bay dry land.
  - Different vertex spacings leave cracks along a shared border: `scripts/heightgrid.py` hangs a
    skirt under mesh edges, with its own vertices (shared ones tilt the smooth normals into a dark
    band). The far ring now starts exactly on the core border.
  - The outer ring sinks 30 m under the sea from 22 to 30 km out; the preview render mixes into the
    sky colour with distance (`blender_shots.HAZE_M`, data passes untouched). Cycles drew the sea
    plane black at 35-50 km.
  - The far field's water is not at sea level: its relief noise lifts it 0-2 m (and more near the
    ranges), a skin over the sea plane. Left as built (a re-fit would be needed).
- **District ties were random.** `max(set(ds), key=ds.count)` picks a tie's winner by string hash
  order, which differs per process: one slot flipped between two districts across rebuilds of the
  same plan. `city.majority_district` breaks ties by the district at the slot's centre.
