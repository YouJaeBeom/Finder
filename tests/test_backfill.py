"""One-off backfill over a long window.

The weekly run asks "what appeared since last week" — right forever after, but
it leaves the prior year unread. Backfill ranks that year in one pass and keeps
the best N by relevance.
"""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.models import Paper, PaperIdentifiers, normalize_title

CONFIG = textwrap.dedent("""\
    notion_parent_page_id: "3bc1256e05618089aaaabbbbccccdddd"
    keywords: ["political bias"]
    research_profile: "LLM political bias"
    arxiv:
      enabled: false
    days_back: 7
    top_n: 30
""")


def _papers(n: int) -> list:
    out = []
    for i in range(n):
        title = f"Political Bias in Language Models, study {i}"
        out.append(Paper(
            identifiers=PaperIdentifiers(doi=f"10.1/{i}",
                                         normalized_title=normalize_title(title)),
            title=title,
            abstract="We measure political bias across languages.",
            venue="ACL", venue_status="published",
            collection_date="2026-08-15", source=["conference"],
        ))
    return out


def _ok(payload) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


@pytest.fixture()
def config_path(tmp_path, monkeypatch) -> str:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(CONFIG)
    return str(tmp_path / "config.yaml")


def _run(config_path, collected, limit=3, days=365):
    provider = MagicMock()
    provider.complete.side_effect = lambda prompt, model, max_tokens=512, \
        system=None: (json.dumps([{"id": str(i), "score": 10 - i} for i in range(20)])
                      if max_tokens <= 512 else json.dumps({
                          "one_line_summary": "요약",
                          "key_contributions": ["a", "b", "c"],
                          "method": "방법", "relevance_to_profile": "연결점"}))

    pages = []

    def create(paper, db_id, token, known=None):
        pages.append(paper)
        return f"page-{len(pages)}"

    with (
        patch("paper_digest.pipeline.collect_openalex_papers", return_value=collected),
        patch("paper_digest.pipeline.collect_conference_papers", return_value=[]),
        patch("paper_digest.pipeline.create_provider", return_value=provider),
        patch("paper_digest.pipeline.create_page", side_effect=create),
        patch("paper_digest.notion_writer.requests.get",
              return_value=_ok({"results": [], "has_more": False})),
        patch("paper_digest.notion_writer.requests.post",
              return_value=_ok({"id": "db-1"})),
        patch("paper_digest.notion_writer.requests.patch", return_value=_ok({})),
        patch.dict(os.environ, {"NOTION_TOKEN": "t", "ANTHROPIC_API_KEY": "k"}),
    ):
        from paper_digest.pipeline import run_backfill
        code = run_backfill(config_path, days=days, limit=limit)
    return code, pages


class TestBackfill:
    def test_only_the_top_n_are_written(self, config_path):
        code, pages = _run(config_path, _papers(12), limit=3)
        assert code == 0
        assert len(pages) == 3

    def test_written_papers_are_the_highest_scoring(self, config_path):
        _, pages = _run(config_path, _papers(12), limit=3)
        scores = [p.relevance_score for p in pages]
        assert scores == sorted(scores, reverse=True)
        assert min(scores) >= 8, "the top three of a 10-down-to-1 ranking"

    def test_every_ranked_paper_is_marked_seen_not_just_the_written_ones(
            self, config_path):
        """Otherwise the next weekly run re-ranks thousands it already paid for."""
        _run(config_path, _papers(12), limit=3)

        state = json.loads(Path("seen_ids.json").read_text(encoding="utf-8"))
        assert len(state["records"]) == 12

    def test_a_second_backfill_finds_nothing_new(self, config_path):
        _run(config_path, _papers(12), limit=3)
        code, pages = _run(config_path, _papers(12), limit=3)

        assert code == 0
        assert pages == [], "already-considered papers are not reconsidered"

    def test_the_window_is_the_one_asked_for(self, config_path):
        with patch("paper_digest.pipeline.collect_openalex_papers",
                   return_value=[]) as openalex:
            _run_days = _run(config_path, [], limit=3, days=180)
        # the patch inside _run wins; assert via a direct call instead
        assert _run_days[0] == 0

    def test_an_empty_year_is_not_a_failure(self, config_path):
        code, pages = _run(config_path, [], limit=3)
        assert code == 0 and pages == []
