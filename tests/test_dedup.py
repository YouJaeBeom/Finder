"""Tests for cross-source and cross-run deduplication."""
from __future__ import annotations

import json
import os

import pytest

from paper_digest.dedup import DedupStore, deduplicate_collected
from paper_digest.models import Paper, PaperIdentifiers, normalize_title
from tests.conftest import make_paper


# ── normalize_title ────────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_lowercases(self):
        assert normalize_title("Attention Is ALL You Need") == "attention is all you need"

    def test_strips_punctuation(self):
        assert normalize_title("LLMs: A Survey.") == "llms a survey"

    def test_collapses_whitespace(self):
        assert normalize_title("  A   B  ") == "a b"

    def test_empty(self):
        assert normalize_title("") == ""

    def test_unicode_preserved(self):
        result = normalize_title("한국어 논문 제목")
        assert "한국어" in result


# ── Within-run deduplication ───────────────────────────────────────────────────

class TestDeduplicateCollected:
    def test_same_arxiv_id_merged(self):
        p1 = make_paper(arxiv_id="2401.00001", title="Foo", source=["arxiv"])
        p2 = make_paper(arxiv_id="2401.00001", title="Foo", source=["openalex"])
        result = deduplicate_collected([p1, p2])
        assert len(result) == 1
        assert set(result[0].source) == {"arxiv", "openalex"}

    def test_same_doi_merged(self):
        p1 = make_paper(doi="10.1234/test", title="Bar", source=["arxiv"])
        p2 = make_paper(doi="10.1234/test", title="Bar", source=["openalex"])
        result = deduplicate_collected([p1, p2])
        assert len(result) == 1

    def test_same_normalized_title_merged(self):
        p1 = make_paper(title="LLM Alignment", source=["arxiv"])
        p2 = make_paper(title="LLM Alignment", source=["openalex"])
        result = deduplicate_collected([p1, p2])
        assert len(result) == 1

    def test_different_papers_kept(self):
        p1 = make_paper(arxiv_id="2401.00001", title="Paper A")
        p2 = make_paper(arxiv_id="2401.00002", title="Paper B")
        result = deduplicate_collected([p1, p2])
        assert len(result) == 2

    def test_abstract_merged_from_openalex(self):
        p1 = make_paper(arxiv_id="2401.00001", title="Foo", abstract=None, source=["arxiv"])
        p2 = make_paper(arxiv_id="2401.00001", title="Foo", abstract="Rich abstract", source=["openalex"])
        result = deduplicate_collected([p1, p2])
        assert result[0].abstract == "Rich abstract"


# ── DedupStore (cross-run state) ──────────────────────────────────────────────

class TestNewsDeduplicateCollected:
    """News identity is the link — the same story runs under many headlines."""

    def _news(self, title: str, url: str, venue: str, points: int | None = None) -> Paper:
        return Paper(
            identifiers=PaperIdentifiers(
                normalized_title=normalize_title(title), url=url
            ),
            title=title,
            abstract=None,
            venue=venue,
            source=["rss"],
            content_type="news",
            url=url,
            points=points,
        )

    def test_same_link_under_two_headlines_is_one_item(self):
        """Syndicated stories reach two feeds with two different headlines."""
        items = [
            self._news("Anthropic ships Opus 5", "https://x.com/a", "TechCrunch"),
            self._news("Opus 5 is out, says Anthropic", "https://x.com/a", "The Verge"),
        ]
        result = deduplicate_collected(items)

        assert len(result) == 1
        assert result[0].source == ["rss"]

    def test_distinct_links_are_kept_apart(self):
        items = [
            self._news("Story one", "https://x.com/1", "TechCrunch"),
            self._news("Story two", "https://x.com/2", "TechCrunch"),
        ]
        assert len(deduplicate_collected(items)) == 2

    def test_merge_keeps_the_higher_community_score(self):
        items = [
            self._news("Same story", "https://x.com/a", "Hacker News", points=120),
            self._news("Same story", "https://x.com/a", "Hacker News", points=800),
        ]
        result = deduplicate_collected(items)

        assert len(result) == 1
        assert result[0].points == 800


class TestCrossIdentifierMerge:
    def test_arxiv_and_openalex_records_of_one_paper_merge_on_title(self):
        """arXiv gives an ID, OpenAlex gives a DOI — only the title is shared."""
        arxiv = make_paper(arxiv_id="2401.00001", title="Scaling Laws", abstract=None,
                           source=["arxiv"])
        openalex = make_paper(doi="10.1234/x", title="Scaling Laws",
                              abstract="Full abstract.", source=["openalex"])

        result = deduplicate_collected([arxiv, openalex])

        assert len(result) == 1, "the same paper must not be listed twice"
        assert sorted(result[0].source) == ["arxiv", "openalex"]
        assert result[0].abstract == "Full abstract."


class TestDedupStore:
    def test_new_paper_not_seen(self, tmp_path):
        store = DedupStore(str(tmp_path / "seen_ids.json"))
        paper = make_paper(arxiv_id="2401.00099")
        assert not store.is_seen(paper)

    def test_after_mark_seen(self, tmp_path):
        store = DedupStore(str(tmp_path / "seen_ids.json"))
        paper = make_paper(arxiv_id="2401.00099")
        store.mark_seen(paper)
        assert store.is_seen(paper)

    def test_persists_across_instances(self, tmp_path):
        path = str(tmp_path / "seen_ids.json")
        store1 = DedupStore(path)
        paper = make_paper(arxiv_id="2401.00099")
        store1.mark_seen(paper)
        store1.persist()

        store2 = DedupStore(path)
        assert store2.is_seen(paper)

    def test_doi_match_across_sources(self, tmp_path):
        path = str(tmp_path / "seen_ids.json")
        store = DedupStore(path)
        p1 = make_paper(doi="10.1234/abc", title="Paper Z")
        store.mark_seen(p1)

        p2 = make_paper(doi="10.1234/abc", title="Paper Z Variant")
        assert store.is_seen(p2)

    def test_title_match(self, tmp_path):
        path = str(tmp_path / "seen_ids.json")
        store = DedupStore(path)
        p1 = make_paper(title="Attention Is All You Need")
        store.mark_seen(p1)

        p2 = make_paper(title="Attention Is ALL You Need")
        assert store.is_seen(p2)
