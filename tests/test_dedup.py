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
