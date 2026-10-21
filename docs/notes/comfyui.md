# Comfyui

The ComfyUI installs, launchers and service commands, and the comfy-sdk backend. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

### ComfyUI: do not touch
- Installs: `~/Projects/AI/ComfyUI` and `~/Projects/AI/ComfyUI-H3`.
- Each has its own venv:
  - Main (`run_comfy.sh`, port 8188): `~/Projects/AI/ComfyUI/venv`, torch 2.9.1+rocm7.2.1.
  - H3 (`run_comfy_h3.sh`, port 8189): `~/Projects/AI/ComfyUI-H3/venv`, torch 2.13.0+rocm7.2.
  - `~/pytorch_env` (torch 2.12) is a separate env, not ComfyUI's.
- **Both instances are pinned to GPU 1** (`HIP_VISIBLE_DEVICES=1` in the launchers). GPU 0
  carries the desktop and is where TRELLIS and other local 3D work run.
- **Never modify these directories or that env.** Talk to ComfyUI only over HTTP.
- **Never stop or restart a ComfyUI you didn't start yourself, in tests and checks too.**
  Record its state first (`ap comfy status`). If it was running before a test or check, it must
  still be running afterwards, untouched: test start/stop only on a stand-in server on a free
  port (as `tests/test_comfy_service.py` does), or ask first. If you started it yourself for a
  check, stop it afterwards, so it ends as you found it. (2026-10-17: a browser check stopped
  the user's running ComfyUI and broke their session.)
- Starting the main instance is OK whenever it's needed: `ap comfy start` (launches
  `run_comfy.sh` per `[comfyui.service]` in `$AP_ROOT/backends.local.toml`, detached, logs to
  `$AP_ROOT/logs/comfyui.log`); `ap comfy status`, `ap comfy stop`. Verified live 2026-10-17:
  answers in ~5 s; stop ends the launcher's process group in <1 s; a hand-started instance is
  found by its port; the sdk backend's proxy comes up and goes down with it.
- Stopping a server found by its port checks the program name (`main.py`, `comfyui`), never a
  substring of the command line: a "comfy" substring match made the first stop test kill
  pytest itself (`tests/test_comfy_service.py` holds the test socket).
- `run_comfy.sh` sets `PYTORCH_HIP_ALLOC_CONF=expandable_segments:True`, but ComfyUI's
  torch logs "expandable_segments not supported on this platform". So it's a no-op
  there, unlike the NaN problem it caused in the TRELLIS env.

## Comfy SDK backend (verified 2026-10-15)
- `AP_COMFY_BACKEND=httpx|sdk` (else `[comfyui] backend`; repo default `sdk`). This machine keeps
  `httpx` in `$AP_ROOT/backends.local.toml`, which is merged over `config/backends.toml`.
- `sdk` = comfy-sdk 0.5.0 through comfy-api-proxy 0.1.6 (`scripts/comfy_api_proxy.sh`, own venv
  `$AP_ROOT/envs/comfy-api-proxy`, port 8190: its default 8189 is the H3 instance here).
- The proxy forwards `Job.cancel()` to `POST /api/jobs/{id}/cancel`, which ComfyUI 0.24 lacks:
  the job keeps running. `SdkComfyClient.cancel_prompt` then does the /queue delete or
  /interrupt itself (verified live: interrupted, job canceled 3 s later).
- The v2 job error has the node class and message but no exception type; SAM's "found nothing"
  arrives as `PreviewImage` + "index 0 is out of bounds ... size 0" (verified live).
- PreviewImage (temp) outputs come back through the proxy; "3d" outputs (SaveGLB) don't, so
  Hunyuan3D needs httpx.
