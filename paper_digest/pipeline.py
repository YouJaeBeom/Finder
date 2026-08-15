"""Main pipeline orchestration for weekly and batch modes."""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import List, Optional, Set

from .collectors.arxiv import collect_arxiv_papers
from .collectors.conferences import collect_conference_papers
from .collectors.hackernews import collect_hackernews_stories
from .collectors.rss import collect_rss_entries
from .collectors.openalex import collect_openalex_papers
from .config import PLACEHOLDERS, Config, load_config
from .dedup import DedupStore, deduplicate_collected
from .keywords import filter_by_keywords
from .llm.base import LLMProvider
from .llm.factory import create_provider
from .models import Paper
from .news_select import select_news
from .notes import generate_notes
from .notion_writer import (
    create_page,
    ensure_database,
    query_preprint_pages,
    update_venue,
)
from .ranking import rank_papers
from .venues import select_venues, venue_aliases_from_list
from .reporter import write_failure_report, write_report

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)


# ── Preflight ──────────────────────────────────────────────────────────────────

def preflight(cfg: Config, needs_llm: bool = True) -> Optional[str]:
    """Return why this run cannot possibly succeed, or None if it can.

    Checked before any collection or LLM call. Everything here is a
    configuration mistake that no amount of work downstream can recover from,
    and finding out after forty minutes of collection and a bill for ranking is
    the wrong time to learn about it.

    *needs_llm* is False for init mode, which only ever talks to Notion.
    """
    if not cfg.notion_token:
        return (
            "NOTION_TOKEN is not set. In GitHub Actions this is a repository "
            "secret: Settings → Secrets and variables → Actions."
        )

    if needs_llm and not cfg.llm_api_key():
        return (
            f"{cfg.llm_key_env_var()} is not set (llm.provider is "
            f"'{cfg.llm.provider}'). In GitHub Actions this is a repository secret."
        )

    raw_parent = cfg.notion_parent_page_id
    if not raw_parent or raw_parent in PLACEHOLDERS:
        return (
            "notion_parent_page_id in config.yaml is still the placeholder. "
            "Open your Notion page, copy its link, and paste it there."
        )

    if not cfg.parent_page_id():
        return (
            f"notion_parent_page_id does not contain a Notion ID: {raw_parent!r}. "
            "Paste the page link (or the 32-character ID) from Notion."
        )

    if cfg.notion_database_id and not cfg.database_id():
        return (
            f"notion_database_id does not contain a Notion ID: "
            f"{cfg.notion_database_id!r}. Leave it empty to auto-resolve."
        )

    return None


def _collect_conferences(
    cfg: Config,
    days_back: Optional[int] = None,
    max_results: Optional[int] = None,
) -> List[Paper]:
    """Papers from the top conferences, or nothing if the source is disabled.

    Never raises: conference proceedings are a bonus source, and a Semantic
    Scholar outage must not cost the arXiv and journal papers of the same run.
    """
    if not cfg.conferences.enabled:
        return []

    venues = select_venues(
        min_score=cfg.conferences.min_score,
        include=cfg.conferences.include,
        exclude=cfg.conferences.exclude,
    )
    if not venues:
        logger.warning("No conferences selected — check conferences.min_score")
        return []

    try:
        return collect_conference_papers(
            venues=venues,
            days_back=cfg.days_back if days_back is None else days_back,
            max_results=(cfg.conferences.max_results if max_results is None
                         else max_results),
        )
    except Exception as exc:
        logger.warning("Conference collection failed: %s", exc)
        return []


# ── Weekly mode ────────────────────────────────────────────────────────────────

