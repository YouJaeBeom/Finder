"""Run report generation.

Every pipeline run writes ``run-report.json`` containing machine-checkable
evidence for the GitHub Actions artifact check.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    error: Optional[str] = None,
    members: Optional[List[Dict[str, Any]]] = None,
    overlap: Optional[List[Dict[str, Any]]] = None,
) -> dict:
    """Write run-report.json and return the report dict.

    *error* records why a run that produced output still exited non-zero — a
    ranking anomaly, say. Without it the report of a failed run says only that
    zero pages were created, leaving the reason in the step log alone.

    *members* is the per-member breakdown, which is the only place a lab run's
    aggregate totals can be taken apart: "84 pages created" hides the fact that
    one member got 80 and another got none. *overlap* lists the papers more than
    one member received — the signal a shared database would have carried in an
    ``Overlap`` column, and the natural shortlist for a seminar.

    Both are omitted from the report when absent rather than written as empty
    lists, so a single-member run's report keeps the shape it always had.
    """
    sections_filled: List[int] = []
    for paper in papers_created:
        if paper.research_note is not None:
            sections_filled.append(paper.research_note.sections_filled_count())
        else:
            sections_filled.append(0)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "status": "failed" if error else "ok",
        "pages_created": len(papers_created),
        "venue_updated": venue_updated,
        "sections_filled": sections_filled,
        "duplicates_created": duplicates_created,
        "candidates_found": candidates_found,
        "papers_ranked": papers_ranked,
    }
    if error:
        report["error"] = error
    if members is not None:
        report["members"] = members
    if overlap is not None:
        report["overlap"] = overlap

    Path(report_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def write_failure_report(
    mode: str,
    error: str,
    report_path: str = REPORT_FILE,
) -> dict:
    """Write a report for a run that never reached its work.

    Without this the Actions artifact upload finds no file on exactly the runs
    where someone most wants to know what happened, and the only record of the
    failure is buried in the step log.
    """
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "status": "failed",
        "error": error,
        "pages_created": 0,
        "venue_updated": 0,
        "sections_filled": [],
        "duplicates_created": 0,
        "candidates_found": 0,
        "papers_ranked": 0,
    }
    Path(report_path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
