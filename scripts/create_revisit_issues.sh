#!/usr/bin/env bash
# Create the "revisit" label and one GitHub issue per parked idea in PLAN.md
# ("Revisit when models improve"). Safe to re-run: existing issues (same title) are skipped.
# Needs the GitHub CLI, logged in: gh auth login
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/.."
command -v gh >/dev/null || { echo "gh not found: see PLAN.md / install the GitHub CLI" >&2; exit 1; }

gh label create revisit --color FBCA04 \
  --description "Parked: retry when a better model or release appears" --force >/dev/null

issue() {  # title, body
  if gh issue list --label revisit --state all --search "\"$1\" in:title" --json title \
       --jq '.[].title' | grep -Fxq "$1"; then
    echo "exists: $1"; return
  fi
  gh issue create --title "$1" --label revisit --body "$2"
}

FOOT='Details: PLAN.md "Revisit when models improve" and CLAUDE.md. Test on a scratch copy of a project, not a live one.'

issue "Revisit: hero tier / multi-view 3D" "**What we found.** Flux reference sheets don't give true side/back views. Wan 2.2 orbit frames + Hunyuan3D-2mv (frames picked by hand) give a shape slightly cleaner from the sides than front-only, but untextured, on a ground slab, with stray spikes, and less detailed than single-image TRELLIS.2. Hero assets currently use single-image TRELLIS like the rest.

**Retry when.** An open multi-view model that keeps geometry consistent across views runs locally (a turnaround / multi-view diffusion model), or TRELLIS releases multi-image conditioning.

**How to rerun.**
\`\`\`
ap hero orbit PROJECT PLAN ASSET
ap hero mesh PROJECT PLAN ASSET orbit_sN --front 0 --left 20 --back 40 --right 60
ap 3d PROJECT PLAN ASSET   # single-image baseline
\`\`\`
Compare the shapes from all four sides (scripts/blender_turntable.py).

$FOOT"

issue "Revisit: orbit frame angles (render matching)" "**What we found.** Render matching (orbit frames vs renders of the asset's TRELLIS mesh, Viterbi-smoothed) recovers an eased full turn within 10 deg on synthetic data, but on real Wan 2.2 orbits it tracks only 25-50 deg: Wan's turntable barely changes the silhouette (house width/height 1.08-1.24 vs 1.15-1.40 for a real rotation), and the mesh's and the orbit's unseen sides are independent inventions.

**Retry when.** A video model whose turntable orbits keep 3D proportions, or a feed-forward pose estimator (VGGT / MASt3R successor) that runs on ROCm.

**How to rerun.**
\`\`\`
pytest tests/test_hero.py::test_match_angles_recovers_a_full_turn   # must still pass
ap hero orbit PROJECT PLAN ASSET
ap hero angles PROJECT PLAN ASSET orbit_sN
\`\`\`
Success: rotation close to 360 deg and front/left/back/right all picked.

$FOOT"

issue "Revisit: texture Hunyuan3D-2mv shapes" "**What we found.** Not attempted. Hunyuan3D-2mv (turbo, in ComfyUI) gives untextured shapes; Hunyuan3D 2.1 paint builds custom rasterizer extensions written for CUDA, untried on ROCm here.

**Retry when.** Hunyuan3D paint supports ROCm, or ComfyUI gets native texturing nodes for Hunyuan meshes.

**How to rerun.**
\`\`\`
ap hero mesh PROJECT PLAN ASSET orbit_sN --front 0 --left 20 --back 40 --right 60
\`\`\`
then texture the shape from the asset's chosen view and compare with its TRELLIS GLB in the Review tab.

$FOOT"

issue "Revisit: TRELLIS.2 1536_cascade out of memory" "**What we found.** \`1536_cascade\` fails with a genuine out-of-memory error in CuMesh \`fill_holes\` -> \`get_edges\` on the 32 GB R9700 (not silent corruption). \`512\` and \`1024_cascade\` work.

**Retry when.** A TRELLIS.2 / CuMesh release with chunked or lower-memory hole filling, or a GPU with more memory.

**How to rerun.**
\`\`\`
trellis turret.webp --type 1536_cascade
trellis crown.png --type 1536_cascade
\`\`\`
Success: no OOM, raw F/V close to 2.0 in the run's JSON, and all four views fine in scripts/blender_check_glb.py.

$FOOT"
