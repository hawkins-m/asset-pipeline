"""ComfyClient on the Comfy SDK (comfy-sdk, Comfy API v2).

Same interface as the httpx ComfyClient, so stages don't know which one they run on.
Against a local ComfyUI the v2 API comes from comfy-api-proxy, a separate service in its
own venv (see README); against Comfy Cloud the SDK talks to Cloud directly.

Where the SDK or the proxy can't do what the pipeline needs, this falls back to ComfyUI's
own HTTP API through the httpx base class (`self.http`, the ComfyUI URL):
- Cancel: the proxy forwards `Job.cancel()` to `POST /api/jobs/{id}/cancel`, which
  ComfyUI <= 0.24 doesn't have (the proxy ignores the 404 and the job keeps running). So
  cancel_prompt() asks the SDK first, then does the httpx /queue delete + /interrupt.
- Errors: the v2 job error carries the node's class and the exception message, not the
  exception type. execution_error() reports exception_type None (segment._no_detections
  matches on the message then).
- 3D outputs: the proxy only returns images, video, audio, text, files and latents, so a
  SaveGLB output (Hunyuan3D, experimental) needs AP_COMFY_BACKEND=httpx.
PreviewImage (temp) outputs come back through the proxy like saved ones.
"""
import os
import threading
import time
from pathlib import Path

import httpx

from .. import jobs
from .client import ComfyClient, ComfyError, ComfyUnavailable, OutputImage

_env_lock = threading.Lock()


def _sdk_client(sdk_url: str | None, timeout: float):
    """comfy_sdk.Comfy for sdk_url (None: Comfy Cloud with COMFY_API_KEY). The SDK reads
    its base URL from COMFY_BASE_URL at construction, so set it for that moment only."""
    from comfy_sdk import Comfy
    with _env_lock:
        old = os.environ.get("COMFY_BASE_URL")
        try:
            if sdk_url:
                os.environ["COMFY_BASE_URL"] = sdk_url
            return Comfy(timeout=timeout, client_info="asset-pipeline")
        finally:
            if old is None:
                os.environ.pop("COMFY_BASE_URL", None)
            else:
                os.environ["COMFY_BASE_URL"] = old


class SdkComfyClient(ComfyClient):
    def __init__(self, url: str, sdk_url: str | None, timeout_s: float = 900,
                 transport: httpx.BaseTransport | None = None, sdk=None):
        super().__init__(url, timeout_s, transport)
        self.sdk_url = sdk_url.rstrip("/") if sdk_url else None
        self.sdk = sdk or _sdk_client(self.sdk_url, timeout=60)
        self._jobs = {}   # prompt id -> comfy_sdk Job

    def health(self) -> dict:
        stats = super().health()
        if self.sdk_url:
            try:
                r = self.http.get(f"{self.sdk_url}/api/v2/health")
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise ComfyUnavailable(
                    f"Comfy API proxy not reachable at {self.sdk_url} ({e}). Start it with "
                    f"scripts/comfy_api_proxy.sh, or set AP_COMFY_BACKEND=httpx") from e
        return stats

    def upload_image(self, path: Path):
        """Upload through the SDK; returns the core/ASSET reference to put in a LoadImage
        node (the proxy swaps it for the uploaded file's name at submit)."""
        from comfy_sdk.exceptions import ComfyError as SdkError
        try:
            return self.sdk.assets.from_file(path).as_reference()
        except SdkError as e:
            raise ComfyError(f"upload of {path.name} failed: {e}") from e

    def queue(self, graph: dict) -> str:
        from comfy_sdk.exceptions import ComfyError as SdkError
        jobs.check_canceled()
        try:
            job = self.sdk.submit(self.sdk.workflows.from_json(graph))
        except httpx.HTTPError as e:
            raise ComfyUnavailable(f"Comfy API not reachable at {self.sdk_url} ({e})") from e
        except SdkError as e:
            raise ComfyError(f"ComfyUI rejected the workflow: {e}") from e
        self._jobs[job.id] = job
        if (j := jobs.current_job()):  # Stop in the UI removes or interrupts this prompt
            j.on_cancel(lambda: self.cancel_prompt(job.id))
        return job.id

    def cancel_prompt(self, prompt_id: str) -> str:
        """SDK cancel first; then ComfyUI's /queue delete or /interrupt (httpx), since the
        SDK's cancel doesn't reach ComfyUI <= 0.24. Returns what the fallback did."""
        job = self._jobs.get(prompt_id)
        if job is not None:
            try:
                job.cancel()
            except Exception:   # noqa: BLE001 - the fallback below is what counts
                pass
        return super().cancel_prompt(prompt_id)

    def wait(self, prompt_id: str, poll_s: float = 1.0):
        """Poll until the job is terminal; returns the SDK Job (fetch_* read it)."""
        job = self._jobs.get(prompt_id) or self.sdk.jobs.get(prompt_id)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            jobs.check_canceled()
            job.refresh()
            if job.status == "succeeded":
                return job
            if job.status in ("failed", "canceled", "expired"):
                err = job.error
                detail = {"node_id": getattr(err, "node_id", None),
                          "node_type": getattr(err, "class_type", None),
                          "exception_type": None,   # the v2 error doesn't carry it
                          "exception_message": getattr(err, "message", "") or ""}
                messages = [["execution_error", detail]] if job.status == "failed" else \
                    [["execution_interrupted", {}]]
                raise ComfyError(f"ComfyUI execution failed: {job.status}: "
                                 f"{detail['node_type']}: {detail['exception_message']}",
                                 messages=messages)
            time.sleep(poll_s)
        raise ComfyError(f"timed out after {self.timeout_s}s waiting for prompt {prompt_id}")

    def _outputs(self, job, output_nodes: list[str], kinds: set[str] | None) -> list[OutputImage]:
        out = []
        for node in output_nodes:
            for o in job.get_outputs(node):
                if kinds is None or o.type in kinds:
                    out.append(OutputImage(node, o.name, o.to_bytes()))
        return out

    def fetch_images(self, entry, output_nodes: list[str]) -> list[OutputImage]:
        images = self._outputs(entry, output_nodes, {"image"})
        if not images:
            raise ComfyError(f"workflow finished but produced no images on nodes {output_nodes}")
        return images

    def fetch_files(self, entry, output_nodes: list[str]) -> list[OutputImage]:
        files = self._outputs(entry, output_nodes, None)
        if not files:
            raise ComfyError(f"workflow finished but produced no files on nodes {output_nodes} "
                             f"(the Comfy API proxy doesn't return 3D outputs: use "
                             f"AP_COMFY_BACKEND=httpx for those)")
        return files
