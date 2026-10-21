# World and city

World and city mode: greybox and passes, concept frames and their control settings, city planning, typologies, walled city, locked shots. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## World mode: greybox and shot passes (verified 2026-10-05)
- Headless Blender 5.2 (CPU) builds an ~80-slot / ~4.4k-piece greybox in about 4 s.
  Pieces with the same primitive share one mesh, so kit pieces stay instanceable.
- The data passes are exact with Cycles on the CPU at 1 sample, with:
  - a box filter of width 0.01;
  - `dither_intensity = 0`;
  - the Raw view transform;
  - an emission-only material override.

  The ID pass then decodes with 0 unknown pixels, and the ID and depth hit masks agree
  pixel for pixel. Any of AA, dither or a view transform blends slot colours into
  colours nobody has.
- Depth is stored as 16-bit camera Z over 10 km (15 cm steps): the sea plane reaches
  about 5 km. Sky is 0.
- About 3.5 s per shot for all four passes at 1344x768.
- A rebuild always changes the .blend's hash, even from the same layout, so every shot's
  passes show as stale afterwards. Re-render them.
- Kit modularity: colonnades snap to whole modules, and a stoa's bay angle is module /
  radius. Without that, every building had its own entablature length (no reuse).

## Concept frames (world mode) findings (2026-10-06)
- ControlNet Union Pro 2.0 (`flux1-dev-controlnet-union-pro-2.0.safetensors`) and the
  BFL `flux1-depth-dev-lora.safetensors` both load in ComfyUI 0.24 with core nodes.
  Union needs the VAE input on `ControlNetApplyAdvanced`. About 35 s per 1344x768 frame
  at 28 steps on GPU 1.
- A/B (`scripts/diag/frames_ab.py`, 3 shots: a domed rotunda at eye level, a wide coastal
  aerial, a curved colonnade beside a garden; same seed and prompt). Edge match
  (chance-corrected, see `s0_frames.edge_match`) means:

  | Variant | Match | Look |
  |---|---|---|
  | Union depth 0.8, end ≥ 0.6 (+ canny 0.4) | 0.88–0.93 | Greybox shows through: flat grey ground, plain masses, desaturated; the sea became a concrete wall |
  | Union depth 0.6/0.6 + canny 0.3/0.5 | 0.79 | Layout exact, but surfaces flat and garden turned to paving |
  | **Union depth 0.6/0.6 alone** | 0.67 | **Best**: layout and scale hold; planting, sky and light come back. The default. |
  | Union depth 0.4 (any end) | 0.50–0.72 | Style good, layout and scale drift (a rotunda shrank behind an invented arch) |
  | Depth LoRA 0.65 / 0.85 | 0.17 / 0.37 | Redesigns or relocates buildings: unusable for one consistent place |

  Canny edges from the greybox are what flatten surfaces: they ask for the greybox's
  plain planes.
- Cross-shot references (`--ref`, an approved wide frame as extra Redux image at total
  +0.08), tried on two medium shots, same seeds:
  - they carry palette, light and planting across shots (the mediums took the wide
    frame's warm backlight and planted parterres: clearly "the same city");
  - they also carry the reference's sky: a hazy, sun-filled sky turned a large dome
    into a translucent glow, and a smaller invented dome took its place.

  Keep it opt-in. Try a lower strength (≈0.04) or a reference with a clear sky before
  making it a default.
- Material wording (A/B 2026-10-06, 2 medium shots x 3 seeds, wording only):
  "board-formed concrete" draws wood: slatted screens, timber fins, plank-streaked
  walls. "Honed pale stone/travertine" gives banded, streaky surfaces. Precise wording
  per material (`SiteLayout.materials`), e.g. "polished white Carrara marble, smooth
  seamless stone with fine soft grey veining, crisp sharp machined edges", gave veined
  marble and clean ashlar. The style text must drop the vague words too: it is appended
  to every prompt. Cost: the frames come out whiter and more sterile (less warm brick).
- A material's `ref` is Redux masked to its slots (id pass; `ConditioningSetMask` +
  `ConditioningCombine`). Each masked cond is one more model evaluation per step; the
  only run so far (one ref) took 91 s including the model load. Not yet A/B'd: no
  moodboard images.
- Wide aerials read a plateau's edge (a depth step around the city) as a circular ring
  wall, and a sparse layout makes wide shots look like a scale model.
- The edge score must be chance-corrected: raw recall gave random noise 0.96. Sobel
  edges at the top 12%, dilated 2 px, cover most of a busy image.
