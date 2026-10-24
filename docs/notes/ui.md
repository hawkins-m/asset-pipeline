# Ui

The browser UI: static versioning, script scope, list layout, polling redraws, job progress, job lanes and VRAM. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## UI basics (from the library 3D notes, 2026-10-17)
- **The UI's static files are versioned (2026-10-17).** index.html is served no-store with
  `?v=<content hash>` on every /static/ URL; unversioned static is `no-cache`. Before that, a
  browser kept the pre-rename shell.js across server restarts and hard refreshes. Python
  changes still need an `ap ui` restart (it prints the code path, git commit and build id);
  static changes show on reload, and an open page shows a Reload bar (`/api/version`).
  Headless UI checks: Playwright with the system Chrome (`channel="chrome"`), its own venv
  `$AP_ROOT/envs/browser-test`.
- UI scripts share one global scope: a second top-level `const clone` silently broke a whole
  script. Check with `cat asset_pipeline/ui/static/*.js > all.js && node --check all.js`.

## Library / Views and 3D layout (2026-10-18)
- Only the lists scroll (`.scroll-list`: #assets-list, #refs-list, #review-list; #asset-detail):
  #library-tab / #review-tab are flex columns with overflow hidden. jump.js keeps a scroll position
  per list and project (localStorage `ap.scroll.<slug>`), restored after every re-render and on
  tab / sub-tab switches: a hidden (display: none) list loses its scrollTop, and a re-render's
  momentary empty list clamps it to 0.
- jump.js wraps renderLibrary / renderRefs / renderReview / showSub / showTab by reassigning the
  globals; the tab bars call them late-bound (`s => showSub(s)`), or clicks would bypass the wrapper.
- Show filter: per-project choice (`ap.libFilter.<slug>`), default the current area (last picked
  in Library › Areas…, `ap.area.<slug>`, else the first). /library carries `areas` {name: entries}.
- tests/test_ui_layout.py drives a real `ap ui` on a scratch AP_ROOT with the browser-test env
  (skipped without it). A district-only area no longer needs the greybox (areas.members).

## Polling redraws (2026-10-18)
- While jobs run, `load()` re-renders every ~2 s. Views and 3D, Asset review and Reference sheets
  now redraw through `keyed.js`: rows keyed by asset with a data signature; unchanged rows keep
  their DOM, changed rows take over the old row's img / model-viewer elements (a file whose `?v=`
  changed is swapped in once loaded). Put everything a row shows into its `sig`, or it goes stale.
- Two CSS rules shared `.asset-review`: the Views row (grid) and the build-review buttons on
  Asset review cards (flex-wrap, later in the file, so it won). The Views row wrapped its parts
  freely and the version strip jumped beside / below the cards. The card div is `.asset-builds`.
- The version strip caches its data per asset (VER_CACHE) and redraws only on a change.

## Job progress (2026-10-18)
- The Make 3D bars "jumped back" every poll: TRELLIS had no counted progress, so both bars were
  marquees, and the Jobs panel (replaceChildren) and the dialog (innerHTML) rebuilt them on every
  redraw, restarting the animation. Bars are now built once per job and updated in place
  (`paintBar`), widths ease between polls, and `jobProgress` (one source for both) never lets a
  stage or a fraction go back (PROG_TRACK).
- `asset_pipeline/trellis_progress.py` reads the stages from trellis.log: loading (2 passes),
  sparse structure, shape (2 passes on cascade modes), texture are counted (passes folded into
  one fraction); decode, UV/bake and export have no usable count and show as striped stages.

## Job lanes and VRAM (2026-10-18)
- `jobs.JobQueue`: one thread per job, FIFO slots per lane (gpu0 1, comfy 1, cpu `[jobs] cpu_slots`,
  service 1). Jobs with the same `jobs.exclusive` key (a project's site/greybox, its UE project, one
  asset) never overlap, even on the CPU: CPU jobs used to run one at a time and some share files.
- `jobs.hop(lane)` moves a running job (Brief & build: brief on gpu0, then Blender on cpu);
  `jobs.using("gpu0", "vlm")` wraps every local VLM request (`vlm.AutoLocal`), so CPU jobs that read
  band materials or draft slots never run the VLM beside TRELLIS. Brief & build reads the band
  materials (`building_proc.read_band_materials`) before it hops, or the build reloads the model.
