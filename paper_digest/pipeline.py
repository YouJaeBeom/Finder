"""Main pipeline: collect once, write news once, then serve each member in turn.

The shape of a run, and why it is this shape:

    1. Notion first        a wrong token costs two seconds here, not a full
                           collection run and a ranking bill
    2. Collect once        Semantic Scholar is queried by venue and date, never
                           by keyword, so ten members cost exactly what one does
    3. News once           shared by everyone, written to the main page
    4. Members in turn     keyword filter → already-received → rank → note → write

Step 2 is what makes the lab version cheap. Nothing about collection depends on
who is reading: the venue allowlist and the date window are lab-wide, and each
member's keywords are applied afterwards as a local string match. Adding a
member adds zero external requests to the collection stage.

Members are processed **sequentially in one process**, not as parallel GitHub
Actions jobs. Notion's rate limit is per integration token, so parallel jobs
share one budget while each behaves as though it owns the whole thing — the
throttle in :mod:`paper_digest.notion_api` can only hold a line it can see. Fault
isolation, the one thing parallel jobs would buy, is a ``try``/``except`` here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import List, Optional, Sequence, Set, Tuple

from .collectors.semantic_scholar import collect_venue_papers
from .config import PLACEHOLDERS, Config, load_config
from .dedup import DedupStore, deduplicate_collected
from .keywords import select_for_keywords
from .llm.base import LLMProvider
from .llm.factory import create_provider
from .members import Member, MemberConfigError, effective_config, load_members
from .models import Paper
from .news_stage import run_news
from .notes import generate_notes
from .notion_query import written_index
from .notion_writer import (
    create_page,
    ensure_member_space,
    ensure_news_database,
    page_exists,
)
from .ranking import rank_papers
from .reporter import write_failure_report, write_report
from .venues import CONFERENCE, JOURNAL, select_venues

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)


# ── Per-member outcome ─────────────────────────────────────────────────────────

@dataclass
class MemberOutcome:
    """What one member's turn produced, including how it failed."""

    member: Member
    candidates: int = 0
    fresh: int = 0
    created: List[Paper] = field(default_factory=list)
    error: Optional[str] = None

    def as_report_row(self) -> dict:
        return {
            "member_id": self.member.member_id,
            "name": self.member.name,
            "candidates": self.candidates,
            "new": self.fresh,
            "created": len(self.created),
            "error": self.error,
        }


# ── Preflight ──────────────────────────────────────────────────────────────────