- Shell gotcha: `pgrep -f`/`pkill -f` with a script path also match the calling
  shell's own command line (a wait loop never ends; pkill kills itself). Match on
  something the caller doesn't contain, or use PIDs.

## City mode (verified 2026-10-06)
- `SiteLayout.city` (`asset_pipeline/city.py`): civic nodes, terrain-steered avenues,
  organic blocks, instanced massing. `ap site init --city` (neutral starter city) and
  `ap site plan PROJECT` (plan image + area report, no Blender, ~1 s).
- Measured at 5 x 3 km on 8 km terrain (6 nodes): ~7.6k buildings, ~10k trees, 468
  slots, 19.8k pieces. Greybox build 11 s; 17 shots' passes 155 s (~9 s each, CPU);
  export 208 s (251 GLBs); UE import 1-1.5 min fresh, 8 s unchanged.
- Housing is a scaled unit box (`scale` on the piece): one mesh for every building, so
  Blender, the export and UE instancing stay cheap. Draped pieces (ribbons, lawns) are
  unique, so the build merges them into one mesh per slot: 496 -> 186 GLBs on the
  starter city, export 314 s -> 115 s.
- Slot ids made file-safe must stay unique: tiles are `x<i>y<j>` (`-3_-1` and `3_-1`
  collided as asset ids).
- Density: `1 - prod(1 - exp(-(d / (falloff * weight))^2))` over the nodes, plus a
  waterfront bonus and noise, minus steep ground. With a 900 m falloff the whole city
  was dense; 650 m gives cores, terraces and a fraying low-rise edge. Classes: >= 0.6
  perimeter courtyard blocks, >= 0.4 terraces, below detached houses.
- Spiral avenues need a cap on their total turn (75 deg), and arterials must aim at the
  bearing to their target: otherwise both coil or loop near a node's pad.
- Auto shots: wides look down ~13-18 deg (lower ones showed the city as a sliver on a
  coastal plain); the aerial shows the radial plan; the tight steps back by the
  monument's longest side.

- First city frames (Union depth 0.6/0.6, 2 per shot, 2026-10-06): the wides read as a
  large coastal city. Eye-level node mediums follow the layout (edge match 0.45-0.78) and
  the precise material words hold (marble, no wood). Views from far above follow it
  poorly: raised node mediums 0.00-0.38 and the aerial 0.06-0.12. Flux keeps the
  "radial city" idea but invents its own plan and megastructures (oval stadia, curved
  slabs), because a near-top-down depth pass is almost flat. Per-view control settings: see "Concept frames: views from above".

## Concept frames: views from above (2026-10-09)
- A shot's view comes from its camera (`s0_frames.view_of`): aerial if it looks down more
  than 35 deg, raised if a medium or tight looks down more than 3 deg, else level. Wides
  at 13-18 deg down hold their layout (0.55-0.58) and stay level.
- Why raised views drift: the plain depth image is inverse depth over the whole view, so
  it's mostly the ground's near-to-far ramp, and 10-30 m of buildings are faint bumps on
  it (on the aerial the radial plan is almost invisible). `sw_shots.depth_relief`
  removes the ramp with a masked blur and gives half the range to the local relief. A
  plane's inverse depth is linear in the image, so the blur keeps the ground flat grey;
  the blur's border must be extended linearly (odd reflection), otherwise the frame's edge
  turns into a false relief band that eats the scale.
- A/B (`scripts/diag/frames_ab.py --set above|above2`, 3 raised mediums + the aerial,
  2 seeds, wording only), mean edge match on the raised shots:

  | Variant | Raised | Aerial | Look |
  |---|---|---|---|
  | default depth 0.6/0.6 | 0.16 | 0.08 | Invents its own plan (an avenue with lettering) |
  | plain depth 0.8/0.8 | 0.45 | 0.10 | Pine canopies became lawns; a plaza flooded |
  | depth 0.6/0.6 + canny 0.3/0.4 | 0.49 | 0.11 | Good layout on one shot, surfaces flatter |
  | relief 0.6/0.6 | 0.22 | 0.13 | Aerial keeps colour and a city look |
  | **relief 0.75/0.75** | **0.49** | 0.19 | **Raised default**: layout held, real buildings; aerial a white scale model |
  | relief 0.75/0.75 + canny 0.3 | 0.71 | 0.18 | The greybox shows through (white boxes, grey plaza) |
  | relief 0.75 to 0.5 | 0.35 | 0.15 | Drifts; aerial still white |

  Defaults (`schema.DEFAULT_VIEWS`): raised = relief 0.75/0.75, aerial = relief 0.6/0.6.
  The harbour raised shot still drifts at the default (0.10/0.31); the aerial is unsolved.
