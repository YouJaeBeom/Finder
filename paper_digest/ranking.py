"""Two-tier LLM relevance ranking pipeline.

Bulk-ranks candidate papers using the cheap ranking model, returns the top-N
papers sorted by descending score.  Papers with no abstract are excluded.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List

from .config import Config
from .llm.base import LLMProvider
from .models import Paper

logger = logging.getLogger(__name__)

_BATCH_SIZE = 20  # papers per LLM ranking call
_MIN_SCORE = 5  # papers below this threshold are not included in top-N

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


def _build_papers_block(papers: List[Paper]) -> str:
    """Serialize papers for the ranking prompt."""
    lines = []
    for i, p in enumerate(papers):
        abstract_preview = (p.abstract or "")[:500]
        lines.append(
            f"[{i}] Title: {p.title}\n"
            f"    Abstract: {abstract_preview}"
        )
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

    Papers with no abstract are excluded before ranking.
    If candidates > 0 but all score below *_MIN_SCORE*, the function raises
    RuntimeError to signal a ranking anomaly (pipeline should exit 1).
    """
    # Exclude papers with no abstract
    rankable = [p for p in papers if p.abstract]

    if not rankable:
        logger.info("No papers with abstracts to rank")
        return []

    # Truncate to max_papers_to_rank
    if len(rankable) > cfg.max_papers_to_rank:
        logger.info(
            "Truncating from %d to %d papers for ranking",
            len(rankable),
            cfg.max_papers_to_rank,
        )
        rankable = rankable[: cfg.max_papers_to_rank]

    # Rank in batches
    for i in range(0, len(rankable), _BATCH_SIZE):
        batch = rankable[i : i + _BATCH_SIZE]
        papers_block = _build_papers_block(batch)
        prompt = _RANKING_PROMPT_TEMPLATE.format(
            profile=cfg.research_profile,
            papers_block=papers_block,
        )

        try:
            raw = provider.complete(
                prompt=prompt,
                model=cfg.llm.ranking_model,
                max_tokens=512,
                system=_RANKING_SYSTEM,
            )
        except Exception as exc:
            logger.error("LLM ranking failed for batch %d: %s", i // _BATCH_SIZE, exc)
            raise

        scores = _parse_scores(raw, len(batch))
        for paper, score in zip(batch, scores):
            paper.relevance_score = score

    # Filter and sort
    qualified = [p for p in rankable if p.relevance_score >= _MIN_SCORE]
    qualified.sort(key=lambda p: p.relevance_score, reverse=True)

    if not qualified and rankable:
        raise RuntimeError(
            "Ranking anomaly: keyword candidates exist but LLM ranked 0 papers "
            f"above the minimum score of {_MIN_SCORE}."
        )

    top = qualified[: cfg.top_n]
    logger.info(
        "Ranking: %d qualified, returning top %d",
        len(qualified),
        len(top),
    )
    return top
