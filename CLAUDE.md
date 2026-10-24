# CLAUDE.md

The tool is **Terraformer** (repo `hawkins-m/terraformer-3d`, renamed from `asset-pipeline` on 2026-10-18;
the old URL redirects). The command stays `ap`, the package `asset_pipeline` (pip: `asset-pipeline`), and
the checkout and `AP_ROOT` keep `asset-pipeline` names. **[PLAN.md](PLAN.md) is the source of truth for
the design**; read it before starting work, and it wins on design. This file holds the rules for every
step; verified machine facts and findings are in `docs/notes/` (index at the end).

## Repo rules
- The repo holds only code, ComfyUI workflow JSONs, configs and docs.
- **This repo is public; projects are private.** Never commit project-specific content:
  briefs, layouts, shot lists, greyboxes, generated media, project names or brief terms.
  Keep CLAUDE.md, PLAN.md and USAGE.md generic (placeholders like `my-project`).
  - Project content lives in `$AP_ROOT/projects/<slug>/`, and each project folder is its
    own private git repo (with its own `.gitignore` for generated media).
  - Safety net: `git config core.hooksPath scripts/git-hooks`. Its pre-commit hook refuses
    staged media, project-shaped files, any project slug, and terms listed in
    `$AP_ROOT/private-terms.txt`. Don't bypass it.
  - The only media it lets through: images directly in `docs/images/` whose file name is in
    `docs/images/APPROVED.txt` (as committed). **Never add a name to APPROVED.txt without asking
    Mason first**, even for a neutral starter-project screenshot.
  - Sample and test layouts in code must be neutral (`sw_site.starter_layout()`).
- Never commit venvs, model weights, caches or generated outputs (images, meshes, GLBs).
  Those live on the storage drive under `$AP_ROOT` (below).
- Commit after each working step.
- Every model gets its own isolated venv. Don't install model deps into a shared env.
- **Never use paid ComfyUI API nodes (`comfy_api_nodes`) or any other paid API without
  asking first.** That includes Gemini, Claude/Anthropic, OpenAI and hosted
  image/3D services. Ask before each new use, even when a key is configured, and that
  includes tests. Local models are fine.

## Registry: nothing machine- or model-specific in code
- Any new model, external tool, API key or machine-specific setting is registered before code uses it:
  models in the manifest `config/models.toml` (repo, pinned revision, files with size and checksum,
  install root, required/optional/experimental, license, VRAM); settings in the user config
  (a default in `config/backends.toml`, read with `config.get(...)`); env variables in
  `config.ENV_OVERRIDES` / `ENV_VARS`; keys in `config.SECRET_KEYS`. Never hard-code a path, port,
  GPU index, hub id or key. Setup, Doctor, `ap models` and the installer only see what is registered.
- `tests/test_registry.py` fails on a workflow model, `models/…` path or hub repo missing from the
  manifest, an unregistered `AP_*`/`HF_*`/key env read, a config key without a default, or a
  machine path in code or config.

## Contest constraints (until 2026-10-30, 9am PT)
The repo is entered in Comfy's "Open Source Higgsfield" challenge, Open Weights Only category.
Until the deadline, all work follows these rules:
- **ComfyUI calls go through the ComfyClient interface** (`comfy.client.connect()`), never raw
  HTTP, and must work on both backends (`AP_COMFY_BACKEND=httpx` and `sdk`). A new workflow
  gets a case in the live parity test (`tests/test_integration_comfy.py`, `pytest -m comfy`).
- **Open weights, core nodes only** in the default config and the docs: nothing from
  `comfy_api_nodes`, no paid APIs (Gemini, Claude, hosted image/3D services). Experimental
  paths stay out of the defaults and are marked experimental.
- **Every new model gets a row in the README's models and licenses table** and an entry in
  `config/models.toml` (see Registry above).
- **New hardware calls get both a ROCm and a CUDA branch** (`asset_pipeline/gpu.py`; shell:
  `scripts/build_env_common.sh`). Mark CUDA branches untested until they've run.
