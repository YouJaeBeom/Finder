"""Main pipeline orchestration for weekly and batch modes."""
from __future__ import annotations

import logging
import sys
from typing import List, Optional

from .collectors.arxiv import collect_arxiv_papers
from .collectors.openalex import collect_openalex_papers
from .config import Config, load_config
from .dedup import DedupStore, deduplicate_collected
from .llm.factory import create_provider
from .models import Paper
from .notes import generate_notes
from .notion_writer import (
    create_page,
    ensure_database,
    query_preprint_pages,
    update_venue,
)
from .ranking import rank_papers
from .reporter import write_report

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)


# ── Keyword filtering ──────────────────────────────────────────────────────────

def _matches_keywords(paper: Paper, keywords: List[str]) -> List[str]:
    """Return the list of keywords that match the paper's title or abstract."""
    text = f"{paper.title} {paper.abstract or ''}".lower()
    matched = [kw for kw in keywords if kw.lower() in text]
    return matched


def _filter_by_keywords(papers: List[Paper], keywords: List[str]) -> List[Paper]:
    """Keep only papers that match at least one keyword; populate matched_keywords."""
    result = []
    for paper in papers:
        matched = _matches_keywords(paper, keywords)
        if matched:
            paper.matched_keywords = matched
            result.append(paper)
    return result


# ── Weekly mode ────────────────────────────────────────────────────────────────

def run_weekly(config_path: str = "config.yaml") -> int:
    """Run the full weekly collection → filter → rank → note → Notion pipeline.

    Returns exit code: 0 for success or clean empty run, 1 for anomalies.
    """
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", config_path, exc)
        return 1

    if not cfg.notion_token:
        logger.error("NOTION_TOKEN environment variable is not set")
        return 1

    llm_key = cfg.anthropic_api_key if cfg.llm.provider == "anthropic" else cfg.openai_api_key
    if not llm_key:
        logger.error(
            "%s API key environment variable is not set",
            "ANTHROPIC_API_KEY" if cfg.llm.provider == "anthropic" else "OPENAI_API_KEY",
        )
        return 1

    # ── 1. Collection ──────────────────────────────────────────────────────────
    logger.info("=== Stage 1: Collection ===")
    arxiv_papers = collect_arxiv_papers(
        categories=cfg.arxiv_categories,
        keywords=cfg.keywords,
        days_back=cfg.days_back,
    )
    openalex_papers = collect_openalex_papers(
        keywords=cfg.keywords,
        days_back=cfg.days_back,
    )
    all_papers = arxiv_papers + openalex_papers
    logger.info("Collected %d total papers (arXiv: %d, OpenAlex: %d)",
                len(all_papers), len(arxiv_papers), len(openalex_papers))

    # ── 2. Within-run deduplication ───────────────────────────────────────────
    logger.info("=== Stage 2: Deduplication ===")
    unique_papers = deduplicate_collected(all_papers)
    logger.info("%d unique papers after cross-source dedup", len(unique_papers))

    # ── 3. Keyword filtering ──────────────────────────────────────────────────
    logger.info("=== Stage 3: Keyword filtering ===")
    candidates = _filter_by_keywords(unique_papers, cfg.keywords)
    candidates_count = len(candidates)
    logger.info("%d candidates after keyword filtering", candidates_count)

    if candidates_count == 0:
        logger.info("No keyword candidates this week — clean exit (quiet week)")
        write_report(
            papers_created=[],
            venue_updated=0,
            duplicates_created=0,
            candidates_found=0,
            papers_ranked=0,
            mode="weekly",
        )
        return 0

    # ── 4. Cross-run deduplication ────────────────────────────────────────────
    dedup_store = DedupStore()
    new_candidates = [p for p in candidates if not dedup_store.is_seen(p)]
    duplicates_skipped = len(candidates) - len(new_candidates)
    logger.info(
        "%d new candidates (%d already seen)", len(new_candidates), duplicates_skipped
    )

    if not new_candidates:
        logger.info("All candidates already seen — no new pages to create")
        write_report(
            papers_created=[],
            venue_updated=0,
            duplicates_created=duplicates_skipped,
            candidates_found=candidates_count,
            papers_ranked=0,
            mode="weekly",
        )
        return 0

    # ── 5. LLM ranking ───────────────────────────────────────────────────────
    logger.info("=== Stage 4: LLM ranking (%s / %s) ===",
                cfg.llm.provider, cfg.llm.ranking_model)
    provider = create_provider(cfg)

    try:
        top_papers = rank_papers(new_candidates, cfg, provider)
    except RuntimeError as exc:
        logger.error("Ranking anomaly: %s", exc)
        write_report(
            papers_created=[],
            venue_updated=0,
            duplicates_created=duplicates_skipped,
            candidates_found=candidates_count,
            papers_ranked=len(new_candidates),
            mode="weekly",
        )
        return 1

    if not top_papers:
        # Ranking anomaly: keyword candidates existed but nothing cleared the
        # relevance cutoff. Exit 1 so the GitHub Actions run is marked failed and
        # the failure email fires — a misconfigured cutoff or a degraded LLM API
        # must not pass silently. (A genuinely quiet week has zero candidates and
        # already returned 0 above.)
        logger.error(
            "Ranking anomaly: %d candidates ranked but none passed the cutoff",
            len(new_candidates),
        )
        write_report(
            papers_created=[],
            venue_updated=0,
            duplicates_created=duplicates_skipped,
            candidates_found=candidates_count,
            papers_ranked=len(new_candidates),
            mode="weekly",
        )
        return 1

    # ── 6. Note generation ────────────────────────────────────────────────────
    logger.info("=== Stage 5: Note generation (%s / %s) ===",
                cfg.llm.provider, cfg.llm.notes_model)
    generate_notes(top_papers, cfg, provider)

    # ── 7. Notion write ───────────────────────────────────────────────────────
    logger.info("=== Stage 6: Notion write ===")
    db_id = ensure_database(cfg.notion_parent_page_id, cfg.notion_token)

    created_papers: List[Paper] = []
    for paper in top_papers:
        try:
            page_id = create_page(paper, db_id, cfg.notion_token)
            paper.notion_page_id = page_id
            dedup_store.mark_seen(paper)
            created_papers.append(paper)
        except Exception as exc:
            logger.error("Failed to create Notion page for '%s': %s", paper.title[:60], exc)

    dedup_store.persist()

    # ── 8. Report ─────────────────────────────────────────────────────────────
    report = write_report(
        papers_created=created_papers,
        venue_updated=0,
        duplicates_created=duplicates_skipped,
        candidates_found=candidates_count,
        papers_ranked=len(new_candidates),
        mode="weekly",
    )

    logger.info(
        "=== Done: %d pages created, %d duplicates skipped ===",
        report["pages_created"],
        report["duplicates_created"],
    )
    return 0


