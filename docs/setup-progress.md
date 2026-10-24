# Clean setup for a new user: progress

Goal: a new user sets Terraformer up from the README with nothing hard-coded to one machine.
Phases, one commit each; this file says where a stopped session picks up.

| Phase | What | State |
|---|---|---|
| 1 | Audit of machine-specific values (below) | done |
| 2 | One user config (`~/.config/terraformer/config.toml` + `secrets.toml`), env overrides, migrate the current machine | done |
| 3 | `ap setup` (CLI) and the first-run Setup screen, with a Test per item | done |
| 4 | `ap doctor`, the same results in the Status box, features disabled with a reason | done |
| 5 | Clean-install test with a fresh HOME, README Quick start fixed | todo |

Rules while doing this: test on a second `ap ui` (not port 8700) and a scratch AP_ROOT; no GPU jobs;
never touch a real project folder.

## Phase 1: audit

Today's config layers: env vars > `$AP_ROOT/backends.local.toml` > `config/backends.toml` (repo).
`AP_ROOT` itself can't come from the local toml (it lives inside AP_ROOT), so it falls back to a
hard-coded path. Secrets are only env vars; nothing reads a secrets file.

### Paths

| Value | Where it is used |
|---|---|
| `/mnt/storage/asset-pipeline` (AP_ROOT default) | `asset_pipeline/config.py:12` `DEFAULT_AP_ROOT`; `config/backends.toml [paths] ap_root`; `scripts/ap_root.sh:11`; `scripts/ap:6` (venv `…/envs/orchestrator`); `scripts/trellis2_image_to_glb.py:32`, `scripts/vlm_server.py:32`, `scripts/diag/seg_depth_dump.py:29`, `scripts/diag/style_anchor_ab.py:13` (diag, hard path) |
| Orchestrator venv `$AP_ROOT/envs/orchestrator` | `scripts/ap:6` (`AP_VENV` overrides), `scripts/trellis:34`; `launcher.py` writes the running venv into the desktop launcher |
| Model venvs `$AP_ROOT/envs/<name>` (trellis2, qwen-vl, llamacpp, segdepth, comfy-api-proxy) | install scripts; `scripts/trellis:45`; `scripts/llamacpp_vlm_server.sh:9`; `scripts/comfy_api_proxy.sh:9`; `asset_pipeline/vlm.py` (via config) |
| Model weights `$AP_ROOT/models/…` (TRELLIS.2-4B, Qwen3-VL GGUF/8B, seg/depth), `$AP_ROOT/hf` (HF_HOME), `$AP_ROOT/src/TRELLIS.2` | `config/backends.toml [local.models.*]`; `scripts/trellis2_image_to_glb.py:33`; `scripts/vlm_server.py:129`; `scripts/build_env_common.sh:9`; `scripts/trellis:44`; `preflight.local_checks` |
| ComfyUI model folder (`COMFYUI_MODELS`, docs say `~/ComfyUI/models`) | `scripts/fetch_models.sh` |
| Blender `/snap/bin/blender` | `$AP_ROOT/backends.local.toml [tools] blender`; repo default `blender` on PATH; `AP_BLENDER` |
| Unreal editor `/mnt/storage/UnrealEngine/5.8.3/Engine/Binaries/Linux/UnrealEditor-Cmd` | `config/backends.toml [unreal] editor_cmd`; `ue.editor_cmd()`; `AP_UE_EDITOR_CMD` |
| UE projects `~/Projects/Unreal` | `config/backends.toml [unreal] projects_dir`; `ue.py:62` default; `AP_UE_PROJECTS` |
| UE backups `/mnt/storage/backups/unreal` | `config/backends.toml [unreal] backup_dir`; `ue.py:66` default; `AP_UE_BACKUPS` |
| ComfyUI launcher `~/Projects/AI/ComfyUI/run_comfy.sh` | `$AP_ROOT/backends.local.toml [comfyui.service] launch` (repo default `python main.py` in `~/ComfyUI`) |
| `$AP_ROOT/local.env` (AP_DINOV3_REPO) | sourced by `scripts/trellis:14` |

### Addresses and ports

