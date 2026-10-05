"""Claude vision backend (Anthropic API, paid). Schema-constrained via messages.parse."""
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from ..paid import require_paid_allowed
from .base import LLMError, LLMRefusal, b64, encode_image

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"


class ClaudeVision:
    name = "claude"

    def __init__(self, model: str = "", effort: str = "high", client=None):
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()  # ANTHROPIC_API_KEY or `ant auth login` profile
        return self._client

    def parse(self, system: str, prompt: str, images: list[Path], model: type[T]) -> T:
        require_paid_allowed("Claude API")
        content = []
        for p in images:
            data, mime = encode_image(Path(p))
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": b64(data)}})
        content.append({"type": "text", "text": prompt})
        kwargs = dict(
            model=self.model, max_tokens=16000,
            messages=[{"role": "user", "content": content}],
            output_format=model,
            output_config={"effort": self.effort},
            # If a safety classifier declines, the API re-runs on Anthropic's
            # recommended fallback model instead of returning a refusal.
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if system:
            kwargs["system"] = system
        response = self.client.beta.messages.parse(**kwargs)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            raise LLMRefusal(f"Claude declined the request "
                             f"(category: {getattr(details, 'category', None)})")
        if response.stop_reason == "max_tokens":
            raise LLMError("Claude hit max_tokens before finishing the JSON")
        parsed = response.parsed_output
        if parsed is None:
            raise LLMError("Claude returned no parsed output")
        return parsed

    def json_text(self, system: str, prompt: str, images: list[Path], schema: dict) -> str:
        raise NotImplementedError("ClaudeVision uses parse(); call llm.base.structured()")
