# Stages 0 6

Findings for the scene-mode stages: style anchor, reference sheets, views, hero orbit, Hunyuan3D, cleanup. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## Style anchor (stage 0) findings
- Flux Redux conditions on content as well as style. This was measured with a 3-image
  ukiyo-e anchor and the prompt "wooden barrel on white", seed 7 (`attn_bias`, total
  strength split across images):

  | Total strength | Result |
  |---|---|
  | ≤ 0.06 | Barely stylised |
  | ~0.08 | Subject kept, mildly stylised (flatter, outlined), palette not transferred |
  | ≥ 0.12 | Subject replaced by the anchor's scene |

  `multiply` mode at 0.15 also replaced the subject. Treat Redux as weak style plus
  content leakage, not a clean style transfer.
- **Anchors of single objects on white leak far less** (`scripts/diag/style_anchor_ab.py`).
  With 3 lantern-on-white anchors plus `style_text`, the barrel was kept at total 0.08
  and at 0.15, and took on the anchor set's look (flat shading, bold outlines, warm
  palette, cream ground). So stage 0 should generate isolated-object concepts by default.
  Scene anchors need ≤ 0.04 when combined with `style_text`.
- **The safe strength depends on the anchor set.** A 4-image set (barrel, planter, two
  market stalls) at 0.12 turned "wooden hand cart" into a stall-cart with an awning. At
  0.08 produce remained; at 0.05 the cart was clean and in the anchor style. Default is
  0.06. Distinctive multi-part objects (stalls) carry more content than simple ones.
- Side effect: object anchors plus text pull the anchors' cream background into
  "plain white background" prompts. Stage 2 must prompt hard for pure white, and stage 3
  cuts out regardless.

