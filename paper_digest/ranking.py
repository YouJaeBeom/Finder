"""Two-tier LLM relevance ranking pipeline.

Bulk-ranks candidate papers using the cheap ranking model, returns the top-N
papers sorted by descending score.  Papers with no abstract are excluded.
"""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List

from .config import Config
from .llm.base import LLMProvider
from .models import Paper

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20  # papers per LLM ranking call
# Fallback for callers holding a config that predates the setting.
_MIN_SCORE = 5

_RANKING_SYSTEM = (
    "You are a research relevance ranker. "
    "Rate each paper's relevance to the researcher's profile on a scale of 0–10. "
    "Return ONLY a valid JSON array with no prose before or after it."
)

_RANKING_PROMPT_TEMPLATE = """\
Researcher Profile:
{profile}

Rate each paper below from 0 (completely irrelevant) to 10 (highly relevant).
Return a JSON array: [{{"id": "<id>", "score": <0-10>}}, ...]

Papers:
{papers_block}
"""


def _is_rankable(paper: Paper) -> bool:
    """Whether a paper carries enough text for the ranking model.

    An abstract is required: a title alone is not enough to judge research
    relevance. Semantic Scholar's abstract coverage is uneven across publishers,
    so this drops a real share of what is collected rather than ranking blind.

    Only papers reach this module. News is selected mechanically without an LLM
    — see :mod:`paper_digest.news_select`.
    """
    return bool(paper.abstract)


def _build_papers_block(papers: List[Paper]) -> str:
    """Serialize papers for the ranking prompt."""
    lines = []
    for i, p in enumerate(papers):
        entry = f"[{i}] Title: {p.title}\n    Abstract: {(p.abstract or '')[:500]}"
        lines.append(entry)
    return "\n\n".join(lines)


def _parse_scores(raw: str, count: int) -> List[float]:
    """Parse LLM JSON response into a list of scores (length == count)."""
    # Strip markdown fences if present
    raw = re.sub(r"```(?:json)?", "", raw).strip()

    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract a JSON array from the text
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                items = json.loads(m.group())
            except json.JSONDecodeError:
                logger.warning("Could not parse ranking response; defaulting scores to 0")
                return [0.0] * count
        else:
            logger.warning("No JSON array found in ranking response; defaulting to 0")
            return [0.0] * count

    if not isinstance(items, list):
        return [0.0] * count

    scores: List[float] = []
    for item in items:
        if isinstance(item, dict):
            scores.append(float(item.get("score", 0)))
        elif isinstance(item, (int, float)):
            scores.append(float(item))

    # Pad or truncate to match expected count
    while len(scores) < count:
        scores.append(0.0)
    return scores[:count]


def rank_papers(
    papers: List[Paper],
    cfg: Config,
    provider: LLMProvider,
) -> List[Paper]:
    """Rank papers by relevance to the research profile and return top-N.

    Papers with no abstract are excluded before ranking (see _is_rankable).
    If candidates > 0 but all score exactly 0.0, the function raises
    RuntimeError to signal a ranking anomaly (pipeline should exit 1).
    """
    rankable = [p for p in papers if _is_rankable(p)]

    if not rankable:
        logger.info("No rankable papers (an abstract is required)")
        return []

    # Truncate to max_papers_to_rank. Papers are dropped in collection order,
    # which is arbitrary — so this is a warning, not a note. With no keyword
    # gate ahead of it this cap is the only thing that can silently shorten a
    # member's month.
    if len(rankable) > cfg.max_papers_to_rank:
        logger.warning(
            "%d papers to rank exceeds max_papers_to_rank (%d) — dropping %d in "
            "collection order, which is arbitrary. Raise the cap if this recurs.",
            len(rankable), cfg.max_papers_to_rank,
            len(rankable) - cfg.max_papers_to_rank,
        )
        rankable = rankable[: cfg.max_papers_to_rank]

    # Rank in batches, several at a time. Each batch is one call about its own
    # twenty papers and nothing else, so the only thing that has to survive
    # concurrency is that a batch's scores land on *that* batch — which they do,
    # because the papers are scored inside the same closure that fetched them
    # rather than by position in a shared list.
    batches = [rankable[i : i + _BATCH_SIZE]
               for i in range(0, len(rankable), _BATCH_SIZE)]

    def score_batch(batch: List[Paper]) -> None:
        prompt = _RANKING_PROMPT_TEMPLATE.format(
            profile=cfg.research_profile,
            papers_block=_build_papers_block(batch),
        )
        raw = provider.complete(
            prompt=prompt,
            model=cfg.llm.ranking_model,
            max_tokens=512,
            system=_RANKING_SYSTEM,
        )
        for paper, score in zip(batch, _parse_scores(raw, len(batch))):
            paper.relevance_score = score

    workers = max(1, min(cfg.llm.concurrency, len(batches)))
    if workers == 1:
        for n, batch in enumerate(batches):
            try:
                score_batch(batch)
            except Exception as exc:
                logger.error("LLM ranking failed for batch %d: %s", n, exc)
                raise
    else:
        logger.info("Ranking %d papers in %d batches, %d at a time",
                    len(rankable), len(batches), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(score_batch, b) for b in batches]
            for future in futures:
                # Raised here rather than swallowed: an unscored batch is 20
                # papers silently sitting at 0.0, which reads as "irrelevant".
                future.result()

    # Filter and sort
    cutoff = getattr(cfg, "min_relevance", _MIN_SCORE)
    qualified = [p for p in rankable if p.relevance_score >= cutoff]
    qualified.sort(key=lambda p: p.relevance_score, reverse=True)

    if not qualified and rankable:
        # Two very different things look alike here, and only one is a fault.
        #
        # _parse_scores answers with [0.0] * count for *every* failure — bad
        # JSON, no array, an unexpected shape — so an all-zero result is the
        # signature of ranking not having happened at all. A working model
        # handed papers it finds irrelevant answers with a spread of small
        # non-zero numbers instead.
        #
        # Treating the spread as a fault used to be defensible, when the monthly
        # window was seven days and everything in it was new. With a 30-day
        # window and per-member deduplication the ordinary week leaves one or
        # two leftover candidates, and one of those scoring a 3 is not an
        # incident — it is Tuesday. Alerting on it teaches people to ignore the
        # alert, which costs more than the case it was meant to catch.
        #
        # The collapse this originally guarded against — candidates arriving
        # with no abstract at all — never reaches this branch: those papers are
        # dropped by _is_rankable, and the caller reports that separately.
        if all(p.relevance_score == 0.0 for p in rankable):
            raise RuntimeError(
                f"Ranking anomaly: all {len(rankable)} papers came back scored "
                "0.0, which is what a failed parse or a degraded API looks like "
                "— not a judgement the model actually made."
            )
        logger.info(
            "Ranking: %d scored, none reached the cutoff of %d (best was %.1f) "
            "— a quiet month, not a fault",
            len(rankable), cutoff,
            max(p.relevance_score for p in rankable),
        )

    # cfg.top_n is None when the member set no limit — keep everything that
    # cleared the cutoff. The cutoff, not a count, is what bounds this.
    top = qualified if cfg.top_n is None else qualified[: cfg.top_n]
    logger.info(
        "Ranking: %d qualified, returning top %d",
        len(qualified),
        len(top),
    )
    return top
