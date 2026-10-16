#!/usr/bin/env bash
# The demo path, end to end. Run `ap preflight` (--ue for the area run) first.
#
#   scripts/demo.sh starter [SLUG]
#       Neutral starter layout: project, greybox, shot passes, two concept frames for the
#       "street" shot (ComfyUI). No Unreal needed. SLUG defaults to my-project.
#
#   scripts/demo.sh area PROJECT SHOT ENTRY [ENTRY ...]
#       One area of an existing project, brief to Unreal render: two new frames for SHOT
#       (written to $AP_ROOT/outputs/demo/PROJECT/frames, never into a locked shot's batches),
#       procedural library builds of the ENTRY buildings visible in SHOT and their instances
#       near its camera, GLB export, Unreal import, and a look scene of SHOT. The look uses
#       the preset site/look_tests/demo-SHOT.json: created on first run as a copy of the
#       shot's own preset (if any) with the vetting gate off, so unapproved builds are placed.
#
# Times (2x R9700, warm): frames ~50-100 s each, builds a few s each, instances ~10 s per
# building, export a few minutes on a large city, UE import ~1 min, look render a few minutes.
set -euo pipefail
REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
AP="${AP:-$REPO/scripts/ap}"
source "$REPO/scripts/ap_root.sh"
step() { echo; echo "=== $*"; }

case "${1:-}" in
  starter)
    slug="${2:-my-project}"
    step "project and starter layout";   "$AP" new "$slug"; "$AP" site init "$slug"
    step "greybox (Blender, CPU)";       "$AP" site build "$slug"
    step "shot passes (Blender, CPU)";   "$AP" shots render "$slug"; "$AP" shots list "$slug"
    step "style";  "$AP" style text "$slug" "sunlit classical town, honed limestone and white marble, terracotta accents, umbrella pines and cypresses, clear Mediterranean light, photoreal"
    step "concept frames (ComfyUI)";     "$AP" frames generate "$slug" street -n 2 --seed 7
    "$AP" frames show "$slug" --prompt ;;
  area)
    [ $# -ge 4 ] || { sed -n 2,14p "$0"; exit 2; }
    project="$2"; shot="$3"; shift 3; entries=("$@")
    root="$AP_ROOT"
    pdir="$root/projects/$project"
    preset="$pdir/site/look_tests/demo-$shot.json"
    if [ ! -f "$preset" ]; then
      python3 - "$pdir/site/look_tests/$shot.json" "$preset" "$shot" <<'EOF'
import json, os, sys
src, dst, shot = sys.argv[1:]
d = json.load(open(src)) if os.path.exists(src) else {}
d.pop("history", None)
d.update(shot=shot, gate=False,
         _note_demo="demo run: a copy of the shot's preset with the vetting gate off")
os.makedirs(os.path.dirname(dst), exist_ok=True)
json.dump(d, open(dst, "w"), indent=2)
print(f"wrote {dst}")
EOF
    fi
    step "concept frames (ComfyUI)"
    "$AP" frames generate "$project" "$shot" -n 2 --force --out "$root/outputs/demo/$project/frames"
    step "library builds (Blender, CPU): ${entries[*]}"
    "$AP" library build "$project" "${entries[@]}"
    "$AP" library instances "$project" "${entries[@]}" --near "$shot"
    step "export (Blender, CPU)";        "$AP" export "$project"
    step "Unreal import (headless)";     "$AP" ue import "$project"
    step "look scene (Unreal render)";   "$AP" ue look "$project" "demo-$shot" ;;
  *) sed -n 2,17p "$0"; exit 2 ;;
esac