## Reference sheets (stage 2) findings
- Flux.1-dev (+ Redux anchor at the project's strength) reliably draws three copies of
  one object side by side on white, consistent in shape, colour and trim, but ignores
  "front / side / back": mostly three near-identical three-quarter views (tested
  2026-10-05 on barrel, stall, house; spelling out the rotations didn't help). The
  anchor didn't break the three-up layout.
- The explicit "LEFT/MIDDLE/RIGHT ... no ground, no shadow" wording gave cleaner white
  than the "character turnaround sheet" wording (no sand/dirt patches).
- ~55 s per 1.2 MP sheet on GPU 1. No local multi-view model is installed in ComfyUI
  (the Kontext/Qwen-Image edit nodes there are paid API nodes).

## Views and review (stages 3-4) findings
- SAM 3.1 finds the three views on a sheet reliably (31/32 test sheets, 2026-10-05)
  but its masks drop white or thin parts (snow on a shrub, the flowers in a planter). Views
  are therefore cut as full-height bands between neighbouring views, trimmed and kept on
  white; TRELLIS removes the background itself (BiRefNet).
- Pillow 12: an image made with `Image.fromarray` shares the array read-only and
  `ImageDraw.floodfill` silently does nothing on it. `.copy()` first.
- UI jobs: lanes "comfy" (GPU 1) and "gpu0"; Stop = `JobQueue.cancel` -> per-job
  cancellers (ComfyUI: `POST /queue {"delete"}` if pending, `POST /interrupt {"prompt_id"}`
  if running; TRELLIS: killpg + remove `logs/trellis-gpu<N>-<pid>.lock`). Verified live:
  another client's prompt was untouched; GPU 0 back to baseline 2 s after stopping TRELLIS.
- A scratch AP_ROOT for UI tests needs symlinks to `envs`, `models`, `tools`, `hf`, `src`,
  since `scripts/trellis` finds its env through AP_ROOT.
- Export and UE backup dialogs (2026-10-09) count progress from logs: export from the
  `EXPORT_TOTAL n` line in `export/blender.log` plus GLBs written, backup from rsync's
  `--info=progress2 --no-inc-recursive` (`to-chk=left/total`) in `export/ue/backup.log`.
  Until then `blender.run_logged` never flushed per line (`log.write(line) or
  log.flush()`: write returns a count), so logs reached disk in 8 KB chunks.
- glTF export walks the whole scene even with `use_selection`: 3.5 s per mesh on an
  80k-object city greybox. Exporting each mesh from an empty scene of its own
  (`temp_override(scene=...)`, `use_active_scene=True`) takes 0.09 s, byte-identical.

## Hero orbit (Wan 2.2) prototype findings (2026-10-05)
- `workflows/wan22_i2v_orbit.json`: Wan 2.2 I2V 14B fp8 high/low + lightx2v 4-step LoRAs,
  640x640, 81 frames: ~150-180 s per orbit on GPU 1 (ComfyUI swaps Flux out; the next
  Flux job reloads it).
- Orbits are visually consistent (same object, centred, same size, white kept). The
  default "slow turntable" prompt turned a stall only ~180 deg in 81 frames; "rotates
  quickly through one complete 360-degree turn in five seconds ..." gave a turn that
  returns to the start (similarity to frame 0: 0.98 at frame ~79).
- Image-only frame angles are NOT reliable: Wan eases in/out (frame 20 of 80 was ~45 deg,
  frame 40 ~90 deg), may swing back instead of completing the turn, and front/back-similar
  objects (stalls, gable houses) fool both the loop-closure and the mirror test.
- Wan's background is off-white (~250) with VAE specks on the frame edge: measure the
  background from the border and ignore a 4 px frame (`s5_hero._silhouette`).

## Hero: render matching and Hunyuan3D-2mv findings (2026-10-05, experimental)
- Render matching (`s5_hero.match_angles`, `scripts/blender_turntable.py`) is correct on
  consistent data (synthetic box: eased full turn recovered within 10 deg, front/back
  told apart) but FAILS on real Wan orbits (stall, house): the track stays within
  ~25-50 deg of the start. Two reasons: Wan's "turn" barely changes the silhouette (house
  width/height 1.08-1.24 over the orbit vs 1.15-1.40 for the mesh's real rotation), and
  the mesh's unseen sides (TRELLIS) and the orbit's (Wan) are independent inventions, so
  only the front is shared.
- Hunyuan3D-2mv turbo: `ComfyUI-app/models/checkpoints/hunyuan3d-dit-v2-mv-turbo.safetensors`
  (model file only, user's OK 2026-10-05; contains conditioner + model + vae), workflow
  `workflows/hunyuan3d_mv.json`. ~30-40 s per shape on GPU 1, untextured.
  - On the stall with frames picked by eye (front 0, left 20, back 40, right 60), the
    4-view shape was cleaner from the sides than front-only; swapping left/right gave a
    visible conflict blob, so a wrong side assignment shows. All Hunyuan shapes had a
    ground slab (floor shading in the frames) and thin stray spikes; single-image TRELLIS
    stayed more detailed (and textured).
  - License: Tencent Hunyuan Community License: not licensed in the EU, UK or South
    Korea; >1M MAU needs a separate licence.
- Running GPU experiments while the user works: wait for an idle ComfyUI queue and for
  GPU 0 to be free (VLM down, no trellis locks); TRELLIS for scratch work via the real
  `scripts/trellis` with `--out-dir` in scratch, so its GPU claim is visible to the UI.

## Cleanup (stage 6) findings (2026-10-05)
- **Snap Blender does nothing when its stdout is a regular file** (exit 0, no output, the
  script's files never written). Pipe its output (`subprocess.PIPE`) and copy it to the log.
  `tests/test_stage6.py::test_module_launches_real_blender` guards this.
- glTF splits vertices along UV seams, so an imported TRELLIS mesh is many islands;
  decimating unwelded islands shrinks them and opens cracks (bake speckles). Weld
  (`remove_doubles`, 1e-5 of the size) before decimating.
- Budgets measured in Cycles (Workbench previews ignore normal maps and mislead): a stall
  warped at 15k and was close to the original at 30k; a house was fine at 30k.
- QuadriFlow rejects TRELLIS meshes as non-manifold even after a voxel remesh plus
  non-manifold repair; it works headless on clean meshes. Kept behind `--quad`.
- TRELLIS output faces glTF +Z (Blender -Y) and is ~1 unit tall; it can come out as a flat
  card (a barrel 4 mm deep) and with proportions far from the plan (house 2.3x deeper):
  the cleanup report warns about both.
