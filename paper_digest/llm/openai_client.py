"""OpenAI LLM provider."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import LLMProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API client."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY must be set")
        import openai  # lazy import
        self._client = openai.OpenAI(api_key=api_key)

    def complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 4096,
        system: Optional[str] = None,
    ) -> str:
        """Call the OpenAI Chat Completions API and return the text response."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(3):
            try:
                response = self._client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,
                )
                return response.choices[0].message.content or ""
            except Exception as exc:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "OpenAI API error (attempt %d/3): %s — retrying in %ds",
                        attempt + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        return ""  # unreachable
