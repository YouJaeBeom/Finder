"""OpenAI (ChatGPT) LLM provider.

Selected with ``llm.provider: "openai"`` in config.yaml. Ranking and note
generation each take their own model, so the cheap/expensive split works the
same way it does on Anthropic.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import LLMProvider

logger = logging.getLogger(__name__)

# Current chat models take max_completion_tokens; max_tokens is the older name
# and is rejected outright by the reasoning models. Start with the current one
# and fall back once if the account's model only knows the old spelling.
_TOKEN_PARAM = "max_completion_tokens"
_LEGACY_TOKEN_PARAM = "max_tokens"


def _extract_text(response) -> str:
    """Pull the assistant's text out of a chat completion.

    Empty content is a real outcome, not an impossibility: a refusal populates
    ``message.refusal`` instead, and a reasoning model that spends its whole
    budget thinking returns nothing with ``finish_reason='length'``. Returning
    "" for either would surface downstream as an unexplained parse failure.
    """
    choice = response.choices[0]
    message = choice.message

    refusal = getattr(message, "refusal", None)
    if refusal:
        raise RuntimeError(f"The model declined this request: {refusal}")

    text = (message.content or "").strip()
    if not text:
        raise RuntimeError(
            f"No text in the model response (finish_reason="
            f"{getattr(choice, 'finish_reason', None)!r})"
        )

    if getattr(choice, "finish_reason", None) == "length":
        logger.warning(
            "Response hit the token limit and is truncated — raise max_tokens"
        )

    return text


class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions API client."""

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY must be set")
        import openai  # lazy import
        self._client = openai.OpenAI(api_key=api_key)
        self._token_param = _TOKEN_PARAM

    def _is_token_param_error(self, exc: Exception) -> bool:
        """True when the model rejected max_completion_tokens by name."""
        if self._token_param != _TOKEN_PARAM:
            return False  # already switched — a second failure is a real error
        message = str(exc).lower()
        return _TOKEN_PARAM in message and any(
            phrase in message
            for phrase in ("unsupported", "unrecognized", "not supported", "unknown")
        )

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
                    messages=messages,
                    **{self._token_param: max_tokens},
                )
                return _extract_text(response)
            except Exception as exc:
                if self._is_token_param_error(exc):
                    logger.info(
                        "Model %s wants the legacy %s parameter — switching",
                        model, _LEGACY_TOKEN_PARAM,
                    )
                    self._token_param = _LEGACY_TOKEN_PARAM
                    continue  # retry straight away; no backoff for a param swap
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