def preflight(cfg: Config, needs_llm: bool = True) -> Optional[str]:
    """Return why this run cannot possibly succeed, or None if it can.

    Checked before any collection or LLM call. Everything here is a configuration
    mistake that no amount of work downstream can recover from, and finding out
    after forty minutes of collection and a bill for ranking is the wrong time to
    learn about it.

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

    if needs_llm and (wrong := cfg.llm_key_looks_wrong()):
        return wrong

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

    return None


def check_budget(cfg: Config, members: Sequence[Member]) -> Optional[str]:
    """Refuse a run that would write more notes than the lab allows.

    Notes are ~95% of the bill and the lab shares one key, so this is the guard
    that no per-member limit can provide: fifteen people each legitimately at
    their own maximum is still a bill nobody approved.
    """
    planned = sum(m.top_n for m in members if m.top_n is not None)
    if cfg.news.enabled:
        planned += cfg.news.top_n

    if planned > cfg.limits.max_notes_per_run:
        return (
            f"This run would write up to {planned} notes, over the lab limit of "
            f"{cfg.limits.max_notes_per_run} (limits.max_notes_per_run). Lower "
            "someone's top_n or raise the limit deliberately."
        )
    return None


def _load_run_inputs(
    config_path: str,
    only: Optional[str] = None,
) -> Tuple[Optional[Config], List[Member], Optional[str]]:
    """Config plus members, or the reason the run cannot start.

    Everything that can be checked without touching the network is checked here,
    together, so a misconfigured member file and a missing token are both
    reported before anything is collected or spent.
    """
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        return None, [], f"config load failed ({config_path}): {exc}"

    try:
        members = load_members(
            cfg.members_dir,
            max_members=cfg.limits.max_members,
            max_top_n=cfg.limits.max_top_n_per_member,
        )
    except MemberConfigError as exc:
        return cfg, [], str(exc)

    if only:
        members = [m for m in members if m.member_id == only]
        if not members:
            return cfg, [], (
                f"No enabled member with id {only!r} in {cfg.members_dir!r}. "
                "Run `python -m paper_digest members list` to see them."
            )

    if problem := preflight(cfg):
        return cfg, members, problem

    if problem := check_budget(cfg, members):
        return cfg, members, problem

    return cfg, members, None


# ── Collection (shared by every member) ────────────────────────────────────────

def _collect_venues(
    cfg: Config,
    kind: str,
    days_back: Optional[int] = None,
    max_results: Optional[int] = None,
) -> List[Paper]:
    """Papers from one venue class, or nothing if that source is disabled.

    Never raises. The two classes fail independently on purpose: proceedings
    arrive in yearly bursts and journals publish steadily, so a Semantic Scholar
    error while fetching one must not cost the other its whole run.
    """
    source = cfg.conferences if kind == CONFERENCE else cfg.journals
    if not source.enabled:
        return []

    venues = select_venues(
        min_score=source.min_score,
        include=source.include,
        exclude=source.exclude,
        kind=kind,
    )
    if not venues:
        logger.warning("No %ss selected — check %ss.min_score", kind, kind)
        return []

    try:
        return collect_venue_papers(
            venues=venues,
            days_back=cfg.days_back if days_back is None else days_back,
            max_results=(source.max_results if max_results is None
                         else max_results),
            source_label=kind,
            # Journals answer with their exact registered name; conferences do
            # not. See semantic_scholar._label_for.
            exact_venue_match=(kind == JOURNAL),
        )
    except Exception as exc:
        logger.warning("%s collection failed: %s", kind.capitalize(), exc)
        return []


def _collect_papers(
    cfg: Config,
    days_back: Optional[int] = None,
    max_results: Optional[int] = None,
    kinds: Sequence[str] = (CONFERENCE, JOURNAL),
) -> List[Paper]:
    """Every paper this run should consider, from both venue classes."""
    collected: List[Paper] = []
    counts = []
    for kind in kinds:
        papers = _collect_venues(cfg, kind, days_back, max_results)
        collected.extend(papers)
        counts.append(f"{kind}s: {len(papers)}")
    logger.info("Collected %d papers (%s)", len(collected), ", ".join(counts))
    return collected


def _build_pool(
    cfg: Config,
    days_back: Optional[int] = None,
    max_results: Optional[int] = None,
    kinds: Sequence[str] = (CONFERENCE, JOURNAL),
) -> List[Paper]:
    """The deduplicated candidate pool every member is filtered against."""
    collected = _collect_papers(cfg, days_back, max_results, kinds)
    pool = deduplicate_collected(collected)
    logger.info("%d unique papers in the pool", len(pool))
    return pool


# ── One member's turn ──────────────────────────────────────────────────────────

def _serve_member(
    cfg: Config,
    member: Member,
    pool: Sequence[Paper],
    provider: LLMProvider,
    top_n: Optional[int] = None,
    rank_all: bool = False,
    note_budget: Optional[int] = None,
) -> MemberOutcome:
    """Filter, rank, annotate and write one member's papers into their database.

    *top_n* and *rank_all* are the backfill overrides: a catch-up run keeps far
    more than a week's worth and scores the entire window rather than obeying
    ``max_papers_to_rank``, because taking the best 200 of a year means ranking
    the year — a cap would drop papers in collection order, which is arbitrary.

    Raises only on Notion problems that make the member unservable (their page or
    database could not be resolved, or their database could not be read). Those
    are caught by the caller so one member's Notion trouble cannot end everyone
    else's run.
    """
    space = ensure_member_space(
        cfg.parent_page_id(), member.member_id, member.name, cfg.notion_token
    )
    # Truth layer: what this member already has. Queried, not remembered — see
    # paper_digest.notion_query.
    index = written_index(space.database_id, cfg.notion_token)
    # Optimization layer: what was scored and cut, which Notion has no record of.
    scored = DedupStore(path=member.scored_cache_path(),
                        database_id=space.database_id)

    candidates = select_for_keywords(pool, member.keywords)
    fresh = [p for p in candidates
             if not index.contains(p) and not scored.is_seen(p)]

    outcome = MemberOutcome(member=member, candidates=len(candidates),
                            fresh=len(fresh))
    logger.info("%s: %d candidates, %d not yet seen", member.name,
                len(candidates), len(fresh))

    if not fresh:
        return outcome

    member_cfg = effective_config(cfg, member)
    if top_n is not None:
        member_cfg = replace(member_cfg, top_n=top_n)
    if rank_all:
        member_cfg = replace(member_cfg, max_papers_to_rank=len(fresh))

    try:
        top = rank_papers(fresh, member_cfg, provider)
    except RuntimeError as exc:
        # Candidates existed and none cleared the cutoff. Reported as an error
        # rather than as a quiet week: that pattern means a misconfigured profile
        # or a degraded LLM API, and it must not pass silently.
        outcome.error = f"ranking anomaly: {exc}"
        logger.error("%s: %s", member.name, outcome.error)
        return outcome

    if not top:
        # Two ways to get here, and only one of them is a fault.
        if not any(p.abstract for p in fresh):
            # A collapse in abstract coverage: candidates cleared the keyword
            # filter and not one could be judged. This is the failure that let
            # the old OpenAlex source return nothing for weeks while every run
            # reported success, so it must not pass silently.
            outcome.error = (
                f"{len(fresh)} new candidates but none were rankable — every "
                "one arrived without an abstract"
            )
            logger.error("%s: %s", member.name, outcome.error)
            return outcome
        # Otherwise the model read them and did not rate any of them highly
        # enough, which is an ordinary outcome for a week with one or two
        # leftover candidates. See rank_papers for why this is not an alert.
        logger.info("%s: nothing cleared the relevance cutoff this week",
                    member.name)
        return outcome

    if note_budget is not None and len(top) > note_budget:
        # Never silently: a cap that trims the tail without saying so reads as
        # "there was nothing else", which is the failure this whole change was
        # meant to avoid.
        logger.warning(
            "%s: %d papers cleared the cutoff but only %d fit inside "
            "limits.max_notes_per_run — %d dropped. Raise the limit if this "
            "keeps happening.",
            member.name, len(top), note_budget, len(top) - note_budget,
        )
        top = top[:note_budget]

    generate_notes(top, member_cfg, provider)

    for paper in top:
        try:
            paper.notion_page_id = create_page(
                paper, space.database_id, cfg.notion_token, space.properties
            )
            index.add(paper)
            outcome.created.append(paper)
        except Exception as exc:
            logger.error("%s: could not write %r: %s", member.name,
                         paper.title[:60], exc)

    # Everything considered is cached, not just what was written. These papers
    # have been judged; leaving the rejected ones out would make next week
    # re-rank what it already paid to score.
    for paper in fresh:
        scored.mark_seen(paper)
    scored.persist()

    logger.info("%s: %d page(s) written", member.name, len(outcome.created))
    return outcome


# ── Reporting helpers ──────────────────────────────────────────────────────────

def _overlap(outcomes: Sequence[MemberOutcome]) -> List[dict]:
    """Papers more than one member received, most-shared first.

    The signal a shared database would have carried in an ``Overlap`` column.
    With one database per member it belongs in the run report instead, which is
    also where anyone deciding what to discuss at a seminar would look.
    """
    by_title: dict = {}
    for outcome in outcomes:
        for paper in outcome.created:
            key = paper.identifiers.normalized_title or paper.title
            entry = by_title.setdefault(key, {"title": paper.title,
                                              "venue": paper.venue,
                                              "members": []})
            entry["members"].append(outcome.member.name)

    shared = [e for e in by_title.values() if len(e["members"]) > 1]
    shared.sort(key=lambda e: len(e["members"]), reverse=True)
    return shared


def _monthly_exit_code(outcomes: Sequence[MemberOutcome]) -> Tuple[int, Optional[str]]:
    """0 when every member was served, 1 with a summary when any failed."""
    failed = [o for o in outcomes if o.error]
    if not failed:
        return 0, None
    summary = "; ".join(f"{o.member.name}: {o.error}" for o in failed)
    return 1, f"{len(failed)} of {len(outcomes)} members failed — {summary}"


# ── Monthly mode ────────────────────────────────────────────────────────────────

def _ensure_notion_structure(
    cfg: Config,
    members: Sequence[Member],
) -> Tuple[Optional[str], Optional[Set[str]]]:
    """Build the workspace layout and return the news database, if enabled.

    Member pages are created **before** the news database on purpose. The Notion
    API appends new children to the end of a page and offers no way to reorder
    them afterwards, so creation order *is* the layout: making the people first
    leaves the news table sitting underneath their links, which is where a
    reader wants it — the roster is the navigation, the news is the feed.

    Runs before collection and before any LLM call. An unshared page is the most
    common setup mistake and should cost two seconds, not a full collection run
    and a ranking bill.
    """
    if not page_exists(cfg.parent_page_id(), cfg.notion_token):
        raise RuntimeError(
            f"parent page {cfg.parent_page_id()} is not visible to this "
            "integration — open the page in Notion and share it via "
            "'···' → 'Connections'"
        )

    for member in members:
        # One member's Notion trouble must not cost everyone else their digest,
        # so it is only logged here. The member loop calls this again and records
        # the failure against that member alone — this pass exists for layout
        # order, not to decide who gets served.
        try:
            space = ensure_member_space(
                cfg.parent_page_id(), member.member_id, member.name,
                cfg.notion_token,
            )
        except Exception as exc:
            logger.warning("Could not prepare %s's space yet: %s",
                           member.name, exc)
            continue
        logger.info("%s → page %s, database %s", member.name,
                    space.page_id, space.database_id)

    if not cfg.news.enabled:
        return None, None
    return ensure_news_database(cfg.parent_page_id(), cfg.notion_token)


def run_monthly(config_path: str = "config.yaml", only: Optional[str] = None) -> int:
    """Collect once, write news once, then serve every enabled member in turn.

    Returns 0 when every member was served (including quiet weeks where nobody
    had new papers), 1 when any member failed or hit a ranking anomaly.
    """
    cfg, members, problem = _load_run_inputs(config_path, only)
    if problem:
        logger.error("Cannot start: %s", problem)
        write_failure_report("monthly", problem)
        return 1
    assert cfg is not None  # _load_run_inputs returns a problem otherwise

    # ── Notion first ──────────────────────────────────────────────────────────
    # Before collection and before any LLM call: a wrong token or an unshared
    # page is the most common setup mistake and should cost two seconds.
    try:
        news_db_id, news_props = _ensure_notion_structure(cfg, members)
    except Exception as exc:
        logger.error("Notion is not reachable or not configured: %s", exc)
        write_failure_report("monthly", f"notion: {exc}")
        return 1

    # ── Collection, once for everyone ─────────────────────────────────────────
    logger.info("=== Collection (shared by %d member(s)) ===", len(members))
    pool = _build_pool(cfg)

    provider = create_provider(cfg)

    # ── News, once for everyone ───────────────────────────────────────────────
    created_news = run_news(cfg, provider, news_db_id, news_props, members)

    # ── Members, in turn ──────────────────────────────────────────────────────
    # Members may set no per-person limit, so the lab ceiling has to hold here
    # rather than in the pre-flight estimate: what is left after the news, shared
    # across everyone, spent in the order members are served.
    remaining = max(cfg.limits.max_notes_per_run - len(created_news), 0)

    outcomes: List[MemberOutcome] = []
    for member in members:
        logger.info("=== Member: %s (%s) ===", member.name, member.member_id)
        try:
            outcome = _serve_member(cfg, member, pool, provider,
                                    note_budget=remaining)
            remaining -= len(outcome.created)
            outcomes.append(outcome)
        except Exception as exc:
            logger.error("Member %s could not be served: %s", member.name, exc)
            outcomes.append(MemberOutcome(member=member, error=str(exc)))

    if remaining <= 0:
        logger.warning(
            "The run reached limits.max_notes_per_run (%d). Members served last "
            "may have received less than they qualified for.",
            cfg.limits.max_notes_per_run,
        )

    exit_code, error = _monthly_exit_code(outcomes)

    created_papers = [p for o in outcomes for p in o.created]
    report = write_report(
        papers_created=created_papers + created_news,
        venue_updated=0,
        duplicates_created=sum(o.candidates - o.fresh for o in outcomes),
        candidates_found=sum(o.candidates for o in outcomes),
        papers_ranked=sum(o.fresh for o in outcomes),
        mode="monthly",
        error=error,
        members=[o.as_report_row() for o in outcomes],
        overlap=_overlap(outcomes),
    )

    logger.info(
        "=== Done: %d paper page(s) across %d member(s), %d news page(s) ===",
        len(created_papers), len(outcomes), len(created_news),
    )
    for row in report.get("members", []):
        logger.info("  %-12s candidates %4d → new %4d → written %3d%s",
                    row["name"], row["candidates"], row["new"], row["created"],
                    f"  [{row['error']}]" if row["error"] else "")
    for entry in report.get("overlap", []):
        logger.info("  겹침 %d명: %s (%s)", len(entry["members"]),
                    entry["title"][:70], ", ".join(entry["members"]))

    return exit_code


# ── Backfill mode ──────────────────────────────────────────────────────────────

BACKFILL_SOURCES = ("conferences", "journals", "both")


def _log_rank_estimate(candidates: int, limit: int, members: int) -> None:
    """Say what this run is about to cost, before it costs it.

    Rough by construction — token counts are estimated from measured prompt sizes
    — but the order of magnitude is what matters when the alternative is finding
    out from a bill.
    """
    batches = (candidates + 19) // 20
    rank_usd = batches * (4179 / 1e6 * 1.0 + 380 / 1e6 * 5.0)   # haiku 4.5
    note_usd = limit * (960 / 1e6 * 3.0 + 800 / 1e6 * 15.0)     # sonnet 5
    logger.info(
        "Backfill will rank up to %d papers per member in %d batches and write "
        "up to %d notes each — roughly $%.2f + $%.2f per member, $%.2f for all "
        "%d (estimate)",
        candidates, batches, limit, rank_usd, note_usd,
        (rank_usd + note_usd) * members, members,
    )


def run_backfill(
    config_path: str = "config.yaml",
    days: int = 365,
    limit: int = 200,
    sources: str = "both",
    only: Optional[str] = None,
) -> int:
    """One-off catch-up: rank a long window at once and keep the best *limit*.

    The monthly run asks "what appeared since last week", which is the right
    question forever after but leaves the whole prior year unread. This ranks that
    year in one pass per member and writes their top *limit* by relevance.

    *sources* narrows what is backfilled. Conferences are the case that needs it:
    proceedings drop once a year, so a digest set up in August has missed
    everything from the spring, while journals publish steadily and the monthly run
    picks them up on its own.

    Use ``--member`` when one person joins an established lab — backfilling
    everyone again would cost the whole lab's catch-up a second time.
    """
    if sources not in BACKFILL_SOURCES:
        logger.error("Unknown backfill source %r — expected one of %s",
                     sources, ", ".join(BACKFILL_SOURCES))
        return 1

    cfg, members, problem = _load_run_inputs(config_path, only)
    if problem:
        logger.error("Cannot start: %s", problem)
        write_failure_report("backfill", problem)
        return 1
    assert cfg is not None

    try:
        if not page_exists(cfg.parent_page_id(), cfg.notion_token):
            raise RuntimeError(f"parent page {cfg.parent_page_id()} is not "
                               "visible to this integration")
    except Exception as exc:
        logger.error("Notion is not reachable or not configured: %s", exc)
        write_failure_report("backfill", f"notion: {exc}")
        return 1

    logger.info("=== Backfill: last %d days, %s, top %d per member, %d member(s) ===",
                days, sources, limit, len(members))

    kinds = {
        "conferences": (CONFERENCE,),
        "journals": (JOURNAL,),
        "both": (CONFERENCE, JOURNAL),
    }[sources]
    pool = _build_pool(cfg, days_back=days, max_results=100_000, kinds=kinds)
    if not pool:
        logger.info("Nothing collected — nothing to backfill")
        write_report([], 0, 0, 0, 0, mode="backfill")
        return 0

    _log_rank_estimate(len(pool), limit, len(members))

    provider = create_provider(cfg)

    outcomes: List[MemberOutcome] = []
    for member in members:
        logger.info("=== Backfilling %s (%s) ===", member.name, member.member_id)
        try:
            outcomes.append(_serve_member(cfg, member, pool, provider,
                                          top_n=limit, rank_all=True))
        except Exception as exc:
            logger.error("Backfill failed for %s: %s", member.name, exc)
            outcomes.append(MemberOutcome(member=member, error=str(exc)))

    exit_code, error = _monthly_exit_code(outcomes)
    created = [p for o in outcomes for p in o.created]
    write_report(
        papers_created=created,
        venue_updated=0,
        duplicates_created=0,
        candidates_found=sum(o.candidates for o in outcomes),
        papers_ranked=sum(o.fresh for o in outcomes),
        mode="backfill",
        error=error,
        members=[o.as_report_row() for o in outcomes],
        overlap=_overlap(outcomes),
    )
    logger.info("=== Backfill done: %d page(s) across %d member(s) ===",
                len(created), len(outcomes))
    return exit_code


# ── Init mode ──────────────────────────────────────────────────────────────────

def run_init(config_path: str = "config.yaml") -> int:
    """Create the whole Notion structure: news database and every member space.

    Idempotent, and worth running before the first scheduled digest — it turns
    "Monday's run created nothing and I do not know why" into an error you read
    immediately, with nothing collected and nothing spent. Needs no LLM key.
    """
    try:
        cfg = load_config(config_path)
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", config_path, exc)
        return 1

    if problem := preflight(cfg, needs_llm=False):
        logger.error("Cannot start: %s", problem)
        return 1

    try:
        members = load_members(
            cfg.members_dir,
            max_members=cfg.limits.max_members,
            max_top_n=cfg.limits.max_top_n_per_member,
        )
    except MemberConfigError as exc:
        logger.error("Cannot start: %s", exc)
        return 1

    try:
        news_db_id, _ = _ensure_notion_structure(cfg, members)
        if news_db_id:
            logger.info("News database ready: %s", news_db_id)
    except Exception as exc:
        logger.error("Notion setup failed: %s", exc)
        return 1

    logger.info("Notion structure ready for %d member(s)", len(members))
    return 0
