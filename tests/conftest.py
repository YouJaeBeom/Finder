"""Shared pytest fixtures for paper_digest tests."""
from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import List

import pytest
import yaml

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

    # Block connecting, not the socket class itself. Replacing the class breaks
    # any module that subclasses it (PySocks does, lazily, the first time a
    # request is built), which surfaces as a baffling TypeError far from here.
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
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
        source = ["conference"]
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
        venue="ACL",
        venue_status="published",
        collection_date=datetime.now(timezone.utc).date().isoformat(),
        source=source,
        matched_keywords=["LLM", "RLHF"],
        relevance_score=score,
    )


# ── Collector stand-in ─────────────────────────────────────────────────────────

def venue_collector(conference: List[Paper] = (), journal: List[Paper] = ()):
    """A stand-in for ``collect_venue_papers`` that answers per venue class.

    The pipeline calls one collector twice — once for conferences, once for
    journals — so a single ``return_value`` would hand the same papers to both
    and the dedup stage would quietly halve them again. Keying on
    *source_label* is what makes a test able to say "conferences returned these,
    journals returned nothing".
    """
    by_label = {"conference": list(conference), "journal": list(journal)}

    def _collect(venues, days_back=7, max_results=500,
                 source_label="conference", exact_venue_match=False):
        return by_label.get(source_label, [])

    return _collect


# ── Lab config fixtures ────────────────────────────────────────────────────────

# The keyword set and profile every pipeline test used before the lab split. It
# now lives in a member file rather than in config.yaml, which is the whole point
# of the split — but the terms are unchanged so the tests' expectations about what
# matches are still the expectations they were written with.
# preflight requires a real Notion ID, so the fixtures use one. The raw form is
# what a user pastes; the dashed form is what parent_page_id() normalizes it to
# and therefore what the fake Notion is keyed on.
PARENT_PAGE_RAW = "3bc1256e056180898608c39506c43463"
PARENT_PAGE_ID = "3bc1256e-0561-8089-8608-c39506c43463"

DEFAULT_KEYWORDS = [
    "large language model", "LLM", "RLHF", "alignment", "transformer",
    "retrieval augmented generation", "RAG", "instruction tuning",
    "chain of thought", "reasoning",
]

DEFAULT_PROFILE = (
    "내 연구는 대형 언어 모델(LLM)의 정렬(alignment)과 안전성에 초점을 맞추고 있습니다.\n"
    "특히 RLHF(인간 피드백을 통한 강화 학습)와 지시 추종에 관심이 있습니다.\n"
)

LAB_CONFIG_TEMPLATE = """\
notion_parent_page_id: "{parent}"
members_dir: "{members_dir}"
limits:
  max_members: {max_members}
  max_top_n_per_member: {max_top_n}
  max_notes_per_run: {max_notes}
conferences:
  enabled: {conferences}
  min_score: 0.5
journals:
  enabled: {journals}
  min_score: 0.5
days_back: {days_back}
max_papers_to_rank: 1500
llm:
  provider: "anthropic"
  ranking_model: "claude-haiku-4-5"
  notes_model: "claude-opus-5"
news:
  enabled: {news}
  top_n: {news_top_n}
"""


def write_member(
    directory,
    member_id: str,
    name: str = None,
    top_n: int = 10,
    keywords=None,
    profile: str = None,
    enabled: bool = True,
) -> Path:
    """Write one member YAML and return its path."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": name or member_id,
        "enabled": enabled,
        "top_n": top_n,
        "research_profile": profile or DEFAULT_PROFILE,
        "keywords": list(DEFAULT_KEYWORDS if keywords is None else keywords),
    }
    path = directory / f"{member_id}.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def write_lab_config(
    tmp_path,
    members=(("jaebeom", "유재범", 10),),
    parent: str = PARENT_PAGE_RAW,
    days_back: int = 7,
    conferences: bool = True,
    journals: bool = True,
    news: bool = False,
    news_top_n: int = 5,
    max_members: int = 15,
    max_top_n: int = 30,
    max_notes: int = 400,
) -> str:
    """Write a lab config plus its member files, and return the config path.

    *members* is a sequence of ``(member_id, name, top_n)``. The members
    directory is written as an absolute path: it is resolved relative to the
    process's working directory, and tests that chdir would otherwise find an
    empty lab.
    """
    members_dir = Path(tmp_path) / "members"
    for member_id, name, top_n in members:
        write_member(members_dir, member_id, name=name, top_n=top_n)

    cfg_file = Path(tmp_path) / "config.yaml"
    cfg_file.write_text(
        LAB_CONFIG_TEMPLATE.format(
            parent=parent,
            members_dir=str(members_dir),
            days_back=days_back,
            conferences=str(bool(conferences)).lower(),
            journals=str(bool(journals)).lower(),
            news=str(bool(news)).lower(),
            news_top_n=news_top_n,
            max_members=max_members,
            max_top_n=max_top_n,
            max_notes=max_notes,
        ),
        encoding="utf-8",
    )
    return str(cfg_file)


@pytest.fixture()
def sample_config(tmp_path):
    """A one-member lab: config.yaml plus members/jaebeom.yaml."""
    return write_lab_config(tmp_path)


@pytest.fixture()
def fake_notion(monkeypatch):
    """A Notion workspace in memory, wired into paper_digest.notion_api."""
    from tests.notion_fake import FakeNotion

    return FakeNotion(PARENT_PAGE_ID).install(monkeypatch)


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
                source=["conference"],
            )
        )
    return papers