def run_weekly(config_path: str = "config.yaml") -> int:
    """Run the full weekly collection → filter → rank → note → Notion pipeline.

    Returns exit code: 0 for success or clean empty run, 1 for anomalies.
    """
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", config_path, exc)
        write_failure_report("weekly", f"config load failed: {exc}")
        return 1

    if problem := preflight(cfg):
        logger.error("Cannot start: %s", problem)
        write_failure_report("weekly", problem)
        return 1

    # ── 0. Notion first ───────────────────────────────────────────────────────
    # Resolved before collection and before any LLM call. A wrong token or page
    # ID is the most common setup mistake, and it should cost two seconds to
    # discover, not a full collection run and a ranking bill.
    try:
        db_id, db_props = ensure_database(
            cfg.parent_page_id(), cfg.notion_token, cfg.database_id()
        )
    except Exception as exc:
        logger.error("Notion is not reachable or not configured: %s", exc)
        write_failure_report("weekly", f"notion: {exc}")
        return 1

    # ── 1. Collection ──────────────────────────────────────────────────────────
    logger.info("=== Stage 1: Collection ===")
    arxiv_papers = (
        collect_arxiv_papers(
            categories=cfg.arxiv.categories,
            keywords=cfg.keywords,
            days_back=cfg.days_back,
        )
        if cfg.arxiv.enabled
        else []
    )
    openalex_papers = collect_openalex_papers(
        keywords=cfg.keywords,
        days_back=cfg.days_back,
        venue_aliases={**venue_aliases_from_list(), **cfg.venue_aliases},
        excluded_venues=cfg.excluded_venues,
        mailto=cfg.openalex_mailto,
        api_key=cfg.openalex_api_key,
        # Weekly asks "what became visible to us", not "what was published".
        # A journal issue from May indexed today is new to this digest, and a
        # publication-date window would miss it permanently.
        by_index_date=True,
    )
    conference_papers = _collect_conferences(cfg)
    all_papers = arxiv_papers + openalex_papers + conference_papers
    logger.info(
        "Collected %d total papers (arXiv: %d, OpenAlex: %d, conferences: %d)",
        len(all_papers), len(arxiv_papers), len(openalex_papers),
        len(conference_papers),
    )

    # ── 2. Within-run deduplication ───────────────────────────────────────────
    logger.info("=== Stage 2: Deduplication ===")
    unique_papers = deduplicate_collected(all_papers)
    logger.info("%d unique papers after cross-source dedup", len(unique_papers))

    # ── 3. Keyword filtering ──────────────────────────────────────────────────
    logger.info("=== Stage 3: Keyword filtering ===")
    candidates = filter_by_keywords(unique_papers, cfg.keywords)
    candidates_count = len(candidates)
    logger.info("%d candidates after keyword filtering", candidates_count)

    # ── 4. Cross-run deduplication ────────────────────────────────────────────
    dedup_store = DedupStore(database_id=db_id)
    new_candidates = [p for p in candidates if not dedup_store.is_seen(p)]
    duplicates_skipped = len(candidates) - len(new_candidates)
    logger.info(
        "%d new candidates (%d already seen)", len(new_candidates), duplicates_skipped
    )

    # From here the paper side can come up empty without ending the run: news is
    # a separate delivery and must not be suppressed by a quiet paper week.
    # paper_exit carries the paper pipeline's verdict to the end.
    paper_exit = 0
    paper_error: Optional[str] = None
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
            paper_error = f"ranking anomaly: {exc}"
        else:
            if not top_papers:
                # Keyword candidates existed but nothing cleared the cutoff. Exit 1
                # so the Actions run is marked failed and the alert fires — a
                # misconfigured cutoff or a degraded LLM API must not pass
                # silently. A genuinely quiet week has zero candidates and keeps 0.
                paper_error = (
                    f"ranking anomaly: {len(new_candidates)} candidates were "
                    f"ranked but none passed the cutoff"
                )
                logger.error("%s", paper_error)
                paper_exit = 1

    # ── 6/7. Notes + Notion write ─────────────────────────────────────────────
    created_papers: List[Paper] = []

    if top_papers:
        logger.info("=== Stage 5: Note generation (%s / %s) ===",
                    cfg.llm.provider, cfg.llm.notes_model)
        generate_notes(top_papers, cfg, provider)

        logger.info("=== Stage 6: Notion write ===")
        for paper in top_papers:
            try:
                page_id = create_page(paper, db_id, cfg.notion_token, db_props)
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
    created_news = _run_news_stage(cfg, provider, dedup_store, db_id, db_props)
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
        error=paper_error,
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
    provider: Optional[LLMProvider],
    dedup_store: DedupStore,
    db_id: Optional[str],
    db_props: Optional[Set[str]] = None,
) -> List[Paper]:
    """Collect, select and write IT news. Returns the items written to Notion.

    Unlike papers, news never goes through the cheap-model relevance gate — see
    :mod:`paper_digest.news_select` for why. The provider here is used only to
    write the Korean briefing for the stories already selected.

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
    # Drop already-written stories before selecting, so a repost from last week
    # cannot consume one of this run's top_n slots.
    fresh = [item for item in unique_news if not dedup_store.is_seen(item)]
    logger.info(
        "News: %d collected, %d unique, %d not yet written",
        len(collected), len(unique_news), len(fresh),
    )

    top_news = select_news(fresh, cfg.news.keywords, cfg.news.top_n)
    if not top_news:
        return []

    generate_notes(top_news, cfg, provider)

    written: List[Paper] = []
    for item in top_news:
        try:
            item.notion_page_id = create_page(item, db_id, cfg.notion_token, db_props)
            dedup_store.mark_seen(item)
            written.append(item)
        except Exception as exc:
            logger.error("Failed to create Notion page for news '%s': %s",
                         item.title[:60], exc)

    logger.info("News: %d pages created", len(written))
    return written


def _log_rank_estimate(candidates: int, limit: int) -> None:
    """Say what this run is about to cost, before it costs it.

    Rough by construction — token counts are estimated from measured prompt
    sizes — but the order of magnitude is what matters when the alternative is
    finding out from a bill.
    """
    batches = (candidates + 19) // 20
    rank_usd = batches * (4179 / 1e6 * 1.0 + 380 / 1e6 * 5.0)   # haiku 4.5
    note_usd = limit * (960 / 1e6 * 3.0 + 800 / 1e6 * 15.0)     # sonnet 5
    logger.info(
        "Backfill will rank %d papers in %d batches and write notes for up to "
        "%d — roughly $%.2f in ranking plus $%.2f in notes (estimate)",
        candidates, batches, limit, rank_usd, note_usd,
    )


# ── Backfill mode ──────────────────────────────────────────────────────────────

BACKFILL_SOURCES = ("conferences", "journals", "both")


def run_backfill(
    config_path: str = "config.yaml",
    days: int = 365,
    limit: int = 200,
    sources: str = "both",
) -> int:
    """One-off catch-up: rank a long window at once and keep the best *limit*.

    The weekly run asks "what appeared since last week", which is the right
    question forever after but leaves the whole prior year unread. This ranks
    that year in a single pass and writes the top *limit* by relevance.

    Every paper it ranks is marked seen, not just the ones written. They have
    been considered and judged; leaving the rejected ones unseen would make the
    next weekly run re-rank thousands of papers it has already paid to score.

    *sources* narrows what is backfilled. Conferences are the case that needs
    it: proceedings drop once a year, so a digest set up in August has missed
    everything from the spring, while journals publish steadily and the weekly
    run picks them up on its own.
    """
    if sources not in BACKFILL_SOURCES:
        logger.error("Unknown backfill source %r — expected one of %s",
                     sources, ", ".join(BACKFILL_SOURCES))
        return 1
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        write_failure_report("backfill", f"config load failed: {exc}")
        return 1

    if problem := preflight(cfg):
        logger.error("Cannot start: %s", problem)
        write_failure_report("backfill", problem)
        return 1

    try:
        db_id, db_props = ensure_database(
            cfg.parent_page_id(), cfg.notion_token, cfg.database_id()
        )
    except Exception as exc:
        logger.error("Notion is not reachable or not configured: %s", exc)
        write_failure_report("backfill", f"notion: {exc}")
        return 1

    logger.info("=== Backfill: last %d days, %s, keeping the top %d ===",
                days, sources, limit)

    openalex_papers = (
        collect_openalex_papers(
            keywords=cfg.keywords,
            days_back=days,
            max_results=100_000,
            venue_aliases={**venue_aliases_from_list(), **cfg.venue_aliases},
            excluded_venues=cfg.excluded_venues,
            mailto=cfg.openalex_mailto,
            api_key=cfg.openalex_api_key,
        )
        if sources in ("journals", "both")
        else []
    )
    conference_papers = (
        _collect_conferences(cfg, days_back=days, max_results=100_000)
        if sources in ("conferences", "both")
        else []
    )
    logger.info("Backfill sources: OpenAlex %d, conferences %d",
                len(openalex_papers), len(conference_papers))
    unique = deduplicate_collected(openalex_papers + conference_papers)
    candidates = filter_by_keywords(unique, cfg.keywords)

    dedup_store = DedupStore(database_id=db_id)
    fresh = [p for p in candidates if not dedup_store.is_seen(p)]
    logger.info("Backfill: %d collected → %d unique → %d matched → %d new",
                len(openalex_papers) + len(conference_papers), len(unique),
                len(candidates), len(fresh))

    if not fresh:
        logger.info("Nothing new to backfill")
        write_report([], 0, 0, len(candidates), 0, mode="backfill")
        return 0

    # Backfill deliberately ranks everything rather than obeying
    # max_papers_to_rank: taking the top 200 of a year means scoring the year,
    # and a cap would drop papers in collection order, which is arbitrary.
    # The cost scales with that count, so it is stated before it is spent.
    _log_rank_estimate(len(fresh), limit)

    provider = create_provider(cfg)
    ranked_cfg = replace(cfg, top_n=limit, max_papers_to_rank=len(fresh))
    try:
        top_papers = rank_papers(fresh, ranked_cfg, provider)
    except RuntimeError as exc:
        logger.error("Backfill ranking anomaly: %s", exc)
        write_failure_report("backfill", f"ranking anomaly: {exc}")
        return 1

    logger.info("Backfill: writing the top %d of %d ranked", len(top_papers),
                len(fresh))
    generate_notes(top_papers, cfg, provider)

    created: List[Paper] = []
    for paper in top_papers:
        try:
            paper.notion_page_id = create_page(paper, db_id, cfg.notion_token,
                                               db_props)
            created.append(paper)
        except Exception as exc:
            logger.error("Failed to create Notion page for %r: %s",
                         paper.title[:60], exc)

    # Everything considered is marked seen — see the docstring.
    for paper in fresh:
        dedup_store.mark_seen(paper)
    dedup_store.persist()

    write_report(created, 0, 0, len(candidates), len(fresh), mode="backfill")
    logger.info("=== Backfill done: %d pages created, %d marked seen ===",
                len(created), len(fresh))
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
        write_failure_report("batch", f"config load failed: {exc}")
        return 1

    if problem := preflight(cfg):
        logger.error("Cannot start: %s", problem)
        write_failure_report("batch", problem)
        return 1

    # Resolves to the same database the weekly run uses — by config, by the
    # cached ID, or by finding it under the parent page. Batch mode used to
    # require a local state.json, which never exists on a fresh CI checkout.
    try:
        db_id, db_props = ensure_database(
            cfg.parent_page_id(), cfg.notion_token, cfg.database_id()
        )
    except Exception as exc:
        logger.error("Could not resolve the Notion database: %s", exc)
        write_failure_report("batch", f"notion: {exc}")
        return 1

    # Update existing preprint pages to accepted venue
    logger.info("=== Batch mode: updating venue to '%s' ===", venue)
    preprint_pages = query_preprint_pages(db_id, cfg.notion_token)
    venue_updated_count = 0

    dedup_store = DedupStore(database_id=db_id)
    for page in preprint_pages:
        page_id = page["id"]
        try:
            update_venue(page_id, venue, cfg.notion_token)
            venue_updated_count += 1
        except Exception as exc:
            logger.error("Failed to update page %s: %s", page_id, exc)

    # Collect new papers (limited to top_n)
    logger.info("=== Batch mode: collecting new papers (top_n=%d) ===", cfg.top_n)
    arxiv_papers = (
        collect_arxiv_papers(
            categories=cfg.arxiv.categories,
            keywords=cfg.keywords,
            days_back=cfg.days_back,
        )
        if cfg.arxiv.enabled
        else []
    )
    openalex_papers = collect_openalex_papers(
        keywords=cfg.keywords,
        days_back=cfg.days_back,
        venue_aliases=cfg.venue_aliases,
        excluded_venues=cfg.excluded_venues,
    )
    all_papers = arxiv_papers + openalex_papers
    unique_papers = deduplicate_collected(all_papers)
    candidates = filter_by_keywords(unique_papers, cfg.keywords)
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
                        page_id = create_page(paper, db_id, cfg.notion_token, db_props)
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

    write_report(
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

    if problem := preflight(cfg, needs_llm=False):
        logger.error("Cannot start: %s", problem)
        return 1

    try:
        db_id, _ = ensure_database(
            cfg.parent_page_id(), cfg.notion_token, cfg.database_id()
        )
    except Exception as exc:
        logger.error("Notion is not reachable or not configured: %s", exc)
        return 1

    logger.info("Notion database ready: %s", db_id)
    logger.info(
        "Pin it by adding this line to config.yaml:\n  notion_database_id: \"%s\"",
        db_id,
    )
    return 0
