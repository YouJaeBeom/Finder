"""Abstract base class for LLM providers."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class LLMProvider(ABC):
    """Common interface for all LLM providers."""

    @abstractmethod
    def complete(
        self,
        prompt: str,
        model: str,
        max_tokens: int = 4096,
        system: Optional[str] = None,
    ) -> str:
        """Send a prompt and return the text response."""
        ...
