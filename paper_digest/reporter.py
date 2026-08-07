"""Run report generation.

Every pipeline run writes ``run-report.json`` containing machine-checkable
evidence for the GitHub Actions artifact check.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import Paper

REPORT_FILE = "run-report.json"


def write_report(
    papers_created: List[Paper],
    venue_updated: int,
    duplicates_created: int,
    candidates_found: int,
    papers_ranked: int,
    mode: str,
    report_path: str = REPORT_FILE,
) -> dict:
    """Write run-report.json and return the report dict."""
    sections_filled: List[int] = []
    for paper in papers_created:
        if paper.research_note is not None:
            sections_filled.append(paper.research_note.sections_filled_count())
        else:
            sections_filled.append(0)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "pages_created": len(papers_created),
        "venue_updated": venue_updated,
        "sections_filled": sections_filled,
        "duplicates_created": duplicates_created,
        "candidates_found": candidates_found,
        "papers_ranked": papers_ranked,
    }

    Path(report_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
