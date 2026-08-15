"""Shared pytest fixtures for paper_digest tests."""
from __future__ import annotations

import socket
import textwrap
from datetime import datetime, timezone
from typing import List

import pytest

from paper_digest.models import Paper, PaperIdentifiers, normalize_title


# ── Network guard ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any test that reaches the network instead of quietly calling it.

    Added after a gap in the Notion mocks let the suite make live calls to
    api.notion.com. A mocked test that silently starts hitting a real API is
    slow, flaky, and — with a token in the environment — capable of writing to
    someone's real workspace.
    """
    def blocked(*args, **kwargs):
        raise AssertionError(
            "This test attempted a real network connection. Mock the request "
            "instead — patch paper_digest.<module>.requests.{get,post,patch}."
        )

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


# ── Sample paper factory ───────────────────────────────────────────────────────

def make_paper(
    arxiv_id: str = None,
    doi: str = None,
    title: str = "Test Paper on LLM Alignment",
    abstract: str = "This paper studies large language model alignment via RLHF.",
    source: List[str] = None,
    score: float = 0.0,
) -> Paper:
    if source is None:
        source = ["arxiv"]
    norm = normalize_title(title)
    return Paper(
        identifiers=PaperIdentifiers(
            arxiv_id=arxiv_id,
            doi=doi,
            normalized_title=norm,
        ),
        title=title,
        abstract=abstract,
        authors=["Alice Smith", "Bob Jones"],
        venue="arXiv preprint",
        venue_status="preprint",
        collection_date=datetime.now(timezone.utc).date().isoformat(),
        source=source,
        matched_keywords=["LLM", "RLHF"],
        relevance_score=score,
    )


# ── Minimal config fixture ─────────────────────────────────────────────────────

@pytest.fixture()
def sample_config(tmp_path):
    """Write a minimal config.yaml and return its path."""
    cfg_content = textwrap.dedent("""\
        notion_parent_page_id: "3bc1256e05618089aaaabbbbccccdddd"
        keywords:
          - "large language model"
          - "LLM"
          - "RLHF"
          - "alignment"
          - "transformer"
          - "retrieval augmented generation"
          - "RAG"
          - "instruction tuning"
          - "chain of thought"
          - "reasoning"
        tracked_venues:
          - "ACL 2026"
        research_profile: |
          내 연구는 대형 언어 모델(LLM)의 정렬(alignment)과 안전성에 초점을 맞추고 있습니다.
          특히 RLHF(인간 피드백을 통한 강화 학습)와 지시 추종에 관심이 있습니다.
        arxiv:
          enabled: true
          categories: [cs.CL, cs.AI, cs.LG]
        days_back: 7
        max_papers_to_rank: 1500
        top_n: 10
        llm:
          provider: "anthropic"
          ranking_model: "claude-haiku-4-5"
          notes_model: "claude-opus-5"
    """)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(cfg_content, encoding="utf-8")
    return str(cfg_file)


@pytest.fixture()
def sample_paper():
    return make_paper(arxiv_id="2401.00001")


@pytest.fixture()
def sample_papers():
    """Return a list of 15 test papers with distinct IDs and varied abstracts."""
    topics = [
        ("LLM Alignment via RLHF", "This paper proposes a new RLHF method for large language model alignment."),
        ("Chain of Thought Prompting", "We study chain of thought reasoning in LLMs and improve instruction tuning."),
        ("RAG for Factual QA", "Retrieval augmented generation improves factuality in LLM responses."),
        ("Transformer Efficiency", "Efficient transformer architectures for large language model pretraining."),
        ("RLHF with Sparse Rewards", "Reinforcement learning from human feedback with sparse reward signals."),
        ("Hallucination Reduction", "Methods to reduce hallucination in LLMs via alignment fine-tuning."),
        ("Code Generation LLM", "A large language model fine-tuned for code generation and reasoning."),
        ("Instruction Following", "Improving instruction following capabilities via fine-tuning with RLHF."),
        ("In-Context Learning", "In-context learning dynamics and meta-learning in large language models."),
        ("Prompt Engineering", "Systematic study of prompt engineering for LLM alignment tasks."),
        ("LLM Reasoning Chains", "Enhancing LLM reasoning via chain of thought and alignment methods."),
        ("Multilingual LLMs", "Multilingual instruction tuning for large language models across 50 languages."),
        ("RLHF Reward Modeling", "Reward model design for RLHF in large language model training."),
        ("Factual Grounding RAG", "Retrieval augmented generation with factual grounding for LLM outputs."),
        ("Safety in LLMs", "Constitutional AI and safety techniques for aligning large language models."),
    ]
    papers = []
    for i, (title, abstract) in enumerate(topics):
        papers.append(
            make_paper(
                arxiv_id=f"2401.{i:05d}",
                doi=f"10.18653/test/{i}",
                title=title,
                abstract=abstract,
                source=["arxiv"],
            )
        )
    return papers