- Masked references cost one model evaluation per step each, whatever their size: 34 s a
  frame with none, ~76 s with one, ~116 s with two. They're skipped below 5% of the frame
  (`FrameSettings.min_ref_coverage`).

## Typology catalog (city mode) findings (2026-10-07)
- Shares must be measured by footprint, not by building count: a row of narrow
  townhouses counted as 56% of "buildings" while covering 22% of the ground.
- With only the district mixes, 2-3 types took 40-60% of a district. Two fixes brought it
  near the 22%/40% caps:
  - weights back off (x0.15) once a type nears its cap, by footprint;
  - the fill around a single civic building is a weighted pick, not the strongest type.

  Diversity per district then went from 0.7-1.5 to 1.5-1.9 (Shannon).
- A 5 x 3 km city with 33 typologies: ~9.4k buildings, plan 2.5 s, 785 slots, greybox
  build 30 s.
- A typology with one roof and one facade can only vary by storeys. At its minimum storey
  count it must step up, or identical runs of 6-9 appear.
- Prompts are not regional: a landmark's material words (a warm stone colour) turned the
  whole raised frame that colour, not just the landmark. Only the masked Redux reference
  stays local. Prefer the masked ref plus neutral district words for colour that must stay
  on one building.
- Eye-level from ~120 m, a lagoon in front of a landmark is a sliver in the depth pass;
  Flux then invents a rectangular pool. Raised shots show it.
- `pkill -f` / `pgrep -f` on a command line match the calling shell (see above): stop
  servers by PID (`ss -ltnp`).
- UE 5.8 Python: `MaterialExpressionSaturate`, `Constant3Vector` (`constant`) and
  `LinearInterpolate` ("A", "B", "Alpha") build in the commandlet with `-nullrhi`;
  `get_material_instance_*_parameter_value` reads the instance's current value, which is
  how a value tuned in the editor is detected (metadata tag holds the pipeline's last
  write).

- Height variation (2026-10-08): with storeys 4-10 and +-2 jitter, a perimeter block's
  height spread (std / mean) lands around 0.21-0.30, even though it visibly runs from 4
  to 10 storeys. A 0.3 minimum flags about a third of blocks; corner emphasis (+1-2
  storeys) barely moves it. The district height field doesn't help within a block: it's
  smooth over ~700 m. Treat ~0.2 as "varied" for mid-rise blocks.
- Core blocks around a civic node are wedges (35-70% of their convex hull): a single
  building centred on the bounding box falls outside. Centre it at the block's most
  interior point (`polylabel`) and check type sizes against the inscribed circle.
- Plot merging: STRtree neighbours include blocks across avenues (19-24 m); only join
  blocks within street_w + 1 of each other. A local street is one long cut line across
  several blocks: clip it to the merged outline rather than testing its midpoint.

