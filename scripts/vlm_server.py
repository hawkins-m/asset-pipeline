"""Local vision-LLM server: Qwen3-VL on one GPU, JSON over HTTP (stdlib only).

Runs in its own venv ($AP_ROOT/envs/qwen-vl, see scripts/install_qwen_vl.sh); the
orchestrator talks to it via asset_pipeline/llm/local.py. Start with `ap vlm up`.

    GET  /health   -> {"model", "loaded", "device", "idle_unload_s"}
    POST /v1/json  {"system", "prompt", "images": [{"mime","data"(b64)}], "schema"}
                   -> {"text": "<model output>"}

The model is loaded on first request and unloaded after --idle-unload seconds without
requests, so GPU 0 is free for TRELLIS in between.
"""
import argparse
import base64
import io
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gfx1201_guard  # noqa: E402

import torch  # noqa: E402
from PIL import Image  # noqa: E402

gfx1201_guard.install()  # tall GEMMs silently corrupt on gfx1201 (CLAUDE.md)

AP_ROOT = Path(os.environ.get("AP_ROOT", "/mnt/storage/asset-pipeline"))
JSON_INSTRUCTION = ("Answer with only a single JSON object, no prose and no code fences. "
                    "It must validate against this JSON Schema:\n{schema}")


class Model:
    def __init__(self, path: str, idle_unload_s: float):
        self.path = path
        self.idle_unload_s = idle_unload_s
        self.lock = threading.Lock()
        self.model = self.processor = None
        self.last_used = time.time()
        threading.Thread(target=self._reaper, daemon=True).start()

    def _load(self):
        if self.model is None:
            from transformers import AutoModelForImageTextToText, AutoProcessor
            t = time.time()
            self.processor = AutoProcessor.from_pretrained(self.path)
            self.model = AutoModelForImageTextToText.from_pretrained(
                self.path, dtype=torch.bfloat16, device_map="cuda").eval()
            print(f"loaded {self.path} in {time.time() - t:.0f}s", flush=True)

    def _reaper(self):
        while True:
            time.sleep(15)
            if self.idle_unload_s > 0 and self.model is not None \
                    and time.time() - self.last_used > self.idle_unload_s:
                with self.lock:
                    if time.time() - self.last_used > self.idle_unload_s:
                        self.model = self.processor = None
                        torch.cuda.empty_cache()
                        print("unloaded after idle", flush=True)

    def generate(self, system: str, prompt: str, images: list[Image.Image], schema: dict) -> str:
        with self.lock:
            self._load()
            sys_text = "\n\n".join(t for t in [system, JSON_INSTRUCTION.format(
                schema=json.dumps(schema))] if t)
            content = [{"type": "image", "image": im} for im in images]
            content.append({"type": "text", "text": prompt})
            messages = [{"role": "system", "content": [{"type": "text", "text": sys_text}]},
                        {"role": "user", "content": content}]
            inputs = self.processor.apply_chat_template(
                messages, tokenize=True, add_generation_prompt=True,
                return_dict=True, return_tensors="pt").to(self.model.device)
            with torch.inference_mode():
                out = self.model.generate(**inputs, max_new_tokens=4096, do_sample=False)
            n_in = inputs["input_ids"].shape[1]
            text = self.processor.batch_decode(out[:, n_in:], skip_special_tokens=True)[0]
            self.last_used = time.time()
            self.last_tokens = (n_in, out.shape[1] - n_in)
            return text


def make_handler(model: Model):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path != "/health":
                return self._send(404, {"error": "not found"})
            self._send(200, {"model": model.path, "loaded": model.model is not None,
                             "device": torch.cuda.get_device_name(0),
                             "idle_unload_s": model.idle_unload_s})

        def do_POST(self):
            if self.path != "/v1/json":
                return self._send(404, {"error": "not found"})
            try:
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                images = [Image.open(io.BytesIO(base64.b64decode(i["data"]))).convert("RGB")
                          for i in req.get("images", [])]
                t = time.time()
                text = model.generate(req.get("system", ""), req["prompt"], images, req["schema"])
                dt = time.time() - t
                n_in, n_out = model.last_tokens
                print(f"/v1/json {len(images)} image(s) {dt:.1f}s, {n_in} tokens in, "
                      f"{n_out} out ({n_out / dt:.1f} tok/s overall)", flush=True)
                self._send(200, {"text": text})
            except Exception as e:  # report to the client, keep serving
                self._send(500, {"error": f"{type(e).__name__}: {e}"})

        def log_message(self, *args):
            pass
    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(AP_ROOT / "models" / "Qwen3-VL-8B-Instruct"))
    ap.add_argument("--port", type=int, default=8710)
    ap.add_argument("--idle-unload", type=float, default=600, help="seconds; 0 = never")
    a = ap.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(Model(a.model, a.idle_unload)))
    print(f"vlm server on 127.0.0.1:{a.port} ({a.model}, {torch.cuda.get_device_name(0)})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
