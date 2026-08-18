"""Exit-code contract for the weekly pipeline.

The exit code is load-bearing: GitHub Actions turns a non-zero exit into the
failure email that surfaces a silently degraded run. Three branches must never be
confused —

  • zero keyword candidates          -> quiet week, exit 0 (no false alarm)
  • candidates > 0 but none ranked   -> anomaly, exit 1 (alert)
  • one member fails, others served  -> exit 1, but the others still get pages

The middle case is the one that cost the most to learn. The removed OpenAlex
source returned candidates whose abstracts had gone behind a paid plan; every run
produced zero pages and every run reported success.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.models import Paper, PaperIdentifiers, normalize_title
from tests.conftest import PARENT_PAGE_RAW, venue_collector, write_lab_config


def _papers(n: int, *, with_abstract: bool) -> List[Paper]:
    """N keyword-matching papers, optionally stripped of their abstracts."""
    out: List[Paper] = []
    for i in range(n):
        title = f"Large Language Model Alignment via RLHF, part {i}"
        out.append(Paper(
            identifiers=PaperIdentifiers(
                doi=f"10.18653/exit-code-test/{i}",
                normalized_title=normalize_title(title),
            ),
            title=title,
            abstract=(
                "RLHF alignment for large language models." if with_abstract else None
            ),
            authors=["A"],
            venue="ACL",
            venue_status="published",
            collection_date=datetime.now(timezone.utc).date().isoformat(),
            source=["conference"],
        ))
    return out


@pytest.fixture()
def config_path(tmp_path, monkeypatch, fake_notion) -> str:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "mock-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "mock-key")
    return write_lab_config(tmp_path, parent=PARENT_PAGE_RAW)


def _run(config_path: str, collected: List[Paper]) -> int:
    from paper_digest.pipeline import run_weekly

    provider = MagicMock()
    provider.complete.return_value = "[]"

    with patch("paper_digest.pipeline.collect_venue_papers",
               side_effect=venue_collector(conference=collected)), \
         patch("paper_digest.pipeline.create_provider", return_value=provider):
        return run_weekly(config_path)


def test_quiet_week_exits_zero(config_path):
    """Nothing collected at all -> not an anomaly, must not raise a false alarm."""
    assert _run(config_path, []) == 0


def test_collected_but_nothing_matching_exits_zero(config_path):
    """Papers arrived, none matched anyone's keywords. Still a quiet week."""
    off_topic = _papers(5, with_abstract=True)
    for paper in off_topic:
        paper.title = f"Sourdough hydration ratios, study {paper.identifiers.doi}"
        paper.abstract = "We measure crumb structure across hydration levels."
        paper.identifiers.normalized_title = normalize_title(paper.title)
    assert _run(config_path, off_topic) == 0


def test_candidates_but_none_rankable_exits_one(config_path):
    """Every candidate lost its abstract — the OpenAlex takedown scenario.

    Candidates cleared the keyword filter but nothing was rankable, so the run
    produced no pages. That must fail the Actions job rather than pass silently.
    """
    assert _run(config_path, _papers(8, with_abstract=False)) == 1


def test_ranking_below_the_cutoff_exits_one(config_path):
    """Rankable candidates that every score puts below the threshold."""
    from paper_digest.pipeline import run_weekly

    provider = MagicMock()
    provider.complete.return_value = (
        '[{"id": "0", "score": 1}, {"id": "1", "score": 0}]'
    )
    with patch("paper_digest.pipeline.collect_venue_papers",
               side_effect=venue_collector(
                   conference=_papers(2, with_abstract=True))), \
         patch("paper_digest.pipeline.create_provider", return_value=provider):
        assert run_weekly(config_path) == 1


def test_missing_notion_token_exits_one(config_path, monkeypatch):
    """A missing secret is a configuration failure, not a quiet week."""
    monkeypatch.setenv("NOTION_TOKEN", "")
    from paper_digest.pipeline import run_weekly

    assert run_weekly(config_path) == 1


def test_a_broken_member_file_exits_one_before_collecting(tmp_path, monkeypatch,
                                                          fake_notion):
    """A member file that cannot be used stops the run before it spends anything."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "tok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "key")
    config_path = write_lab_config(tmp_path, parent=PARENT_PAGE_RAW)
    (tmp_path / "members" / "broken.yaml").write_text(
        "name: 없는사람\nkeywords: []\n", encoding="utf-8"
    )

    collector = MagicMock(return_value=[])
    from paper_digest.pipeline import run_weekly

    with patch("paper_digest.pipeline.collect_venue_papers", collector):
        assert run_weekly(config_path) == 1
    collector.assert_not_called()