- `gpu_policy.GpuPolicy` keeps a GPU job queued until free VRAM >= `[jobs.vram_gb]` (the VLM: its
  model's `vram_gb`); memory of the process the job uses counts as free (ComfyUI's, the loaded VLM's).
  TRELLIS unloads the VLM first; the VLM is unloaded after a VLM job unless another one waits.
- **amd-smi's per-process `vram_mem` is wrong here**: 368 KB for a ComfyUI holding 2.96 GB on GPU 1
  (amd-smi 26.2.2). `gpu.vram_by_process` uses `rocm-smi --showpids --json` (VRAM per PID) plus
  the text of `--showpidgpus` (its devices; that flag has no JSON).
- Tests that need the old serial CPU lane pass `JobQueue(slots={"cpu": 1})`; `JobQueue()` has no policy
  (no VRAM checks), the app builds it with one.

## Workflow header, stages and guide (UX pass, 2026-10-20)
- `asset_pipeline/ui/static/workflow.js` holds the steps (tab order; Site and City are one step) and
  computes every status from loaded data. The header strip, the Stages list and the guide all read
  it. Never mark a step Complete because one file exists.
- Library "Out of date" means a locked frame the plan never considered (`plan.candidates`), not one
  missing from `plan.sources`: `frame_fit` drops old-layout frames on purpose.
- `showSub` hides every `[data-sub]` element in the Library tab and `showTab` every `.tabpanel[data-tab]`.
  Generated buttons use `data-go-tab` / `data-go-sub`; an empty-state button with `data-sub=""`
  vanished as soon as a sub-tab was shown.
- `tests/test_ui_workflow.py` runs workflow.js in node against stubbed project data. A new global
  that workflow.js touches needs a stub in its harness.
- Test UI changes on a second instance (`ap ui --port 8701` from the checkout); the user's `ap ui`
  serves the same static files, so a reload shows page changes there, but Python changes need the
  user to restart it.

## Long-job warning (2026-10-22)
- estimates.py: seconds per item = median of the last 15 recorded runs per job kind (and TRELLIS
  mode) in `$AP_ROOT/logs/job_times.jsonl` (the queue records every successful job), else the
  stages' own timings in the projects' runs.jsonl, else DEFAULTS (said so in the dialog).
- The UI asks the job's own endpoint first (`X-AP-Estimate: 1`): the handler runs its checks and
  counts and stops at jobs.submit (EstimateOnly). Only ESTIMATE_ROUTES (app.py) may be asked:
  routes that write before they submit (rebuild, guide/apply, shot camera, assembly, restore) are
  left out. Over 10 min api() shows the long-job dialog; Library › Areas batches show the same
  lines in their own confirm and pass {confirmed: true}. A canceled start throws "Not started.",
  which alert() ignores.
- Jobs the panel can't count show their time left from the job's est_s.

## Version actions refresh the row (2026-10-23)
- Every version action (Delete, Use this version, Keep / Stop keeping, Remove old versions) and a finished
  Make 3D / Reroll / rebuild / restore job goes through `versionsChanged(asset, onChange)` (versions.js):
  it refetches the listing into VER_CACHE, bumps `ROW_REV` (part of the Views and 3D row's sig, so the
  row is rebuilt), runs onChange, then redraws every `.version-strip` of the asset. Before, a Keep or a
  delete that changed no model card left the row and its strip stale until a reload.
- `versionsAfterLoad()` (called in `load()`) spots version jobs finished since the last load.
- Test: `tests/test_ui_versions_refresh.py` (browser); `_browse(..., seed=fn)` adds project data.

## Automatic values are real text (2026-10-24)
- Fields with an automatic value use `autoFieldHtml` / `autoFieldsWire` / `autoFieldValue` (shell.js): the
  automatic value is the field's editable text with an "automatic" / "edited" mark and Reset to auto, never a
  placeholder (it vanished on click). Unchanged or empty saves as automatic (null). Used by Manage entry's Name
  and Description and the brief form's Storey and Bay (`state: false`: that form has its own source marks).
- The brief form's Storey placeholder ignored `measured.storey_h`, which the builder takes first.
- Manage entry's Save says what changed fields affect (`entrySavedNotice`): the description only reaches the
  next reference sheets (sw_library.asset_fields -> s2_refs); form and size the next unguided Reroll or Brief &
  build; the brief, guided Reroll and Make 3D never read the description.
- Test: tests/test_ui_auto_fields.py (browser).
