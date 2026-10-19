# Terraformer runbook

How to produce library assets for one area of a city, day to day, in the app: every step
below is a click. Nothing needs a terminal after installing (README › Setup); the command line
still works and is listed in the appendix. Examples use the placeholders `my-project`,
`my-area`, `my-asset` and shots `med-a`, `med-b`. USAGE.md has every command's details;
PLAN.md has the design.

## 1. Open the app

- **Once:** Tools › **Install desktop launcher…** › Install. It adds **Terraformer** to the
  applications menu.
- **Every day:** open Terraformer from the applications menu. It starts the app if it isn't
  running and opens it in the browser (http://127.0.0.1:8700).
- **ComfyUI** (sheets, frames, SAM checks): **Start ComfyUI** in the Status box. Stop and
  Restart appear once it runs. Tools › ComfyUI status… shows the same buttons, the GPU and
  the startup log line. Starting a job that needs ComfyUI while it's down asks **Start
  ComfyUI and run**: the job waits until ComfyUI answers.
- The vision model starts by itself when a step needs it (Tools › Start / Stop vision LLM to
  do it by hand). Builds and review renders run on the CPU, TRELLIS on GPU 0, ComfyUI on GPU 1.
- Every long step is a job: progress in the **Jobs** panel, details and Stop in its dialog.
  **Run in background** closes the dialog and the app stays usable; click the job in the Jobs
  panel to open it again. A small notice in the corner says when a background job is done or
  failed (click it for the details).

### What runs where

| Lane (Jobs panel) | Runs | At once |
|---|---|---|
| **GPU 0** | TRELLIS, the local vision model (scene analysis, Brief & build's brief, guidance, band materials) | 1 job |
| **GPU 1** | ComfyUI: scenes, reference sheets, cut views, frames, SAM checks | 1 job |
| **CPU** | Blender builds and review renders, city plan, greybox, shot passes, export, Unreal | 2 jobs (config `[jobs] cpu_slots`) |

- A job started on a busy lane waits in line; the Jobs panel shows its place (#1, #2…).
  Jobs that write the same files (one asset, the greybox and shots, the Unreal project) wait for
  each other even on the CPU.
- A GPU job also waits until its GPU has enough free VRAM (config `[jobs.vram_gb]`), for
  example while your Blender or an Unreal editor holds GPU 0. The job's note says how much is
  free and what holds it. ComfyUI's own memory counts as free for ComfyUI jobs.
- The vision model loads for its job and unloads after it, unless another vision job is
  queued. TRELLIS unloads it before it starts. **Brief & build** writes the brief on GPU 0, then
  moves to the CPU for Blender, so GPU 0 is free for the next job during the build.
- Unreal renders pick whichever GPU has room themselves (they run on the CPU lane).

## 2. Preflight

Tools › **Preflight…** checks ComfyUI (and the proxy for the `sdk` backend), the model files
the workflows load, the local models, Blender and, with *Also require Unreal Engine*, Unreal.
Each failed check has a fix button where one exists (**Start ComfyUI**; **Copy** for an install
command). Optional checks only warn.

## 3. The daily loop for one area

Library › **Areas…** is the area panel. An area is a set of shots and/or districts: **New
area…** defines one (tick its shots or districts). The table lists every asset of the area with
what it has (sheets, brief, build, review) and its **next step as a button**. Work down the
list, largest frame share first:

1. **Decide** (undecided entries): the button opens Library › Asset review at the entry.
   **Manage entry…** on its card: decision (good source, description, reference frame,
   exclude), route (procedural or TRELLIS), description (yours / auto), and for procedural
   entries **Form** and **Size**. The area panel marks entries with *no form* or *no size*
   (its **Set form…** button opens this) so they're fixed before Brief & build. See section 7 for new,
   duplicated and renamed entries.
   - **Form gaps** (the box above the area's table) lists those assets with their fixes as
     buttons, the suggested one first: *Form: …, size* (from the entry's words: a portico, an
     arcade block, a courtyard; kit modules take their kit piece's size; a city wall one wall bay),
     or *Use the TRELLIS route* for a building no form describes. Hover a button for why.
2. **Sheets.** The area's **Sheets for missing…** makes two sheets for every decided,
   unapproved entry that has none (about 55 s each; it shows the count first). For another try:
   Library › Reference sheets › **+2 more** on the asset.
   - **Guidance** steers a reroll: type it in the field next to +2 more, e.g. "slatted timber
     counter boxes under one shared timber pergola, no stone" (Enter also rerolls). It goes into
     that asset's prompt for that batch only and is saved with those sheets, which show it as a
     caption. The field starts from the last batch's guidance; edit or clear it before the next
     reroll.
3. **Star the best sheet.** The asset's row then shows its next step:
   - **Brief & build** (procedural: buildings and structures). Choose the view to measure
     (*Auto* picks the most frontal one; curved buildings such as vaults need you to pick the
     left, middle or right view), the **Form** (the building generator; pre-selected from the
     catalog, the brief, or a suggestion from the entry's description, e.g. *One-storey colonnade
     range* for a portico) and the **Size** (width × depth × height, from the plan). The vision
     model then writes the brief and Blender builds and renders it. The form and size you choose
     are kept with the entry.
   - **Guidance for the next build** (the text box in the same dialog, or **Guidance…** next to
     Save & Rebuild in Asset review): describe what to change, e.g. "round arches on square
     piers, a lighter gallery above". The vision model reads it with the starred sheet and
     proposes brief edits as a before → after list; untick or adjust any row, then **Accept &
     build**. Edits it can't map to a field are listed as dropped, with the reason (that's
     usually a builder feature to request). The guidance is saved with the build's version. With a brief already there it asks: *Re-brief from this sheet* or *Just
     rebuild*. Then **Open in Asset review**.
   - **Cut views** (TRELLIS: props and irregular landmarks), then **Choose view in Views and
     3D**: click the view to use and **Make 3D** there. Tick **New seed** for another try
     from the same view; to steer the result with words, reroll the sheet with guidance
     (step 2) instead.
4. **Review.** Library › Asset review › the asset: match the sheet with the brief fields
   (section 4) and press **Save & Rebuild** (a quick preview in seconds, Cycles about 15 s
   later). Approve with **A**, or **C** (Needs changes) with a note, then **Save review**. A new
   brief or a brief edit sets an Approved asset back to Needs review. TRELLIS results are
   approved in Views and 3D (**Approve** / **Needs changes**).
5. **Batches** for the area (panel buttons; each shows what it will do first): **Build all…**
   after brief edits or a tool update, **TRELLIS all…** for every prop with a chosen view,
   **Instances…** for every building at its own footprint and storeys.
6. **Vegetation.** Planters, garden strips and beds are tagged as vegetation zones on every
   build; review renders show proxies. Street and plaza tree slots: World › **Vegetation
   slots…** (with a preview shot; settings in City › Rules › Street trees). The plants come from
   a PCG graph in Unreal later.
7. **Close the day.** **Write checklist** in the area panel refreshes `areas/my-area.md` in the
   project (text outside the table is kept). Then File › **Commit project…** commits the
   project's own private repo (the list shows what changes; its .gitignore keeps media out).

Every batch skips approved assets and builds whose brief hasn't changed, reports a failure and
goes on. After a crash, or after restarting ComfyUI, run the same batch again: finished items
are skipped.

**Versions.** Every build (procedural or TRELLIS) is a version of its asset, shown as a strip
in Asset review and in Views and 3D: thumbnail, date, source (sheet, view, form, guidance,
seed) and size. Tick two and **Compare** (side by side, with the source and brief fields that
differ); **Restore** brings one back (a procedural version is rebuilt from its saved brief; a
pinned one, or a TRELLIS one, comes back as it was); **Pin** keeps a version and its 3D model
for good; **Delete** removes one (asks first; never the Approved one). The last 3 unpinned
versions are kept (Tools › **Settings…**); when there are more, the strip offers to remove the
old ones. Pinned, Approved and current versions are never removed. Tools › **Storage…** shows
the space per asset and per project, with "Delete unpinned versions" per asset or for the
whole project.

The review round: Library › **Review round: task list…** lists every Needs changes and Needs
new feature note (what Claude Code works from); **Review round: mark done…** marks the handled
assets Ready for review and opens the next round.

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
| Hipped roof | hip roof (rise, over) on civic and arcade halls, colonnade ranges and arcades; campanile › cap `hip` |
| Arches on square piers (arcades, porticoes, arcaded blocks) | arcade: bay m (pier centre to centre), pier w, arch h (to the crown), arch `round` / `seg` (rise: share of the opening), h (the arcade storey), depth (the walk), back `shop` / `wall`; on a colonnade range tick *arcade* |
| The moulding at the arches' springing | arcade › impost: h, proj (untick for none) |
| A storey or open gallery over the arcade | arcade › gallery: kind `loggia` (open arches) / `windowed`, storeys (0: fill the height), storey h, arch `round` / `seg` / `rect`, w / h / sill (shares), balustrade, balustrade h |
| Stone frames round the windows over an arcade | arcade › gallery › grid: w, proj |
| A set-back top storey (arcade form) | attic: storey h, setback, side setback (0 on a module's party walls) |
| Party walls (bay and corner modules) | side: pattern `solid`, ground kind `solid` |
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
| "ComfyUI is not running" | ComfyUI not started, or it stopped | Status › Start ComfyUI, or **Start ComfyUI and run** in the dialog |
| Start ComfyUI fails | The launcher exited or didn't answer in time; the job shows the log's error lines | Tools › Preflight…; the log is `$AP_ROOT/logs/comfyui.log`; the launch command is config `[comfyui.service]` |
| A yellow bar "The app was updated" | New code since the page loaded | **Reload** |
| Brief & build: "no near-front view" | No view on the sheet is frontal enough (curved buildings) | Brief & build again and choose the left, middle or right view |
| Brief & build: "has no building form" / "has no size" | The entry's catalog type has no generator, or no size | Choose Form / Size in the Brief & build dialog, or Manage entry… › Form, Size |
| "no view chosen" (TRELLIS) | No view clicked | Views and 3D › click a view of the asset |
| "not decided yet" (sheets) | No source decision | Manage entry… › Decision |
| "GPU 0 is busy with TRELLIS" | Another TRELLIS run holds the GPU | Wait, or Stop it in the Jobs panel |
| A wall or strip renders black | Two faces in one plane | The builder pulls these apart; if one remains, review it as Needs new feature |
| Planters look bare | Plants come from PCG; reviews show proxies | Nothing to do |
| The build doesn't change after Save | The field is overridden lower down (a facade's own value) | Look for the **brief** badge on a nested part |
| The area panel names no shots | The shots' id passes are missing | World › Shots… › tick them › Render passes |

## 6. Starting a new district

1. World › **Shots…** lists the shots, with **Render passes for checked** for those without
   passes. World › **Add shot…** adds one.
2. Library › Areas… › **New area…**: tick the district (or the shots that cover it).
3. Run section 3 on it. Types already approved in another area are skipped; a new material of
   an approved type needs only its own brief (copy the closest brief's fields in the form).
4. **Instances…** in the area panel for the per-building builds.

## 7. Entries: new, duplicate, rename

- Library › Asset review › **New entry…**: id, name, description, kind (building or prop),
  route, size. It gets sheets like any other entry.
- **Manage entry… › Duplicate…** copies an entry with its sheets, views, brief, builds and
  TRELLIS results to a new id (not its review status). Use it to keep a model while rerolling
  the original: e.g. duplicate a market stand to `stone-counter`, then reroll the stand.
- **Variant sets** (props): Manage entry… on a prop type's entry (e.g. a market's canopy tables)
  › **Make a variant set**, then **Add entry** for each variant (make it first with New entry…)
  and give each a **weight**. Every place of that prop picks one entry by weight, the same one
  every time; adding an entry moves only some places to it, the rest keep theirs. *Areas* limits
  the set to those areas' places. Variants appear in the area table (*variant of …*) and go
  through sheets and TRELLIS like any prop. CLI: `ap library variants my-project my-prop
  --member my-variant=2`.
- **Manage entry… › Rename…**: entries of your own get a new id (their files move); entries
  from the library plan keep their id and change only their display name.

## 8. Frames, Unreal and the rest

- **Frames:** World › Generate frames… (pick shots), Lock a shot… (or its greybox).
- **Terrain:** World › Terrain: analyse locked skylines, build, rebuild the outer ring, apply
  to the layout (asks first), Unreal landscape tiles, terrain into the Unreal project (asks).
- **Unreal:** Unreal › Create UE project, **Import…** (reset layout, only stale, prune; reset
  and prune need a backup first and ask before discarding), Stale report…, Render shots…,
  Look scene…, Pull layout. Backups: the Unreal tab or Pipeline › Back up Unreal project.
- **Other library tools:** Library › Pending slots…, Silhouette check….
- **Demo:** Help › Run demo… (the starter project, or one area end to end).

## 9. What needs Claude Code

- A new builder part or form: a feature no field covers. Review it as **Needs new feature**
  with a note; the review round's task list collects them.
- New generators for a catalog form; fixes to the builder (geometry errors a rebuild doesn't
  solve).
- Unreal work beyond the menu items (look-scene presets, PCG).
- Changes to ComfyUI, new models, paid APIs.

## Appendix: command-line equivalents

Everything above also runs from a terminal (`ap --help`, USAGE.md):

| In the app | Command |
|---|---|
| Open Terraformer (launcher) | `ap ui` (`ap launcher install` to add the launcher) |
| Status › Start / Stop / Restart ComfyUI | `ap comfy start` / `stop` / `restart` / `status` |
| Tools › Preflight… | `ap preflight [--ue]` |
| Library › Areas… (define, status, checklist) | `ap area define my-project my-area --shots med-a med-b`; `ap area status my-project my-area`; `ap area checklist my-project my-area` |
| Sheets for missing… / +2 more (with guidance) | `ap library sheets my-project --area my-area`; `--unit my-asset --guidance "..."`; `--view three-quarter` |
| Brief & build | `ap library brief my-project my-asset refs/library/my-asset/sheet_000.png front` (or a view number 0 / 1 / 2), then `ap library build my-project my-asset --renders` |
| Build all… / TRELLIS all… / Instances… | `ap library build|trellis|instances my-project --area my-area` |
| Manage entry… (decision, route, form and size, variant set) | `ap library decide my-project ENTRY ...`; `ap library route my-project ENTRY procedural|trellis`; `ap library form my-project ENTRY FORM --size W D H`; `ap library variants my-project ENTRY --member OTHER=WEIGHT` |
| Review round task list / mark done | `ap library review apply my-project [--done ASSET ...]` |
| Pending slots… / Silhouette check… | `ap library pending my-project --shot SHOT`; `ap library silhouette my-project my-asset --shot SHOT` |
| World › Generate frames… / Lock a shot… | `ap frames generate my-project SHOT -n 4`; `ap frames lock my-project SHOT [--greybox]` |
| World › Shots… / Add shot… | `ap shots list|add|render my-project ...` |
| World › Vegetation slots… / Plan overlap… | `ap site vegetation my-project [--preview SHOT]`; `ap site overlap my-project SHOT` |
| World › Terrain: … | `ap terrain analyse|build|outer|apply|landscape|ue my-project` |
| Unreal › … | `ap ue init|import|stale|render|pull-layout|look|backup my-project ...` |
| Help › Run demo… | `scripts/demo.sh starter|area` |
| File › Commit project… | `git -C $AP_ROOT/projects/my-project commit -am "..."` |
