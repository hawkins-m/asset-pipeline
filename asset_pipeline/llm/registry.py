"""Pick the vision LLM for a project (project.llm: "local" | "gemini" | "claude")."""
from .. import config
from ..schema import Project
from .base import VisionLLM

PROVIDERS = ("local", "gemini", "claude")


def make_llm(name: str) -> VisionLLM:
    cfg = config.backends()
    if name == "local":  # whichever local model is configured; started on first use
        from ..vlm import AutoLocal
        return AutoLocal()
    if name == "gemini":
        from .gemini import GeminiVision
        return GeminiVision(cfg.get("gemini", {}).get("vision_model", ""))
    if name == "claude":
        from .claude import ClaudeVision
        c = cfg.get("claude", {})
        return ClaudeVision(c.get("vision_model", ""), effort=c.get("effort", "high"))
    raise ValueError(f"unknown LLM provider {name!r} (expected one of {', '.join(PROVIDERS)})")


def llm_for(project: Project | None = None) -> VisionLLM:
    default = config.backends().get("defaults", {}).get("llm", "local")
    return make_llm((project.llm if project else None) or default)
