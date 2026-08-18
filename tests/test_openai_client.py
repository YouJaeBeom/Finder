"""Reading a response out of the OpenAI Chat Completions API.

The provider is selectable — ``llm.provider: "openai"`` — so it has to hold the
same contract as the Anthropic one: return text, or raise something that says
why. Both failure modes here return a *successful* HTTP response carrying no
text, which is the shape that surfaces downstream as an unexplained parse
failure if it is not caught at the boundary.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from paper_digest.config import Config, LLMConfig
from paper_digest.llm.factory import create_provider
from paper_digest.llm.openai_client import (
    _LEGACY_TOKEN_PARAM,
    _TOKEN_PARAM,
    OpenAIProvider,
    _extract_text,
)


def _response(content=None, *, refusal=None, finish_reason="stop") -> SimpleNamespace:
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )


class TestExtractText:
    def test_plain_text_comes_back(self):
        assert _extract_text(_response("hello")) == "hello"

    def test_surrounding_whitespace_is_stripped(self):
        assert _extract_text(_response("  hello\n")) == "hello"

    def test_a_refusal_says_it_was_refused(self):
        """Not a parse failure — the model answered, and the answer was no."""
        response = _response(None, refusal="I can't help with that")
        with pytest.raises(RuntimeError, match="declined this request"):
            _extract_text(response)

    def test_an_empty_response_names_the_finish_reason(self):
        """A reasoning model can spend its whole budget thinking."""
        with pytest.raises(RuntimeError, match="finish_reason='length'"):
            _extract_text(_response("", finish_reason="length"))

    def test_a_truncated_but_non_empty_response_is_returned_with_a_warning(self, caplog):
        with caplog.at_level("WARNING"):
            assert _extract_text(_response("partial", finish_reason="length")) == "partial"
        assert "truncated" in caplog.text


def _provider(monkeypatch, create) -> OpenAIProvider:
    """An OpenAIProvider whose chat.completions.create is *create*."""
    client = MagicMock()
    client.chat.completions.create = create
    fake_openai = SimpleNamespace(OpenAI=lambda api_key: client)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)
    return OpenAIProvider(api_key="key")


class TestProvider:
    def test_a_missing_key_fails_before_any_call(self):
        with pytest.raises(ValueError, match="OPENAI_API_KEY"):
            OpenAIProvider(api_key="")

    def test_the_system_prompt_becomes_the_first_message(self, monkeypatch):
        seen = {}

        def create(**kwargs):
            seen.update(kwargs)
            return _response("ok")

        provider = _provider(monkeypatch, create)
        assert provider.complete("질문", model="m", system="지시") == "ok"
        assert seen["messages"] == [
            {"role": "system", "content": "지시"},
            {"role": "user", "content": "질문"},
        ]

    def test_without_a_system_prompt_only_the_user_message_is_sent(self, monkeypatch):
        seen = {}

        def create(**kwargs):
            seen.update(kwargs)
            return _response("ok")

        _provider(monkeypatch, create).complete("질문", model="m")
        assert [m["role"] for m in seen["messages"]] == ["user"]

    def test_the_current_token_parameter_is_used_first(self, monkeypatch):
        seen = {}

        def create(**kwargs):
            seen.update(kwargs)
            return _response("ok")

        _provider(monkeypatch, create).complete("q", model="m", max_tokens=1234)
        assert seen[_TOKEN_PARAM] == 1234
        assert _LEGACY_TOKEN_PARAM not in seen

    def test_a_model_that_wants_the_legacy_parameter_is_retried_immediately(
            self, monkeypatch):
        """The older spelling, swapped in without spending a backoff."""
        calls = []

        def create(**kwargs):
            calls.append(dict(kwargs))
            if _TOKEN_PARAM in kwargs:
                raise RuntimeError(
                    f"Unsupported parameter: '{_TOKEN_PARAM}' is not supported"
                )
            return _response("ok")

        monkeypatch.setattr("paper_digest.llm.openai_client.time.sleep",
                            lambda s: pytest.fail("a parameter swap must not sleep"))
        provider = _provider(monkeypatch, create)

        assert provider.complete("q", model="m") == "ok"
        assert len(calls) == 2
        assert _LEGACY_TOKEN_PARAM in calls[1]

    def test_the_swap_is_remembered_for_later_calls(self, monkeypatch):
        calls = []

        def create(**kwargs):
            calls.append(dict(kwargs))
            if _TOKEN_PARAM in kwargs:
                raise RuntimeError(f"Unrecognized request argument: {_TOKEN_PARAM}")
            return _response("ok")

        monkeypatch.setattr("paper_digest.llm.openai_client.time.sleep", lambda s: None)
        provider = _provider(monkeypatch, create)
        provider.complete("q", model="m")
        provider.complete("q2", model="m")

        # Three calls, not four: the second complete() starts on the legacy name.
        assert len(calls) == 3
        assert all(_LEGACY_TOKEN_PARAM in c for c in calls[1:])

    def test_a_transient_error_is_retried_with_backoff(self, monkeypatch):
        attempts = {"n": 0}
        waits = []

        def create(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("503 upstream")
            return _response("ok")

        monkeypatch.setattr("paper_digest.llm.openai_client.time.sleep",
                            lambda s: waits.append(s))
        assert _provider(monkeypatch, create).complete("q", model="m") == "ok"
        assert waits == [2]

    def test_the_last_error_is_raised_rather_than_returning_empty(self, monkeypatch):
        def create(**kwargs):
            raise RuntimeError("still broken")

        monkeypatch.setattr("paper_digest.llm.openai_client.time.sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="still broken"):
            _provider(monkeypatch, create).complete("q", model="m")


class TestFactory:
    def test_anthropic_is_selected_by_name(self, monkeypatch):
        cfg = Config(llm=LLMConfig(provider="anthropic"), anthropic_api_key="k")
        from paper_digest.llm.anthropic_client import AnthropicProvider

        assert isinstance(create_provider(cfg), AnthropicProvider)

    def test_openai_is_selected_by_name(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setitem(__import__("sys").modules, "openai",
                            SimpleNamespace(OpenAI=lambda api_key: client))
        cfg = Config(llm=LLMConfig(provider="openai"), openai_api_key="k")

        assert isinstance(create_provider(cfg), OpenAIProvider)

    def test_the_name_is_case_insensitive(self, monkeypatch):
        cfg = Config(llm=LLMConfig(provider="Anthropic"), anthropic_api_key="k")
        assert create_provider(cfg) is not None

    def test_an_unknown_provider_lists_the_supported_ones(self):
        cfg = Config(llm=LLMConfig(provider="gemini"), anthropic_api_key="k")
        with pytest.raises(ValueError, match="anthropic.*openai"):
            create_provider(cfg)

    def test_the_provider_decides_which_key_is_required(self):
        """A config with only the wrong key set must fail, not silently pass."""
        cfg = Config(llm=LLMConfig(provider="openai"), anthropic_api_key="k")
        assert cfg.llm_api_key() == ""
        assert cfg.llm_key_env_var() == "OPENAI_API_KEY"