| Value | Where it is used |
|---|---|
| ComfyUI `http://127.0.0.1:8188` | `config/backends.toml [comfyui] url`; `scripts/comfy_api_proxy.sh:11` (`COMFYUI_URL`) |
| Second ComfyUI port 8189 (this machine only) | `look.py:479` (`comfy_running` checks 8188 and 8189); `scripts/trellis:38`; `scripts/comfy_api_proxy.sh:22` |
| Comfy API proxy `http://127.0.0.1:8190` | `config/backends.toml [comfyui] sdk_url`; `comfy/service.py:58` default; `AP_COMFY_SDK_URL` |
| Backend `sdk` (repo) vs `httpx` (this machine's local toml) | `[comfyui] backend`; `AP_COMFY_BACKEND` |
| VLM servers `127.0.0.1:8710` (8B) / `8711` (32B) | `config/backends.toml [local.models.*] url`; `llm/local.py:9`, `llm/llamacpp.py:15` defaults; `scripts/llamacpp_vlm_server.sh` |
| App port 8700 | `ap ui` default, `launcher.py DEFAULT_PORT` (a default, not machine-specific) |

### GPU assignment (GPU 0 = TRELLIS and vision model, GPU 1 = ComfyUI)

| Value | Where it is used |
|---|---|
| `[jobs] gpu0_gpu = 0`, `comfy_gpu = 1`, `[comfyui.service] gpu` ("1" here) | `gpu_policy.py:28-30` (job lanes, VRAM checks) |
| TRELLIS on GPU 0 | `scripts/trellis:27` (`TRELLIS_GPU`, default 0) |
| VLM on GPU 0 | `vlm.up(gpu=0)` default, `vlm.py` callers |
| Unreal: renders on Vulkan adapter 0, city renders on GPU 1 only while ComfyUI is down | `ue.py:234-358`; `ue_build.render_gpu` (hard 1); `look.py:34` `render_gpu: auto` |
| Lane labels "GPU 0" / "GPU 1 (ComfyUI)" | `estimates.py:78-80`, UI text |

### External services, keys and accounts

| Service | Setting / secret | Where |
|---|---|---|
| ComfyUI (local, open weights) | `[comfyui] url`, `backend`, `timeout_s` | `comfy/client.py`, `comfy/service.py` |
| Comfy SDK (comfy-sdk through the local `comfy-api-proxy`) | `sdk_url`; no key for a local proxy. `COMFY_API_KEY` only for Comfy Cloud (sdk_url unset), a paid service, not used | `comfy/sdk_client.py:33-46` (sets `COMFY_BASE_URL` per call); `scripts/comfy_api_proxy.sh` |
| Gemini (paid, off by default) | `GEMINI_API_KEY` / `GOOGLE_API_KEY` env, `AP_ALLOW_PAID_APIS=1` | `llm/gemini.py:13`, `imagegen/gemini*` |
| Claude / Anthropic (paid, off by default) | `ANTHROPIC_API_KEY` env or `ant auth login` profile | `llm/claude.py:27` |
| Hugging Face (gated downloads: FLUX Redux, DINOv3) | `hf auth login` token in `~/.cache/huggingface` (HF_HOME stays default for installs) | `scripts/fetch_models.sh`, `install_*.sh` |
| DINOv3 mirror | `AP_DINOV3_REPO` in `$AP_ROOT/local.env` | `scripts/trellis` |

### Docs that name this machine

`USAGE.md` (lines 8-28, 513-532, 798-934: `/mnt/storage/...`, `~/Projects/Unreal`), `PLAN.md:24-25,61`
(ports 8188/8189), `AGENTS.md:20` and `CLAUDE.md:51` (`~/Projects/AI/ComfyUI*`: agent rules for this
machine, kept), `docs/notes/*` (machine notes, kept by design), `docs/ue-build-progress.md:46`.

### Phase 2 plan (done; notes below)

- New `~/.config/terraformer/config.toml` (XDG_CONFIG_HOME respected; `AP_CONFIG` overrides the
  path) holding `[paths] ap_root`, `[tools] blender`, `[comfyui]`, `[unreal]`, `[jobs]` GPUs; and
  `secrets.toml` (mode 0600) beside it for API keys. Order: env > user config > (legacy)
  `$AP_ROOT/backends.local.toml` > repo `config/backends.toml` (defaults only, no machine paths).
- The repo defaults lose `/mnt/storage/...`: AP_ROOT defaults to `~/.local/share/terraformer`;
  `editor_cmd` empty (Unreal optional); UE backups under `$AP_ROOT/backups/unreal`.
- Migration: on first load with no user config, write this machine's effective values into it.
- Shell scripts get AP_ROOT from `ap config get paths.ap_root` (python3 fallback reads the user config).

## Phase 2: done

- `asset_pipeline/config.py`: `config_dir()` (`AP_CONFIG_DIR` > `$XDG_CONFIG_HOME/terraformer`),
  `user()`, `save_user()`, `get("section.key")`, `secrets()` / `save_secret()` / `load_secrets()`
  (0600 file, 0700 folder; a group/world-readable file is refused), `migrate(old_root, old_repo_values)`,
  `ENV_OVERRIDES` (merged last in `backends()`). AP_ROOT: env > user > repo (old checkouts) >
  `~/.local/share/terraformer`. `cli.main` loads the secrets first, so `ap ui` has them too.
- `ap config path|show|get|set|unset|migrate`. `scripts/ap_config.py KEY` gives shell scripts the same
  values (system python3); `scripts/ap`, `ap_root.sh`, `trellis`, `comfy_api_proxy.sh` use it.
- Repo `config/backends.toml`: no machine values. New keys: `[paths] venv`, `[comfyui] other_ports`.
  UE defaults: projects `~/Documents/Unreal Projects`, backups `$AP_ROOT/backups/unreal`.
- GPU numbers come from `[jobs] gpu0_gpu` / `comfy_gpu` in `vlm.up`, `ap vlm`, `scripts/trellis`,
  `ue_build.render_gpu`; `look.comfy_ports()` replaces the hard 8188/8189.
- This machine: `~/.config/terraformer/config.toml` written by `ap config migrate` with the old repo
  values + `$AP_ROOT/backends.local.toml` + `paths.venv`, `other_ports = [8189]`, GPUs 0/1. Effective
  config compared old code vs new: identical apart from the two new keys.
- `tests/conftest.py` gives every test an empty user config folder.
- Pre-commit hook refuses staged `secrets.toml`, `.env`, `local.env`, `backends.local.toml`.

## Phase 3: done

- `asset_pipeline/setup.py`: `ITEMS` (storage, backend, ComfyUI address, proxy address, launch
  command and folder, Comfy Cloud key, Gemini/Anthropic keys only when a default backend is paid,
  Blender, Unreal + its projects folder, the two GPU roles), `detect()` (ComfyUI on 8188/8000,
  Blender on PATH/snap/opt/home, Unreal under common install folders, GPUs via `gpu.list_gpus()`),
  `test(id, value, values)` (one plain sentence; never a GPU job or a paid call; Blender runs
  `--version` only; Unreal reads Build.version, never starts), `save()`, `first_run()`.
- A value still at its repo default yields to a different detected one (e.g. `blender` -> the
  snap's full path). Launch settings that fail only warn while ComfyUI already answers.
- The sdk Test is end to end: proxy installed, `/api/v2/health`, then `SdkComfyClient.health()`
  (ComfyUI through the proxy). Verified against a real proxy on a spare port (8195) in front
  of the running ComfyUI.
- `ap setup [--check] [--yes] [--set ITEM=VALUE]`; UI: `ui/setup_api.py`, `static/setup.js`
  (Tools › Setup…, opens by itself when there is no user config). Keys are sent to the server
  only to test or save, never sent back.
- `tests/test_setup.py`; `tests/test_ui_layout.py` writes a user config for its server (else the
  first-run window covers the page) and finds the browser env through the config.

## Phase 4: done

- `asset_pipeline/doctor.py`: checks settings, storage, GPUs (roles exist), the Comfy proxy (sdk),
  ComfyUI through the configured backend, its model files (`/object_info`), Blender, Unreal
  (optional; full editor next to UnrealEditor-Cmd), vision LLM, seg/depth, TRELLIS (optional), keys
  for paid defaults. Each check names the features it switches off (`FEATURES`: comfy, blender,
  unreal, vlm, segdepth, trellis). ComfyUI or the proxy merely not running is a note while Start
  ComfyUI can launch them, not "missing".
- `ap doctor [--json]`: exit 1 when a required piece is missing (optional ones don't count).
- App: `GET /api/doctor` (lazy: the first call starts a background run; cached 5 min; `refresh=1`;
  re-run after a Setup save). `static/doctor.js`: Status box line ("Setup: all set / N missing",
  Details…), Tools › Doctor…, `ACTION_NEEDS` wraps menu actions (disabled + tooltip),
  `data-needs="…"` on tab buttons kept off by a MutationObserver across re-renders, the
  `#unreal-missing` note at the top of the Unreal tab. Tagged: Unreal steps, Export (blender),
  Make 3D (blender or trellis), Brief & build (vlm blender), greybox/passes/frames actions.
- Status bar "GPU n" follows `[jobs] comfy_gpu`.
- Verified on a second instance (8702) with a fresh config and empty storage: the Unreal tab note,
  buttons off with reasons through polling redraws, Doctor dialog; sdk through a real proxy (8195).