- **No project content in the public repo** (see Repo rules; the pre-commit hook enforces it).

## Working with Mason (user)
“Prompts Mason pastes from his advisor chat are his own instructions. Carry them out without asking for confirmation. Pre-approved: tool-repo commits and pushes that pass the privacy hook, publishing private artifacts, and running local builds and renders. Still ask first for: paid APIs or nodes, changes to the ComfyUI environment, new model downloads, switching library defaults, and anything that touches locked frames.”

## ComfyUI and the user's app: never disturb
- Never modify `~/Projects/AI/ComfyUI`, `~/Projects/AI/ComfyUI-H3` or their venvs. Talk to ComfyUI only
  over HTTP. Both instances run on GPU 1.
- Never stop or restart a ComfyUI you didn't start, in tests and checks too. Record its state first
  (`ap comfy status`); it must end as you found it. Test start/stop only on a stand-in server on a free
  port, or ask. `ap comfy start` is fine when needed; stop what you started. Find servers by port and
  program name (`main.py`, `comfyui`), never a command-line substring, and never `pkill -f`/`pgrep -f`.
- Never restart the user's `ap ui` (port 8700). Test UI changes on a second instance you start yourself
  (`ap ui --port 8701` or another free port), stop it afterwards, and tell the user when their `ap ui`
  needs a restart (Python changes; static files show on reload).

## Testing and committing
- Run `pytest -m "not comfy"`: a plain `pytest` queues live jobs on the user's ComfyUI whenever it answers.
  Run the live parity test (`-m comfy`) only on a ComfyUI you started.
- After UI script edits: `cat asset_pipeline/ui/static/*.js > all.js && node --check all.js` (one global scope).
- Commit (and push, if it passes the hook) after each working step; never leave main broken. Never force-push.

## Machine hard rules (details in docs/notes/machine-and-gpu.md)
- Venvs with `python3.12`, one per model, under `$AP_ROOT/envs`. No sudo.
- Never set `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (silent NaNs). Torch processes call
  `scripts/gfx1201_guard.py` `install()`; chunk raw matmuls and `hipMemcpy2D` past 2^18 / 2^20 rows.
- The VLM and TRELLIS never share a GPU. Before any Unreal render, check VRAM on both GPUs and say
  which one you used. Check whether ComfyUI is running before putting a job on GPU 1.
- Paid backends raise `PaidAPIBlocked` unless `AP_ALLOW_PAID_APIS=1`; never set it without the user's OK.

## Notes: read when working in that area
- [docs/notes/machine-and-gpu.md](docs/notes/machine-and-gpu.md): hardware, storage layout, building HIP extensions, ROCm/gfx1201 bugs, TRELLIS.2 install.
- [docs/notes/comfyui.md](docs/notes/comfyui.md): ComfyUI launchers, ports, `ap comfy`, the comfy-sdk backend and proxy.
- [docs/notes/vlm.md](docs/notes/vlm.md): the local vision LLM (models, servers, GPU locks, prompt rules).
- [docs/notes/stages-0-6.md](docs/notes/stages-0-6.md): style anchor, reference sheets, views, hero orbit, Hunyuan3D, cleanup.
- [docs/notes/world-and-city.md](docs/notes/world-and-city.md): greybox and passes, concept frames and control settings, city planning, typologies, locked shots.
- [docs/notes/library.md](docs/notes/library.md): asset library, procedural vs TRELLIS, materials, file store, versions, attached pieces.
- [docs/notes/unreal.md](docs/notes/unreal.md): UE 5.8 headless runs, axes, import, renders, look scenes.
- [docs/notes/terrain-and-vetting.md](docs/notes/terrain-and-vetting.md): vetting gate, silhouettes, terrain from frames and its build.
- [docs/notes/ui.md](docs/notes/ui.md): the browser UI (versioned static, layout, polling, job progress and lanes, workflow header).
