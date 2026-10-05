"""Local vision-LLM backend: Qwen3-VL served by scripts/vlm_server.py on GPU 0 (free).
Start it with `ap vlm up`. The caller validates and retries (llm.base.structured)."""
from pathlib import Path

import httpx

from .base import LLMError, b64, encode_image

DEFAULT_URL = "http://127.0.0.1:8710"


class LocalVision:
    name = "local"

    def __init__(self, url: str = "", timeout_s: float = 900, transport=None):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.http = httpx.Client(base_url=self.url, timeout=timeout_s, transport=transport)

    def health(self) -> dict:
        try:
            r = self.http.get("/health")
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            raise LLMError(f"local VLM server not reachable at {self.url} ({e}). "
                           f"Start it with `ap vlm up`.") from e

    def json_text(self, system: str, prompt: str, images: list[Path], schema: dict) -> str:
        imgs = []
        for p in images:
            data, mime = encode_image(Path(p))
            imgs.append({"mime": mime, "data": b64(data)})
        try:
            r = self.http.post("/v1/json", json={"system": system, "prompt": prompt,
                                                 "images": imgs, "schema": schema})
        except httpx.HTTPError as e:
            raise LLMError(f"local VLM server not reachable at {self.url} ({e}). "
                           f"Start it with `ap vlm up`.") from e
        if r.status_code != 200:
            raise LLMError(f"local VLM server error {r.status_code}: {r.text[:500]}")
        return r.json()["text"]
