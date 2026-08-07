"""Tests for arXiv and OpenAlex collectors (abstract parsing and dedup logic)."""
from __future__ import annotations

import pytest

from paper_digest.collectors.openalex import reconstruct_abstract
from paper_digest.models import normalize_title


class TestReconstructAbstract:
    """Tests for OpenAlex abstract_inverted_index reconstruction."""

    def test_basic_reconstruction(self):
        # "hello world" encoded as inverted index
        inverted = {"hello": [0], "world": [1]}
        result = reconstruct_abstract(inverted)
        assert result == "hello world"

    def test_out_of_order_tokens(self):
        # Tokens at positions 0, 2, 1 -> "the quick brown"
        inverted = {"the": [0], "brown": [2], "quick": [1]}
        result = reconstruct_abstract(inverted)
        assert result == "the quick brown"

    def test_multi_position_tokens(self):
        # "a b a" — 'a' appears at positions 0 and 2
        inverted = {"a": [0, 2], "b": [1]}
        result = reconstruct_abstract(inverted)
        assert result == "a b a"

    def test_none_input(self):
        assert reconstruct_abstract(None) is None

    def test_empty_dict(self):
        assert reconstruct_abstract({}) is None

    def test_realistic_abstract(self):
        inverted = {
            "We": [0],
            "propose": [1],
            "a": [2],
            "new": [3],
            "method": [4],
            "for": [5],
            "LLM": [6],
            "alignment": [7],
        }
        result = reconstruct_abstract(inverted)
        assert result == "We propose a new method for LLM alignment"


class TestArxivIdParsing:
    """Tests for arXiv ID version filtering."""

    def test_v1_accepted(self):
        from paper_digest.collectors.arxiv import _parse_arxiv_id
        result = _parse_arxiv_id("http://arxiv.org/abs/2401.12345v1")
        assert result == "2401.12345"

    def test_v2_excluded(self):
        from paper_digest.collectors.arxiv import _parse_arxiv_id
        result = _parse_arxiv_id("http://arxiv.org/abs/2401.12345v2")
        assert result is None

    def test_v3_excluded(self):
        from paper_digest.collectors.arxiv import _parse_arxiv_id
        result = _parse_arxiv_id("2401.12345v3")
        assert result is None

    def test_bare_id_v1(self):
        from paper_digest.collectors.arxiv import _parse_arxiv_id
        result = _parse_arxiv_id("2401.12345v1")
        assert result == "2401.12345"

    def test_five_digit_id(self):
        from paper_digest.collectors.arxiv import _parse_arxiv_id
        # arXiv post-2015 IDs have exactly 5 digits after the dot
        result = _parse_arxiv_id("http://arxiv.org/abs/2401.12345v1")
        assert result == "2401.12345"

    def test_invalid_id(self):
        from paper_digest.collectors.arxiv import _parse_arxiv_id
        result = _parse_arxiv_id("not-an-arxiv-id")
        assert result is None
