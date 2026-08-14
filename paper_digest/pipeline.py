"""Main pipeline orchestration for weekly and batch modes."""
from __future__ import annotations

import logging
import sys
from typing import List, Optional

from dataclasses import replace

from .collectors.arxiv import collect_arxiv_papers
from .collectors.hackernews import collect_hackernews_stories
from .collectors.rss import collect_rss_entries
from .collectors.openalex import collect_openalex_papers
from .config import Config, load_config
from .dedup import DedupStore, deduplicate_collected
from .llm.base import LLMProvider
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

    # ── 4. Cross-run deduplication ────────────────────────────────────────────
    dedup_store = DedupStore()
    new_candidates = [p for p in candidates if not dedup_store.is_seen(p)]
    duplicates_skipped = len(candidates) - len(new_candidates)
    logger.info(
        "%d new candidates (%d already seen)", len(new_candidates), duplicates_skipped
    )

    # From here the paper side can come up empty without ending the run: news is
    # a separate delivery and must not be suppressed by a quiet paper week.
    # paper_exit carries the paper pipeline's verdict to the end.
    paper_exit = 0
    top_papers: List[Paper] = []
    provider: Optional[LLMProvider] = None

    if not new_candidates:
        if candidates_count == 0:
            logger.info("No keyword candidates this week (quiet week)")
        else:
            logger.info("All candidates already seen — no new pages to create")
    else:
        # ── 5. LLM ranking ────────────────────────────────────────────────────
        logger.info("=== Stage 4: LLM ranking (%s / %s) ===",
                    cfg.llm.provider, cfg.llm.ranking_model)
        provider = create_provider(cfg)

        try:
            top_papers = rank_papers(new_candidates, cfg, provider)
        except RuntimeError as exc:
            logger.error("Ranking anomaly: %s", exc)
            top_papers = []
            paper_exit = 1
        else:
            if not top_papers:
                # Keyword candidates existed but nothing cleared the cutoff. Exit 1
                # so the Actions run is marked failed and the alert fires — a
                # misconfigured cutoff or a degraded LLM API must not pass
                # silently. A genuinely quiet week has zero candidates and keeps 0.
                logger.error(
                    "Ranking anomaly: %d candidates ranked but none passed the cutoff",
                    len(new_candidates),
                )
                paper_exit = 1

    # ── 6/7. Notes + Notion write ─────────────────────────────────────────────
    created_papers: List[Paper] = []
    db_id: Optional[str] = None

    if top_papers or cfg.news.enabled:
        db_id = ensure_database(cfg.notion_parent_page_id, cfg.notion_token)

    if top_papers:
        logger.info("=== Stage 5: Note generation (%s / %s) ===",
                    cfg.llm.provider, cfg.llm.notes_model)
        generate_notes(top_papers, cfg, provider)

        logger.info("=== Stage 6: Notion write ===")
        for paper in top_papers:
            try:
                page_id = create_page(paper, db_id, cfg.notion_token)
                paper.notion_page_id = page_id
                dedup_store.mark_seen(paper)
                created_papers.append(paper)
            except Exception as exc:
                logger.error("Failed to create Notion page for '%s': %s",
                             paper.title[:60], exc)

    # ── 8. IT news (opt-in) ───────────────────────────────────────────────────
    # Runs after the papers so a news-side failure can never cost us the paper
    # pages that were already written.
    if cfg.news.enabled and provider is None:
        provider = create_provider(cfg)
    created_news = _run_news_stage(cfg, provider, dedup_store, db_id)
    created_papers.extend(created_news)

    dedup_store.persist()

    # ── 9. Report ─────────────────────────────────────────────────────────────
    report = write_report(
        papers_created=created_papers,
        venue_updated=0,
        duplicates_created=duplicates_skipped,
        candidates_found=candidates_count,
        papers_ranked=len(new_candidates),
        mode="weekly",
    )

    logger.info(
        "=== Done: %d pages created (%d news), %d duplicates skipped ===",
        report["pages_created"],
        len(created_news),
        report["duplicates_created"],
    )
    return paper_exit


def _run_news_stage(
    cfg: Config,
    provider: LLMProvider,
    dedup_store: DedupStore,
    db_id: str,
) -> List[Paper]:
    """Collect, rank and write IT news. Returns the items written to Notion.

    Deliberately never raises and never changes the run's exit code: the
    exit-code contract in the requirements is about the paper pipeline, and a
    flaky RSS feed must not fail a run whose papers landed fine. News trouble is
    logged as a warning instead.
    """
    if not cfg.news.enabled or db_id is None:
        return []

    logger.info("=== Stage 7: IT news ===")
    collected: List[Paper] = []

    try:
        if cfg.news.hacker_news_enabled:
            collected.extend(collect_hackernews_stories(
                min_points=cfg.news.hacker_news_min_points,
                days_back=cfg.days_back,
            ))
        if cfg.news.rss_feeds:
            collected.extend(collect_rss_entries(
                feed_urls=cfg.news.rss_feeds,
                days_back=cfg.days_back,
            ))
    except Exception as exc:
        logger.warning("News collection failed: %s", exc)
        return []

    if not collected:
        logger.info("News: nothing collected this run")
        return []

    unique_news = deduplicate_collected(collected)
    news_keywords = cfg.news.keywords or cfg.keywords
    candidates = _filter_by_keywords(unique_news, news_keywords)
    fresh = [item for item in candidates if not dedup_store.is_seen(item)]
    logger.info(
        "News: %d collected, %d unique, %d matched keywords, %d new",
        len(collected), len(unique_news), len(candidates), len(fresh),
    )
    if not fresh:
        return []

    news_cfg = replace(cfg, top_n=cfg.news.top_n)
    try:
        top_news = rank_papers(fresh, news_cfg, provider)
    except RuntimeError as exc:
        # Nothing cleared the cutoff. For papers this is an alert; for news it is
        # a quiet week in the feeds, so it stays a warning.
        logger.warning("News ranking passed nothing: %s", exc)
        return []

    if not top_news:
        return []

    generate_notes(top_news, cfg, provider)

    written: List[Paper] = []
    for item in top_news:
        try:
            item.notion_page_id = create_page(item, db_id, cfg.notion_token)
            dedup_store.mark_seen(item)
            written.append(item)
        except Exception as exc:
            logger.error("Failed to create Notion page for news '%s': %s",
                         item.title[:60], exc)

    logger.info("News: %d pages created", len(written))
    return written


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
