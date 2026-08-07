"""Korean research note generation using the high-performance LLM.

Each note has four sections that map to the ResearchNote dataclass:
  1. 한 줄 요약   (one_line_summary)
  2. 핵심 기여   (key_contributions, 3 items)
  3. 방법        (method)
  4. 내 연구와의 연결점 (relevance_to_profile)

The 연결점 section explicitly contrasts the paper against the configured
research profile — it must not merely restate the abstract.
"""
from __future__ import annotations

import json
import logging
import re
from typing import List, Optional

from .config import Config
from .llm.base import LLMProvider
from .models import Paper, ResearchNote

logger = logging.getLogger(__name__)

_NOTES_SYSTEM = (
    "당신은 한국어로 연구 노트를 작성하는 전문가입니다. "
    "주어진 논문에 대해 깊이 있고 통찰력 있는 한국어 연구 노트를 작성하세요. "
    "응답은 반드시 유효한 JSON만 반환하십시오."
)

_NOTES_PROMPT_TEMPLATE = """\
다음 논문에 대한 한국어 연구 노트를 작성해주세요.

논문 제목: {title}
저자: {authors}

초록:
{abstract}

연구자 프로필:
{profile}

아래 JSON 형식으로 반환하세요 (반드시 한국어로 작성):

{{
  "one_line_summary": "논문의 핵심을 한 문장으로 요약",
  "key_contributions": [
    "첫 번째 핵심 기여",
    "두 번째 핵심 기여",
    "세 번째 핵심 기여"
  ],
  "method": "사용된 방법론과 접근 방식에 대한 상세 설명 (2-4문장)",
  "relevance_to_profile": "이 논문이 연구자 프로필과 어떻게 연결되고 차이가 있는지 구체적으로 설명 — 단순히 초록을 재진술하지 말고, 연구자의 현재 연구와의 유사점, 차이점, 활용 가능성을 논의 (3-5문장)"
}}
"""


def _parse_note(raw: str) -> Optional[ResearchNote]:
    """Parse LLM JSON response into a ResearchNote."""
    # Strip markdown fences
    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON object from text
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                logger.warning("Could not parse note JSON from LLM response")
                return None
        else:
            logger.warning("No JSON object found in note response")
            return None

    note = ResearchNote(
        one_line_summary=data.get("one_line_summary", ""),
        key_contributions=data.get("key_contributions", []),
        method=data.get("method", ""),
        relevance_to_profile=data.get("relevance_to_profile", ""),
    )

    # Ensure key_contributions has exactly 3 items
    while len(note.key_contributions) < 3:
        note.key_contributions.append("")
    note.key_contributions = note.key_contributions[:3]

    return note


def generate_note(paper: Paper, cfg: Config, provider: LLMProvider) -> ResearchNote:
    """Generate a Korean research note for one paper."""
    authors_str = ", ".join(paper.authors[:5]) if paper.authors else "Unknown"
    abstract = (paper.abstract or "")[:2000]  # Truncate very long abstracts

    prompt = _NOTES_PROMPT_TEMPLATE.format(
        title=paper.title,
        authors=authors_str,
        abstract=abstract,
        profile=cfg.research_profile,
    )

    raw = provider.complete(
        prompt=prompt,
        model=cfg.llm.notes_model,
        max_tokens=2048,
        system=_NOTES_SYSTEM,
    )

    note = _parse_note(raw)
    if note is None:
        # Fallback: create a minimal note from the title
        logger.warning("Note generation failed for '%s'; using fallback", paper.title)
        note = ResearchNote(
            one_line_summary=f"[LLM 응답 파싱 실패] {paper.title}",
            key_contributions=["(내용 없음)", "(내용 없음)", "(내용 없음)"],
            method="(내용 없음)",
            relevance_to_profile="(내용 없음)",
        )

    return note


def generate_notes(
    papers: List[Paper],
    cfg: Config,
    provider: LLMProvider,
) -> None:
    """Generate Korean research notes for all papers (in-place mutation)."""
    for i, paper in enumerate(papers, 1):
        logger.info("Generating note %d/%d: %s", i, len(papers), paper.title[:60])
        paper.research_note = generate_note(paper, cfg, provider)
