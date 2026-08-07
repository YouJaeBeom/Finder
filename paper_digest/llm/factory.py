"""Factory function to instantiate the configured LLM provider."""
from __future__ import annotations

from ..config import Config
from .base import LLMProvider


def create_provider(cfg: Config) -> LLMProvider:
    """Return an LLM provider instance based on *cfg.llm.provider*."""
    provider = cfg.llm.provider.lower()

    if provider == "anthropic":
        from .anthropic_client import AnthropicProvider
        return AnthropicProvider(api_key=cfg.anthropic_api_key)

    if provider == "openai":
        from .openai_client import OpenAIProvider
        return OpenAIProvider(api_key=cfg.openai_api_key)

    raise ValueError(
        f"Unknown LLM provider '{provider}'. Supported: 'anthropic', 'openai'."
    )
