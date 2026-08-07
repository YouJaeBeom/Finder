"""Anthropic (Claude) LLM provider."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import LLMProvider

logger = logging.getLogger(__name__)


class AnthropicProvider(LLMProvider):
    """Anthropic Messages API client."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY must be set")
        import anthropic  # lazy import to keep startup fast
        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 4096,
        system: Optional[str] = None,
    ) -> str:
        """Call the Anthropic Messages API and return the text response."""
        messages = [{"role": "user", "content": prompt}]
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        for attempt in range(3):
            try:
                response = self._client.messages.create(**kwargs)
                return response.content[0].text
            except Exception as exc:
                if attempt < 2:
                    wait = 2 ** (attempt + 1)
                    logger.warning(
                        "Anthropic API error (attempt %d/3): %s — retrying in %ds",
                        attempt + 1,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    raise
        return ""  # unreachable
