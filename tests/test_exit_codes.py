"""Exit-code contract for the weekly pipeline.

The exit code is load-bearing: GitHub Actions turns a non-zero exit into the
failure email that surfaces a silently degraded run. Two branches must never be
confused —

  • zero keyword candidates        -> quiet week, exit 0 (no false alarm)
  • candidates > 0 but none ranked -> anomaly, exit 1 (alert)

Only the return value is asserted. run_weekly writes run-report.json and
seen_ids.json CWD-relative, so report-based assertions are order-sensitive; the
exit code is self-contained.
"""
from __future__ import annotations

import os
import textwrap
from datetime import datetime, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.models import Paper, PaperIdentifiers, normalize_title

CONFIG = textwrap.dedent("""\
    notion_parent_page_id: "3bc1256e05618089aaaabbbbccccdddd"
    keywords: ["large language model", "LLM", "RLHF", "alignment"]
    tracked_venues: ["ACL 2026"]
    research_profile: |
      내 연구는 대형 언어 모델의 정렬과 안전성에 초점을 맞추고 있습니다.
    arxiv_categories: [cs.CL, cs.AI, cs.LG]
    days_back: 7
    max_papers_to_rank: 1500
    top_n: 10
    llm:
      provider: "anthropic"
      ranking_model: "claude-haiku-4-5"
      notes_model: "claude-opus-5"
""")


def _papers(n: int, *, with_abstract: bool) -> List[Paper]:
    """N keyword-matching papers, optionally stripped of their abstracts."""
    out: List[Paper] = []
    for i in range(n):
        title = f"Large Language Model Alignment via RLHF, part {i}"
        out.append(Paper(
            identifiers=PaperIdentifiers(
                arxiv_id=None,
                doi=f"10.18653/exit-code-test/{i}",
                normalized_title=normalize_title(title),
            ),
            title=title,
            abstract=(
                "RLHF alignment for large language models." if with_abstract else None
            ),
            authors=["A"],
            venue="OpenAlex",
            venue_status="preprint",
            collection_date=datetime.now(timezone.utc).date().isoformat(),
            source=["openalex"],
        ))
    return out


def _run(config_path: str, collected: List[Paper]) -> int:
    """Run the weekly pipeline with every external service mocked."""
    notion_resp = MagicMock()
    notion_resp.raise_for_status = MagicMock()
    notion_resp.json.return_value = {"id": "mock-id"}

    # The database is resolved up front now, before collection, so even a quiet
    # week reaches Notion. An empty children list sends it down the create path.
    notion_get = MagicMock()
    notion_get.raise_for_status = MagicMock()
    notion_get.json.return_value = {"results": [], "has_more": False}

    with (
        patch("paper_digest.pipeline.collect_arxiv_papers", return_value=[]),
        patch("paper_digest.pipeline.collect_openalex_papers", return_value=collected),
        patch("paper_digest.pipeline.collect_conference_papers", return_value=[]),
        patch("paper_digest.pipeline.create_provider", return_value=MagicMock()),
        patch("paper_digest.notion_writer.requests.post", return_value=notion_resp),
        patch("paper_digest.notion_writer.requests.get", return_value=notion_get),
        patch("paper_digest.notion_writer.requests.patch", return_value=notion_resp),
        patch.dict(os.environ, {
            "NOTION_TOKEN": "mock-token",
            "ANTHROPIC_API_KEY": "mock-key",
        }),
    ):
        from paper_digest.pipeline import run_weekly
        return run_weekly(config_path)


@pytest.fixture()
def config_path(tmp_path, monkeypatch) -> str:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "seen_ids.json").write_text("[]")
    (tmp_path / "config.yaml").write_text(CONFIG)
    return str(tmp_path / "config.yaml")


def test_quiet_week_exits_zero(config_path):
    """Nothing collected at all -> not an anomaly, must not raise a false alarm."""
    assert _run(config_path, []) == 0


def test_candidates_but_none_rankable_exits_one(config_path):
    """Every candidate lost its abstract — the OpenAlex takedown scenario.

    Candidates cleared the keyword filter but nothing was rankable, so the run
    produced no pages. That must fail the Actions job rather than pass silently.
    """
    assert _run(config_path, _papers(8, with_abstract=False)) == 1


def test_missing_notion_token_exits_one(config_path):
    """A missing secret is a configuration failure, not a quiet week."""
    with patch.dict(os.environ, {"NOTION_TOKEN": "", "ANTHROPIC_API_KEY": "k"}):
        from paper_digest.pipeline import run_weekly
        assert run_weekly(config_path) == 1
