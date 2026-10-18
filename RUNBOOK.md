# Terraformer runbook

How to produce library assets for one area of a city, day to day, without editing code or
JSON. Everything an artist adjusts is done in the app (the UI at `ap ui`) or with one `ap`
command. Examples use the placeholders `my-project`, `my-area`, `my-asset` and shots
`med-a`, `med-b`. USAGE.md has every command's details; PLAN.md has the design.

## 1. Start the services

| What | Command | Needed for |
|---|---|---|
| The app | `ap ui` (http://127.0.0.1:8700) | everything below |
| ComfyUI | **Start ComfyUI** in the app's Status box, or `ap comfy start` | sheets, frames, SAM checks |
| Check it | the Status box (Stop and Restart appear once it runs), or `ap comfy status` / `ap comfy-check` | |

Start uses the machine's own launcher and GPU from config `[comfyui.service]` (here:
`run_comfy.sh` on GPU 1, port 8188), logs to `$AP_ROOT/logs/comfyui.log`, and also starts the
Comfy API proxy when the backend is `sdk`. Startup progress and any errors from the log show
in the Jobs panel (a "Comfy" row while it starts). Starting a job that needs ComfyUI while
it's down asks **Start ComfyUI and run**: the job waits in the GPU 1 queue until ComfyUI
answers, and fails with the log's error if the start fails. Stop cancels ComfyUI jobs first
(it asks); Restart keeps queued jobs, which run once ComfyUI is back.

Builds (Blender) and review renders run on the CPU. TRELLIS runs on GPU 0, ComfyUI on GPU 1.
The local vision model starts by itself when a step needs it and stops after 10 idle minutes.

## 2. Preflight

`ap preflight` checks ComfyUI (and the proxy for `sdk`), the model files the workflows load,
the local models and envs under `$AP_ROOT`, Blender and (with `--ue`) Unreal. A required check
that fails names what's missing; fix that first. Optional checks (TRELLIS, Unreal) only warn.

## 3. The daily loop for one area

An area is a set of shots and/or districts. Define it once:

```
ap area define my-project my-area --shots med-a med-b
ap area status my-project my-area        # one line per asset: what it has, its next step
```

`ap area status` prints each asset's next step as a place in the UI or one command. Work
down the list, largest frame share first:

1. **Decide the source** of undecided entries: Library › Entries (good source, description,
   reference, kit, exclude).
2. **Sheets.** `ap library sheets my-project --area my-area` makes two sheets for every
   decided, unapproved entry that has none (about 55 s each). For another try, use
   Library › Reference sheets › **+2 more** on the asset (or `--more`, or `--unit my-asset`). Add
   `--view three-quarter` for deep buildings.
   - To steer a reroll, type **guidance** in the field next to +2 more, e.g. "slatted timber
     counter boxes under one shared timber pergola, no stone" (Enter also rerolls). It is added
     to that asset's prompt for that batch only and saved with those sheets: each one shows it
     as a caption. The field starts from the last batch's guidance; edit or clear it before the
     next reroll. CLI: `ap library sheets my-project --unit my-asset --guidance "..."`.
3. **Buildings and structures (procedural route).**
   - A new asset needs a brief from its best sheet view:
     `ap library brief my-project my-asset refs/library/my-asset/sheet_000.png front`.
   - Then Library › Asset review › the asset: match the sheet with the brief fields
     (section 4) and press **Save & Rebuild**. A quick preview appears in seconds, the Cycles
     renders about 15 s later.
   - Approve with **A**, or **C** (Needs changes) with a note.
   - Batch rebuild after edits, or after a tool update:
     `ap library build my-project --area my-area` (add `--renders` for the review images).
4. **Props (TRELLIS route).**
   - Star a sheet, then Review › Cut views, and click the view to use.
   - Make 3D there, or for all props at once: `ap library trellis my-project --area my-area`.
   - Approve each result in Review (**Approve** / **Needs changes**).
5. **Per-building instances** (every building at its own footprint and storeys):
   `ap library instances my-project --area my-area`.
6. **Vegetation.** Planters, garden strips, roof-edge planters and beds are tagged as
   vegetation zones on every build, and review renders show proxies on them. Street and
   plaza tree slots: `ap site vegetation my-project --preview med-a` (settings: City tab ›
   Rules › Street trees). The plants themselves come from a PCG graph in Unreal later.
7. **Close the day:** `ap area checklist my-project my-area` refreshes the table in
   `areas/my-area.md` in the project. Text you write outside the table is kept. Commit the
   project repo.

Every batch command skips approved assets, skips builds whose brief hasn't changed (`--force`
rebuilds them), reports a failure and goes on, and ends with a summary. After a crash, or after
restarting ComfyUI, run the same command again: finished items are skipped.

## 4. Matching a sheet with brief fields

Open the asset in Library › Asset review. The reference view is on the left, the current
build in the middle, the form on the right. Fields marked **brief** differ from the type's
defaults. Change one or two things at a time, then Save & Rebuild.

