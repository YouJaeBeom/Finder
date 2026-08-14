"""Tests for arXiv and OpenAlex collectors (abstract parsing and dedup logic)."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from paper_digest.collectors.arxiv import ATOM_NS, _parse_entry
from paper_digest.collectors.openalex import _parse_work, reconstruct_abstract


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


class TestPaperLinkAndDate:
    """Both were parsed and then dropped: no link reached Notion, and the Date
    column showed the day the run happened rather than the paper's own date."""

    def _arxiv_entry(self, published: str) -> ET.Element:
        xml = f"""
        <entry xmlns="{ATOM_NS}">
          <id>http://arxiv.org/abs/2408.01234v1</id>
          <published>{published}</published>
          <title>Scaling Laws for Alignment</title>
          <summary>We study RLHF alignment in large language models.</summary>
          <author><name>Alice Smith</name></author>
        </entry>
        """
        return ET.fromstring(xml.strip())

    def _parse_arxiv(self, published: str):
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        return _parse_entry(self._arxiv_entry(published), keywords=[], cutoff=cutoff)

    def test_arxiv_paper_gets_its_abstract_page_link(self):
        recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        paper = self._parse_arxiv(recent)

        assert paper is not None
        assert paper.url == "https://arxiv.org/abs/2408.01234"

    def test_arxiv_published_date_is_the_v1_submission_not_today(self):
        submitted = datetime.now(timezone.utc) - timedelta(days=3)
        paper = self._parse_arxiv(submitted.isoformat())

        assert paper.published_at == submitted.date().isoformat()
        assert paper.published_at != paper.collection_date

    def _openalex_work(self, **overrides) -> dict:
        work = {
            "title": "Retrieval Augmented Generation",
            "abstract_inverted_index": {"We": [0], "study": [1], "RAG": [2]},
            "doi": "https://doi.org/10.18653/v1/2026.acl-long.1",
            "publication_date": "2026-08-11",
            "primary_location": {
                "source": {"display_name": "ACL 2026"},
                "landing_page_url": "https://aclanthology.org/2026.acl-long.1/",
            },
            "authorships": [{"author": {"display_name": "Bob Jones"}}],
        }
        work.update(overrides)
        return work

    def test_openalex_paper_links_via_doi(self):
        paper = _parse_work(self._openalex_work())
        assert paper.url == "https://doi.org/10.18653/v1/2026.acl-long.1"

    def test_openalex_falls_back_to_the_landing_page_without_a_doi(self):
        paper = _parse_work(self._openalex_work(doi=None))
        assert paper.url == "https://aclanthology.org/2026.acl-long.1/"

    def test_openalex_publication_date_is_carried_through(self):
        """It was computed into a local variable and never used."""
        paper = _parse_work(self._openalex_work())
        assert paper.published_at == "2026-08-11"


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
