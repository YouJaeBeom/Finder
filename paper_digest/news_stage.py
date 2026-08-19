"""The shared news stage: collected, selected and written once per run.

News is lab-wide rather than per member. The sources are already curated by
construction — a Hacker News score floor and hand-picked feeds — so there is
nothing to personalise, and one shared pass costs a tenth of ten.

That sharing is also what makes the profile question awkward, and it is worth
being explicit about: a paper's note is written against the member who received
it, but news belongs to nobody. The members' own profiles, joined, fill that
slot; see :func:`news_profile`.

Unlike the paper stage this never raises and never changes the run's exit code. A
flaky RSS feed must not fail a run whose papers landed fine.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from typing import List, Optional, Sequence, Set

from .collectors.hackernews import collect_hackernews_stories
from .collectors.rss import collect_rss_entries
from .config import Config
from .dedup import deduplicate_collected
from .llm.base import LLMProvider
from .members import Member
from .models import Paper
from .news_select import select_news
from .notes import generate_notes
from .notion_query import written_index
from .notion_writer import create_page

logger = logging.getLogger(__name__)


def news_profile(cfg: Config, members: Sequence[Member]) -> str:
    """The context the shared news briefing is written against.

    News has no member to belong to, so it has no ``research_profile`` — and the
    briefing's "why this is worth knowing" section is empty air without one.

    Built by joining the members' own profiles rather than from a single lab
    paragraph. Members here work on genuinely different things, so one shared
    description would have to be vague enough to cover all of them, which is
    the same as saying nothing.
    """
    return "\n\n".join(f"[{m.name}] {m.research_profile.strip()}"
                       for m in members if m.research_profile.strip())


def run_news(
    cfg: Config,
    provider: LLMProvider,
    db_id: Optional[str],
    db_props: Optional[Set[str]],
    members: Sequence[Member] = (),
) -> List[Paper]:
    """Collect, select and write IT news to the shared database.

    Selection never involves the ranking model — see
    :mod:`paper_digest.news_select` for why. *provider* is used only to write the
    Korean briefing for the stories that already made the cut.

    Returns what was written, empty on any trouble. See the module docstring for
    why nothing here is allowed to fail the run.
    """
    if not cfg.news.enabled or db_id is None:
        return []

    logger.info("=== News (shared) ===")
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

    try:
        index = written_index(db_id, cfg.notion_token)
    except Exception as exc:
        logger.warning("Could not read the news database (%s) — skipping news "
                       "rather than risk duplicating it", exc)
        return []

    unique = deduplicate_collected(collected)
    # Drop already-written stories before selecting, so a repost from last week
    # cannot consume one of this run's top_n slots.
    fresh = [item for item in unique if not index.contains(item)]
    logger.info("News: %d collected, %d unique, %d not yet written",
                len(collected), len(unique), len(fresh))

    top = select_news(fresh, cfg.news.keywords, cfg.news.top_n)
    if not top:
        return []

    generate_notes(top, replace(cfg, research_profile=news_profile(cfg, members)),
                   provider)

    written: List[Paper] = []
    for item in top:
        try:
            item.notion_page_id = create_page(item, db_id, cfg.notion_token,
                                              db_props)
            index.add(item)
            written.append(item)
        except Exception as exc:
            logger.error("Failed to write news %r: %s", item.title[:60], exc)

    logger.info("News: %d page(s) written", len(written))
    return written


