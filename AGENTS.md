# AGENTS.md

Guidance for coding agents other than Claude Code, for the repo
[`hawkins-m/terraformer-3d`](https://github.com/hawkins-m/terraformer-3d) (formerly `asset-pipeline`; the command is still `ap`, the
package still `asset_pipeline`). The rules live in
[CLAUDE.md](CLAUDE.md); read it in full before changing anything. The design source of truth
is [PLAN.md](PLAN.md); if the two disagree on design, PLAN.md wins.

Headline rules (CLAUDE.md has the details):

- **Public repo, private projects.** Never commit project content (briefs, layouts, shot
  lists, greyboxes, generated media, project names or brief terms). Use placeholders such as
  `my-project`. Enable the privacy hook with `git config core.hooksPath scripts/git-hooks`
  and never bypass it.
- **No paid APIs or ComfyUI API nodes** (`comfy_api_nodes`, Gemini, Claude, OpenAI, hosted
  services) without asking first, tests included. Local, open-weights models only.
- **Contest constraints** (until 2026-10-30): ComfyUI calls go through `comfy.client.connect()`
  and must work on both backends; core nodes only; every new model gets a README licenses row;
  new hardware calls get ROCm and CUDA branches.
- **ComfyUI is off limits.** Never modify `~/Projects/AI/ComfyUI*` or their venvs; talk to
  them over HTTP only.
- **GPU rules.** Pin jobs with `HIP_VISIBLE_DEVICES`; GPU 1 is ComfyUI's, GPU 0 runs the
  desktop, TRELLIS and the VLM (never both at once). Check VRAM before Unreal renders. Use
  `scripts/gfx1201_guard.py` for large GEMMs.
- One venv per model; no venvs, weights or generated outputs in the repo. Commit after each
  working step.
