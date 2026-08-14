"""Anthropic (Claude) LLM provider."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import LLMProvider

logger = logging.getLogger(__name__)


def _extract_text(response) -> str:
    """Pull the assistant's text out of a Messages API response.

    Not simply ``content[0].text``. Current Claude models think by default, so
    the first block is often a thinking block, which has no ``.text`` at all —
    indexing blindly raises AttributeError on a perfectly good response. Scan
    for text blocks instead and join them.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        raise RuntimeError(
            "The model declined this request (stop_reason=refusal). "
            "Its content is empty or partial and cannot be used."
        )

    parts = [
        block.text
        for block in (response.content or [])
        if getattr(block, "type", None) == "text"
    ]
    text = "".join(parts).strip()

    if not text:
        raise RuntimeError(
            f"No text in the model response (stop_reason="
            f"{getattr(response, 'stop_reason', None)!r}, "
            f"blocks={[getattr(b, 'type', '?') for b in (response.content or [])]})"
        )

    if getattr(response, "stop_reason", None) == "max_tokens":
        # Thinking and the answer share max_tokens, so a budget sized for the
        # answer alone truncates mid-JSON and the note parser silently falls
        # back. Say so rather than shipping a half-written note.
        logger.warning(
            "Response hit max_tokens and is truncated — raise max_tokens for this call"
        )

    return text


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
                return _extract_text(response)
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
