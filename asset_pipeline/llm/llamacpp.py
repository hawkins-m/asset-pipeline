"""Local vision-LLM backend: a GGUF model behind llama.cpp's llama-server (free).

For models too big for one 32 GB card at bf16 (scripts/install_llamacpp_vlm.sh). Uses the
OpenAI-style chat endpoint with response_format=json_schema, so llama.cpp constrains
decoding to the schema; the caller still validates (llm.base.structured) because the
grammar can't express every pydantic constraint (e.g. gt=0).
"""
import json
from pathlib import Path

import httpx

from .base import LLMError, b64, encode_image

DEFAULT_URL = "http://127.0.0.1:8711"
# Same wording as scripts/vlm_server.py, so both local backends see the schema the same way.
JSON_INSTRUCTION = ("Answer with only a single JSON object, no prose and no code fences. "
                    "It must validate against this JSON Schema:\n{schema}")


class LlamaCppVision:
    name = "llamacpp"

    def __init__(self, url: str = "", timeout_s: float = 1800, max_tokens: int = 4096,
                 sampling: dict | None = None, transport=None):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.max_tokens = max_tokens
        self.sampling = sampling or {"temperature": 0}  # greedy unless told otherwise
        self.http = httpx.Client(base_url=self.url, timeout=timeout_s, transport=transport)
        self.last_usage: dict = {}

    def health(self) -> dict:
        try:
            r = self.http.get("/health")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"llama-server not reachable at {self.url} ({e})") from e

    def json_text(self, system: str, prompt: str, images: list[Path], schema: dict) -> str:
        sys_text = "\n\n".join(t for t in [system, JSON_INSTRUCTION.format(schema=json.dumps(schema))] if t)
        content = []
        for p in images:
            data, mime = encode_image(Path(p))
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64(data)}"}})
        content.append({"type": "text", "text": prompt})
        body = {"messages": [{"role": "system", "content": sys_text},
                             {"role": "user", "content": content}],
                **self.sampling, "max_tokens": self.max_tokens,
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "answer", "schema": schema}}}
        try:
            r = self.http.post("/v1/chat/completions", json=body)
        except httpx.HTTPError as e:
            raise LLMError(f"llama-server not reachable at {self.url} ({e})") from e
        if r.status_code != 200:
            raise LLMError(f"llama-server error {r.status_code}: {r.text[:500]}")
        out = r.json()
        self.last_usage = out.get("usage", {}) | {"timings": out.get("timings", {})}
        choice = out["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("llama-server hit max_tokens before finishing the JSON",
                           raw=choice["message"]["content"])
        return choice["message"]["content"]
