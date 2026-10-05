"""Pick the image backend for a stage from the project's settings."""
from .. import config
from ..comfy.client import ComfyClient
from ..schema import Project
from .base import ImageGenBackend


def make_backend(name: str) -> ImageGenBackend:
    cfg = config.backends()
    if name == "comfyui":
        from .comfyui import ComfyUIBackend
        c = cfg["comfyui"]
        return ComfyUIBackend(ComfyClient(c["url"], c.get("timeout_s", 900)), c["workflows"])
    if name == "gemini":
        from .gemini import GeminiImageBackend
        return GeminiImageBackend(cfg.get("gemini", {}).get("image_model", ""))
    raise ValueError(f"unknown image backend {name!r} (expected 'comfyui' or 'gemini')")


def backend_for(stage: str, project: Project | None = None) -> ImageGenBackend:
    """Backend configured for `stage` in the project, else the repo default."""
    defaults = config.backends().get("defaults", {}).get("backends", {})
    name = (project.backends if project else {}).get(stage) or defaults.get(stage, "comfyui")
    return make_backend(name)