- Frame review round 2 (2026-10-08):
  - Style drift (frames reading as suburban California, North African or Middle Eastern)
    came from the words: "warm ochre / pale rose render", terracotta pitched roofs as the
    default roof, "citrus", "canvas awnings", and lollipop street trees (read as palms).
    Flux has no negative prompt here, so name what you want instead: travertine, marble,
    flat roofs with crisp cornices, umbrella pines, cypresses, olives.
  - A hill node levelled as one 220 m pad (the default pad) reads as a flat ring city;
    level only the summit plaza and let the slope stay.
  - Contour strips on a hill come out a few degrees off the local slope (the cut follows
    the parent block's slope). Terraces must follow the strip's own axis, or a 40 m
    terrace won't fit a 15 m strip, and the block stays empty.
  - A street cut can cross a block in several pieces: give each piece its own id.
  - **Arched walls were solid until 2026-10-08.** A notched outline (arches cut from the
    bottom edge) made by `bm.faces.new` + triangulate came out with overlapping triangles
    over the openings: the front faces covered twice the wall's own area, and every arch
    (rotunda sides, gates, aqueducts, arcades) was a solid wall in all passes. Build such
    walls from convex pieces (piers plus a comb of trapezoids over each arch);
    `tests/test_world.py::test_arched_walls_have_open_arches_in_blender` checks the area.
  - Depth only shows detail near the camera: a 6 m arcade walk at 35 m is under 2% of the
    shot's inverse-depth range (invisible), and canny needs an 8% relative depth step. An
    arcade reads at 5-20 m: look along it, not across a court at it.
  - Anchor A/B (6 moodboard scene images, total 0.05, plus two masked references): about
    310 s per frame vs 117 s without the anchor (vs ~48 s with no references at all).
    The anchor added ornament and palms and lowered the edge match (0.15 vs 0.40, same
    seed): left off.
  - Palms came from the words, not the trees: "sunlit coast" with sandy stone gave palms in
    every market frame, even with no palm-like silhouettes. "clear central-Italian light"
    plus "no palm trees" removed them on the same seeds, with olives in the planters
    instead. Flux's T5 handles that short negation.
  - Check what a frame shows before using it as a reference: the user's match numbers and
    descriptions for two frames were swapped. Go by the description.

## Walled city and locked shots (2026-10-10)
- `CitySpec.enclosure`: blocks only inside a ring road behind a circular wall (archwall
  bays of one mesh, on land only, open at the gates), density floored inside (no fraying
  edge), a greenbelt outside in hole-free wedges (a lawn is drawn from its outline: an
  annulus would cover the city). `towns`: small settlements with lanes and a square;
  `terrain.bay_*` puts a bay between. A 2 km ring with 6 nodes: ~2.3k buildings, build 14 s.
- Steep hills inside a compact ring leave bare patches (slope cut); hill_height 170 m over
  500-3200 m inland filled them.
- The aerial of a walled city is a high oblique (`aerial_pitch`, 35-42 deg); its distance
  must fit the near and far edges in the frame's height, not just the width
  (`city.aerial_distance`). A wide looking down >= 28 deg is an "aerial" view.
- `ShotSpec.locked`: approved shot, never regenerated nor re-placed (`ap frames lock`).
- Concrete wording A/B (2 shots x 2 seeds, wording only): smooth fair-faced, "board-marked
  cast concrete, cool grey, fine horizontal formwork lines, no timber" and "porous
  concrete" gave near-identical frames per seed, none with wood (the round-1 wood came
  from "board-formed"). The cream cast comes from "pale"/"ivory"/"warm" words, not the
  material name.

## Raised shots: realism A/B and the forum centre (2026-10-10)
- Raised frames read as white CG scale models. A/B (`scripts/diag/frames_ab.py --set
  realism|realism2`, 4 raised mediums x 2 seeds, an approved eye-level frame as target):

  | Variant | Edge match | Look |
  |---|---|---|
  | relief 0.75 to 0.75 (old default) | 0.69 | Scale model |
  | **relief 0.75 to 0.5 / 0.6** | 0.48 / 0.54 | Only lever that helps: haze, hills, weathering; palms can creep back. Default now 0.55 |
  | own locked medium as ref at 0.10 / 0.15 | 0.68 / 0.66 | No visible change |
  | + "dense dark-green umbrella pine canopies, seen from above" | same | No change: crowns stay grey dishes |
  | img2img realism pass (`s0_frames.refine`, no control) 0.25-0.5 | -0.03 to -0.1 | Stains and sharper detail, harsher contrast; never softer or hazier, never less model-like |

  Smooth dome crowns seen from above read as dishes or mushrooms whatever the words
  (from the side they read as pines). Seen from above, the scale-model look comes from the
  greybox itself (big blank roofs, smooth crowns), not the sampler.
- Pine crowns are domes 0.32 x their width (were 0.24 x the height, ~0.2 x the width:
  discs in wides). Trunk 1.4 m.
- `CivicNode.centre = "forum"`: an enclosed rectangular court (0.83 R x 0.5 R), four
  12 m portico ranges (shops behind, two orders, attic), gate arches where each radial
  crosses a range (6 radials: both short ends, +-24 m on the long sides), a fountain; a
  market node fills it with a stall grid. Its own random stream, and the loose market's
  draws still taken, so the rest of the plan (other markets, every tree) stays put.
  - The eye-level camera must stand clear of the stall rows (a hut 5 m away fills the
    frame) and outside the gate piers: in the court just in front of the end colonnade,
    beside its gate, looking diagonally at the far side's gate.
  - The masked landmark ref makes each frame ~100 s.
- `ap frames lock SHOT --greybox`: camera and greybox locked, no frame (wides: Flux
  can't resolve a whole city at frame size; they render in UE from the real assets).