| On the sheet | Field (Parts › …) |
|---|---|
| Overall size, storey height, bay rhythm | Footprint, Height, Storey, Bay rhythm (top of the form) |
| Window size and position | facade › window: w, h (shares of a bay or storey), sill, reveal |
| Taller or wider openings on one storey | facade › open w / open h: one factor per upper storey, e.g. `1, 1, 0.8` |
| Window frames in timber | facade › window › frame mat `wood`, frame t (m) |
| Pointed (lancet) or segmental heads | window › kind `pointed` / `seg`; ground › arch `pointed` for arcades |
| Loggias: depth, opening, parapet | facade › loggia: depth (m), w, h, parapet h (m), balustrade glass / solid / rail, lining `wood` |
| Glazing between the slabs | pattern entry `curtain`; facade › curtain: mullion m, transom |
| Exactly N windows on an attic or a plain wall | facade › count N, spacing (m between centres; 0 spreads them) |
| Bay sequence | facade › pattern, e.g. `loggia, slot` (window, slot, loggia, blind, curtain, solid) and pattern w |
| Doors and the entrance | facade › ground: door count, door width m, door height m, door leaf `wood` / `glass`, entrance m (width), entrance x (−0.5 to 0.5 along the front) |
| Ground storey type | facade › ground › kind (plain, shop, sparse, arcade, pilotis, recessed, blind arcade) |
| A slab over the ground storey | facade › ground › canopy (m) |
| Fins and pilasters | facade › pilasters: every, w, proj, profile `pier` (projecting), `strip` (flat with a shadow line), `fin` (thin and deep), from ground |
| Panelled concrete or stone joints | facade › panels: panel w, panel h (0: one per storey), groove |
| Set-back or projecting bays | facade › recess (Add): x0, x1 (shares of the front), depth (m; negative projects) |
| Balconies | facade › balcony: kind slab / band, depth, balustrade, every, from |
| Attic storey | top: storeys, setback, span, balustrade, planter; top › facade for its windows |
| Garden walls along the street | street garden: depth, h, gap, length (share of the frontage), planter |
| Planters everywhere | planter: box, trough, pots, hedge, bed, none (a part's own planter overrides it) |
| Low side buildings | annexes (Add): side, at, w, d, h, pattern, canopy |
| Hipped roof | hip roof (rise, over) on civic and arcade halls; campanile › cap `hip` |
| Wall material per band | Materials › Type per band (auto reads the sheet) and Colour; Surfaces library / patch |
| Texture scale (joint size, veining) | Materials › the × box next to Type per band: a multiplier on the material's tile size, default 1 (0.5 on 2 m stone gives 1 m blocks; 2 doubles them). Saved only when not 1 |
| Raised moulding around the basilica's central block | basilica front › panel frame: on, w (share of the block width), proj (m proud of the wall), margin (share in from its edges), gap (m clear of the window and oculus). Off by default |
| Kit pieces (gates, colonnade bays) | the piece's measurements in metres, in the same form |

Validation runs as you type. Errors (red) block the rebuild. Notes (grey) only warn, for
example a footprint outside the catalog's range: the sheet may show another size. If a
rebuild changes the size of an asset that a locked frame shows, the app asks first.

## 5. Common failures

| Symptom | Cause | Fix |
|---|---|---|
| "ComfyUI is not answering" | ComfyUI not started, or it stopped | Status › Start ComfyUI (or `ap comfy start`), then run the same command again |
| The app shows an older UI (old name, no Start ComfyUI button) | The browser kept old scripts (fixed 2026-10-17: the page and its scripts are now versioned), or `ap ui` still runs old code | Restart `ap ui` once (it prints the code path and git commit it serves), then reload the page; afterwards a yellow bar offers Reload whenever the app is updated |
| Start ComfyUI fails | The launcher exited or didn't answer in time; the Jobs panel shows the log's error lines | Read `$AP_ROOT/logs/comfyui.log`; check `ap comfy status` (launch command, GPU) |
| "no brief for …" | No brief yet | `ap library brief my-project my-asset <sheet> front` |
| "no view chosen" (TRELLIS) | No view clicked in Review | Review › click a view of the asset |
| "not decided yet" (sheets) | No source decision | Library › Entries |
| "GPU 0 is busy with TRELLIS" | Another TRELLIS run holds the GPU | Wait, or Stop it in the app's job list |
| A wall or strip renders black | Two faces in one plane | The builder pulls these apart; if one remains, note it (Needs new feature) |
| A dark slot beside a projecting bay | Fixed 2026-10-16; old builds | Rebuild the asset |
| Planters look bare | Plants come from PCG; reviews show proxies | Nothing to do |
| The build doesn't change after Save | The field is overridden lower down (a facade's own value) | Look for the **brief** badge on a nested part |
| `ap area status` names no shots | The shots' id passes are missing | `ap shots render my-project med-a` |

## 6. Starting a new district

1. Check that the district's shots exist and are rendered: `ap shots list my-project`.
2. `ap area define my-project north --districts north` (or `--shots ...` for the shots that
   cover it), then `ap area status my-project north`.
3. Run section 3 on it. Types already approved in another area are skipped; new materials
   of an approved type need only their own brief (copy the closest brief's fields in the form).
4. `ap library instances my-project --area north` for the per-building builds.

## 7. What you can do yourself, and what needs Claude Code

You can do, from the app or one command:
- sheets, rerolls, views, the choice of view;
- briefs from a sheet, every brief and material field, rebuilds, approvals;
- TRELLIS for props, instances, street-tree slots and previews;
- area status and checklists, project commits.

Needs Claude Code (code changes):
- a new builder part or form (a feature no field covers: mark it **Needs new feature** with
  a note, and `ap library review apply my-project` lists them);
- new generators for a catalog form;
- fixes to the builder (geometry errors that a rebuild doesn't solve);
- anything in Unreal (imports, look scenes, PCG) until that work resumes;
- changes to ComfyUI, new models, paid APIs.
