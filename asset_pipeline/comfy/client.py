"""Thin HTTP client for a ComfyUI server (queue a graph, wait, fetch images)."""
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import jobs


class ComfyError(RuntimeError):
    def __init__(self, msg: str, messages: list | None = None):
        super().__init__(msg)
        self.messages = messages or []  # ComfyUI's status messages, when execution failed

    def execution_error(self) -> dict:
        """The 'execution_error' payload (node_type, exception_type, ...), or {}."""
        return next((m[1] for m in self.messages if m and m[0] == "execution_error"), {})


class ComfyUnavailable(ComfyError):
    pass


@dataclass
class OutputImage:
    node: str
    filename: str
    data: bytes


class ComfyClient:
    def __init__(self, url: str, timeout_s: float = 900, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.client_id = uuid.uuid4().hex
        self.http = httpx.Client(base_url=self.url, timeout=60, transport=transport)

    def health(self) -> dict:
        try:
            r = self.http.get("/system_stats")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ComfyUnavailable(
                f"ComfyUI not reachable at {self.url} ({e}). Start it with "
                f"~/Projects/AI/ComfyUI/run_comfy.sh") from e
        return r.json()

    def upload_image(self, path: Path) -> str:
        """Upload into ComfyUI's input dir; returns the name to put in a LoadImage node."""
        with path.open("rb") as f:
            r = self.http.post("/upload/image", files={"image": (path.name, f, "image/png")},
                               data={"type": "input", "overwrite": "true"})
        r.raise_for_status()
        d = r.json()
        return f"{d['subfolder']}/{d['name']}" if d.get("subfolder") else d["name"]

    def queue(self, graph: dict) -> str:
        jobs.check_canceled()
        r = self.http.post("/prompt", json={"prompt": graph, "client_id": self.client_id})
        if r.status_code != 200:
            try:
                detail = r.json()
            except ValueError:
                detail = r.text
            raise ComfyError(f"ComfyUI rejected the workflow: {detail}")
        prompt_id = r.json()["prompt_id"]
        if (job := jobs.current_job()):  # Stop in the UI removes or interrupts this prompt
            job.on_cancel(lambda: self.cancel_prompt(prompt_id))
        return prompt_id

    def cancel_prompt(self, prompt_id: str) -> str:
        """Delete `prompt_id` if it's waiting in ComfyUI's queue, interrupt it if it's the
        one running; other clients' prompts are never touched. Returns what was done."""
        q = self.http.get("/queue").json()
        if any(item[1] == prompt_id for item in q.get("queue_pending", [])):
            self.http.post("/queue", json={"delete": [prompt_id]}).raise_for_status()
            return "deleted"
        if any(item[1] == prompt_id for item in q.get("queue_running", [])):
            self.http.post("/interrupt", json={"prompt_id": prompt_id}).raise_for_status()
            return "interrupted"
        return "finished"

    def wait(self, prompt_id: str, poll_s: float = 1.0) -> dict:
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            jobs.check_canceled()
            r = self.http.get(f"/history/{prompt_id}")
            r.raise_for_status()
            entry = r.json().get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise ComfyError(f"ComfyUI execution failed: {status.get('messages')}",
                                     messages=status.get("messages"))
                if status.get("completed", True):
                    return entry
            time.sleep(poll_s)
        raise ComfyError(f"timed out after {self.timeout_s}s waiting for prompt {prompt_id}")

    def fetch_images(self, entry: dict, output_nodes: list[str]) -> list[OutputImage]:
        images = []
        for node in output_nodes:
            for img in entry.get("outputs", {}).get(node, {}).get("images", []):
                r = self.http.get("/view", params={"filename": img["filename"],
                                                   "subfolder": img.get("subfolder", ""),
                                                   "type": img.get("type", "output")})
                r.raise_for_status()
                images.append(OutputImage(node, img["filename"], r.content))
        if not images:
            raise ComfyError(f"workflow finished but produced no images on nodes {output_nodes}")
        return images

    def fetch_files(self, entry: dict, output_nodes: list[str]) -> list[OutputImage]:
        """Every file an output node reported, whatever its kind ("images", "3d", ...)."""
        files = []
        for node in output_nodes:
            for kind, items in entry.get("outputs", {}).get(node, {}).items():
                for f in items if isinstance(items, list) else []:
                    if isinstance(f, dict) and f.get("filename"):
                        r = self.http.get("/view", params={"filename": f["filename"],
                                                           "subfolder": f.get("subfolder", ""),
                                                           "type": f.get("type", "output")})
                        r.raise_for_status()
                        files.append(OutputImage(node, f["filename"], r.content))
        if not files:
            raise ComfyError(f"workflow finished but produced no files on nodes {output_nodes}: "
                             f"{entry.get('outputs')}")
        return files

    def run_files(self, graph: dict, output_nodes: list[str]) -> list[OutputImage]:
        return self.fetch_files(self.wait(self.queue(graph)), output_nodes)

    def run(self, graph: dict, output_nodes: list[str]) -> list[OutputImage]:
        return self.fetch_images(self.wait(self.queue(graph)), output_nodes)
