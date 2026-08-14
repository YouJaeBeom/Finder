"""Reading a response out of the Anthropic Messages API.

The interesting case is the first one: current Claude models think by default,
so the first content block is routinely a thinking block. Reaching for
``content[0].text`` — which this code used to do — raises AttributeError on a
completely successful response, and the failure only appears against the live
API.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from paper_digest.llm.anthropic_client import _extract_text
from paper_digest.notes import generate_notes
from paper_digest.config import Config
from paper_digest.models import Paper, PaperIdentifiers


def _block(block_type: str, **fields) -> SimpleNamespace:
    return SimpleNamespace(type=block_type, **fields)


def _response(*blocks, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


class TestExtractText:
    def test_skips_a_leading_thinking_block(self):
        """The regression: thinking is on by default, so content[0] is not text."""
        response = _response(
            _block("thinking", thinking="내부 추론..."),
            _block("text", text='{"one_line_summary": "요약"}'),
        )
        assert _extract_text(response) == '{"one_line_summary": "요약"}'

    def test_plain_text_response_still_works(self):
        assert _extract_text(_response(_block("text", text="hello"))) == "hello"

    def test_joins_multiple_text_blocks(self):
        response = _response(
            _block("text", text='{"a": 1,'),
            _block("text", text=' "b": 2}'),
        )
        assert _extract_text(response) == '{"a": 1, "b": 2}'

    def test_refusal_is_reported_not_silently_empty(self):
        response = _response(stop_reason="refusal")
        with pytest.raises(RuntimeError, match="declined"):
            _extract_text(response)

    def test_thinking_only_response_names_the_block_types(self):
        """The error must say what came back, or it is undebuggable from a log."""
        response = _response(_block("thinking", thinking="..."))
        with pytest.raises(RuntimeError, match="thinking"):
            _extract_text(response)

    def test_empty_content_raises(self):
        with pytest.raises(RuntimeError, match="No text"):
            _extract_text(_response())

    def test_truncated_response_is_returned_with_a_warning(self, caplog):
        response = _response(_block("text", text='{"partial"'), stop_reason="max_tokens")
        assert _extract_text(response) == '{"partial"'
        assert "max_tokens" in caplog.text


class TestNoteFailureContainment:
    def test_one_failure_does_not_abort_the_batch(self):
        """A refusal on paper 2 must not throw away papers 1 and 3."""
        papers = [
            Paper(identifiers=PaperIdentifiers(), title=f"Paper {i}", abstract="abs")
            for i in range(3)
        ]

        calls = {"n": 0}

        class Provider:
            def complete(self, prompt, model, max_tokens=4096, system=None):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise RuntimeError("model declined this request")
                return (
                    '{"one_line_summary": "요약", "key_contributions": ["a","b","c"], '
                    '"method": "방법", "relevance_to_profile": "연결점"}'
                )

        generate_notes(papers, Config(research_profile="p"), Provider())

        assert all(p.research_note is not None for p in papers)
        assert papers[0].research_note.one_line_summary == "요약"
        assert "노트 생성 실패" in papers[1].research_note.one_line_summary
        assert papers[2].research_note.one_line_summary == "요약"
