# Clean setup for a new user: progress

Goal: a new user sets Terraformer up from the README with nothing hard-coded to one machine.
Phases, one commit each; this file says where a stopped session picks up.

| Phase | What | State |
|---|---|---|
| 1 | Audit of machine-specific values (below) | done |
| 2 | One user config (`~/.config/terraformer/config.toml` + `secrets.toml`), env overrides, migrate the current machine | todo |
| 3 | `ap setup` (CLI) and the first-run Setup screen, with a Test per item | todo |
| 4 | `ap doctor`, the same results in the Status box, features disabled with a reason | todo |
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

### Plan for phase 2

- New `~/.config/terraformer/config.toml` (XDG_CONFIG_HOME respected; `AP_CONFIG` overrides the
  path) holding `[paths] ap_root`, `[tools] blender`, `[comfyui]`, `[unreal]`, `[jobs]` GPUs; and
  `secrets.toml` (mode 0600) beside it for API keys. Order: env > user config > (legacy)
  `$AP_ROOT/backends.local.toml` > repo `config/backends.toml` (defaults only, no machine paths).
- The repo defaults lose `/mnt/storage/...`: AP_ROOT defaults to `~/.local/share/terraformer`;
  `editor_cmd` empty (Unreal optional); UE backups under `$AP_ROOT/backups/unreal`.
- Migration: on first load with no user config, write this machine's effective values into it.
- Shell scripts get AP_ROOT from `ap config get paths.ap_root` (python3 fallback reads the user config).
