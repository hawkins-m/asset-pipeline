"""Gemini vision backend (Google Gemini API, paid). JSON mode with our schema; the
caller validates and retries (llm.base.structured)."""
from pathlib import Path

from ..paid import require_paid_allowed
from .base import LLMError, encode_image

DEFAULT_MODEL = "gemini-3.1-pro-preview"


def gemini_client():
    from google import genai
    return genai.Client()  # GEMINI_API_KEY / GOOGLE_API_KEY from the environment


class GeminiVision:
    name = "gemini"

    def __init__(self, model: str = "", client=None):
        self.model = model or DEFAULT_MODEL
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = gemini_client()
        return self._client

    def json_text(self, system: str, prompt: str, images: list[Path], schema: dict) -> str:
        require_paid_allowed("Gemini API")
        from google.genai import types
        parts = []
        for p in images:
            data, mime = encode_image(Path(p))
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        parts.append(prompt)
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            response_mime_type="application/json",
            response_json_schema=schema,
        )
        response = self.client.models.generate_content(model=self.model, contents=parts, config=config)
        text = getattr(response, "text", None)
        if not text:
            feedback = getattr(response, "prompt_feedback", None)
            raise LLMError(f"Gemini returned no text (prompt feedback: {feedback})")
        return text