# ── Batch mode ─────────────────────────────────────────────────────────────────

def run_batch(config_path: str = "config.yaml", venue: Optional[str] = None) -> int:
    """Batch mode: stamp accepted venue onto preprint pages, add at most top_n new ones.

    Triggered manually via workflow_dispatch.
    """
    if not venue:
        logger.error("--venue must be specified for batch mode (e.g. 'ACL 2026')")
        return 1

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return 1

    if not cfg.notion_token:
        logger.error("NOTION_TOKEN environment variable is not set")
        return 1

    from pathlib import Path
    import json

    state_path = Path("state.json")
    if not state_path.exists():
        logger.error("state.json not found — run weekly mode first to create the database")
        return 1
    state = json.loads(state_path.read_text())
    db_id = state.get("notion_database_id")
    if not db_id:
        logger.error("notion_database_id not found in state.json")
        return 1

    # Update existing preprint pages to accepted venue
    logger.info("=== Batch mode: updating venue to '%s' ===", venue)
    preprint_pages = query_preprint_pages(db_id, cfg.notion_token)
    venue_updated_count = 0

    dedup_store = DedupStore()
    for page in preprint_pages:
        page_id = page["id"]
        try:
            update_venue(page_id, venue, cfg.notion_token)
            venue_updated_count += 1
        except Exception as exc:
            logger.error("Failed to update page %s: %s", page_id, exc)

    # Collect new papers (limited to top_n)
    logger.info("=== Batch mode: collecting new papers (top_n=%d) ===", cfg.top_n)
    arxiv_papers = collect_arxiv_papers(
        categories=cfg.arxiv_categories,
        keywords=cfg.keywords,
        days_back=cfg.days_back,
    )
    openalex_papers = collect_openalex_papers(
        keywords=cfg.keywords,
        days_back=cfg.days_back,
    )
    all_papers = arxiv_papers + openalex_papers
    unique_papers = deduplicate_collected(all_papers)
    candidates = _filter_by_keywords(unique_papers, cfg.keywords)
    new_candidates = [p for p in candidates if not dedup_store.is_seen(p)]

    created_papers: List[Paper] = []
    if new_candidates:
        llm_key = cfg.anthropic_api_key if cfg.llm.provider == "anthropic" else cfg.openai_api_key
        if llm_key:
            provider = create_provider(cfg)
            try:
                top_papers = rank_papers(new_candidates, cfg, provider)
                top_papers = top_papers[: cfg.top_n]
                generate_notes(top_papers, cfg, provider)
                for paper in top_papers:
                    paper.venue = venue
                    paper.venue_status = "accepted"
                    try:
                        page_id = create_page(paper, db_id, cfg.notion_token)
                        paper.notion_page_id = page_id
                        dedup_store.mark_seen(paper)
                        created_papers.append(paper)
                    except Exception as exc:
                        logger.error("Failed to create page: %s", exc)
            except RuntimeError as exc:
                logger.warning("Batch ranking anomaly: %s", exc)
        else:
            logger.warning("No LLM key set — skipping new paper additions in batch mode")

    dedup_store.persist()

    report = write_report(
        papers_created=created_papers,
        venue_updated=venue_updated_count,
        duplicates_created=0,
        candidates_found=len(candidates),
        papers_ranked=len(new_candidates),
        mode="batch",
    )

    logger.info(
        "=== Batch done: %d venue updated, %d new pages created ===",
        venue_updated_count,
        len(created_papers),
    )
    return 0


# ── Init mode ──────────────────────────────────────────────────────────────────

def run_init(config_path: str = "config.yaml") -> int:
    """Create the Notion database under the configured parent page."""
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return 1

    if not cfg.notion_token:
        logger.error("NOTION_TOKEN environment variable is not set")
        return 1

    if not cfg.notion_parent_page_id:
        logger.error("notion_parent_page_id must be set in config.yaml")
        return 1

    db_id = ensure_database(cfg.notion_parent_page_id, cfg.notion_token)
    logger.info("Notion database ready: %s", db_id)
    return 0
