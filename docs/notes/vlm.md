# Vlm

The local vision LLM (stage 1): models, servers, GPU sharing, plan-quality findings. Moved from CLAUDE.md on 2026-10-21; rules here still apply when you work in this area.

## Vision LLM (stage 1)
- Providers per project (`ap set PROJECT --llm local|gemini|claude`). Default `local`:
  Qwen3-VL-8B-Instruct, bf16, env `$AP_ROOT/envs/qwen-vl` (torch 2.13.0+rocm7.2,
  transformers 5.18), weights `$AP_ROOT/models/Qwen3-VL-8B-Instruct`. Install with
  `scripts/install_qwen_vl.sh`.
- **Default local VLM is Qwen3-VL-32B Q5_K_M (llama.cpp, :8711); the 8B (:8710) is the
  fallback** (config `[local]`). `asset_pipeline/vlm.py` manages it: started on demand by
  stage 1, state in `$AP_ROOT/logs/vlm.json`, idle unload after 600 s for both.
- **VLM and TRELLIS never share a GPU.** `scripts/trellis` writes
  `logs/trellis-gpu<N>-<pid>.lock`, then `ap vlm down --gpu N` (waits for a request in
  flight); `vlm.up()` refuses a GPU with a live lock. Verified live 2026-10-05.
- Verified 2026-10-05: structured scene analysis on a test project validates first try,
  ~25 s warm (1.4k tokens in, ~560 out, ~23 tok/s).
- Stage 1 plans (verified 2026-10-05 on a test project): both scenes validated first try,
  45–70 s warm. Without the explicit "parts are not assets" and "kit only when ≥2 pieces
  share it" rules, Qwen split stalls into poles/awnings/shingles (23 assets, 173 s) and
  gave every asset its own kit. Its `bbox_2d` boxes are rough (some tight, many offset or
  oversized); refine with SAM 3.1 before relying on them.
- Larger local VLMs (verified 2026-10-05): llama.cpp's prebuilt **Vulkan** build (RADV,
  `scripts/install_llamacpp_vlm.sh`, b11433) runs Qwen3-VL GGUFs on gfx1201; Vulkan0 =
  GPU 0. Serve with `scripts/llamacpp_vlm_server.sh`, backend `llm/llamacpp.py`.
  - 32B dense fits one card only at ~5 bit: Q5_K_M peaks 26.8 GB on GPU 0 at 8k ctx
    (Q8_0 34.8 GB and FP8 35.5 GB don't fit). 24 tok/s, ~110 s per scene.
  - 30B-A3B Q6_K: 27.4 GB, ~130 tok/s, but loops at greedy decoding (repeats assets
    until max_tokens) and still duplicates heavily with Qwen's recommended sampling.
  - 8B Q8_0 on llama.cpp: 65 tok/s (3x transformers) but worse plans than 8B bf16.
  - A/B on 4 test scenes, blind-judged against a reference written first
    (`$AP_ROOT/outputs/vlm_ab/`, `scripts/diag/vlm_ab*.py`): 32B Q5 greedy 18/20,
    8B bf16 13/20, 8B Q8 9.5/20, 30B-A3B <=8.5/20. LLM box hits SAM object (IoU>=0.3):
    32B 79%, 8B bf16 36%.
  - The 32B listed a dominant mountain (1500x2000x3000 m). Fixed by a "Do NOT list"
    prompt block (parts, landscape/backdrop, people) plus dropping any asset > 200 m
    without a retry (`plan.dropped`). Part violations on the 4 scenes: 10 -> 3.
- SAM 3.1 finding nothing for a noun fails the ComfyUI graph (empty batch -> IndexError
  in the image output node). `segment.detect` treats that as no detections.
- Paid backends (Gemini, Claude, Gemini images) raise `PaidAPIBlocked` unless
  `AP_ALLOW_PAID_APIS=1` is set for that run. Never set it yourself without the user's OK.
  Their tests use mocked SDKs only.
