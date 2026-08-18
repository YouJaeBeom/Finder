"""End-to-end weekly pipeline, against an in-memory Notion.

Every external service is a stand-in: Semantic Scholar, the LLM, and Notion (see
:mod:`tests.notion_fake`, which keeps real state rather than replaying canned
payloads). What is exercised is the whole run — collect once, write news once,
then serve each member into their own database.

The properties these tests are here to hold:

* each member's papers land in *their* database, never in someone else's
* one member's failure does not cost the others their digest
* news is written once, to the main page, not once per member
* a second run creates nothing new, because Notion is the source of truth
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.models import Paper, PaperIdentifiers, normalize_title
from paper_digest.notion_writer import NEWS_DB_TITLE, PAPERS_DB_TITLE
from tests.conftest import (
    PARENT_PAGE_ID,
    PARENT_PAGE_RAW,
    venue_collector,
    write_lab_config,
    write_member,
)

# The dashed ID Notion is addressed by, and the raw form the config carries.
ROOT = PARENT_PAGE_ID


# ── Fixtures for the pool ──────────────────────────────────────────────────────

# Each entry is (title, abstract, which member's keywords it is written to hit).
# Keeping that mapping explicit is what lets a test assert "this paper belongs to
# exactly one member" without guessing at the keyword rules.
_TOPICS = [
    ("Political Bias in Large Language Models",
     "We measure political bias and ideological bias in large language models.", "pol"),
    ("Sycophancy under Persona Prompting",
     "Sycophancy in LLM responses when a persona is assigned; political stance shifts.", "pol"),
    ("Prompt Sensitivity of Political Stance",
     "Prompt sensitivity and prompt robustness of political leaning in a language model.", "pol"),
    ("Measuring Partisan Bias with Benchmarks",
     "A benchmark to measure bias in a large language model along partisan lines.", "pol"),
    ("Filter Bubbles in Personalized Search",
     "Filter bubble effects of personalized search and viewpoint diversity in ranking.", "ir"),
    ("Retrieval Augmented Generation Diversity",
     "Retrieval-augmented generation and information diversity in a retriever.", "ir"),
    ("Echo Chamber Effects in Recommenders",
     "Echo chamber and selective exposure in recommendation and information retrieval.", "ir"),
    ("Viewpoint Diversity in Conversational Search",
     "Conversational search with viewpoint diversity and result diversification.", "ir"),
    ("A Web Crawling Pipeline at Scale",
     "Web crawling and a large-scale distributed data collection pipeline.", "web"),
    ("Corpus Construction from Common Crawl",
     "Corpus construction from common crawl with a scalable crawling pipeline.", "web"),
    ("Reproducible Social Media Data Collection",
     "Reproducible data collection from social media via the twitter API and sampling.", "web"),
    ("Web Corpus Provenance",
     "Data provenance and dataset curation for a web corpus built by a web crawler.", "web"),
]

# The keyword sets the tests register members with — narrow on purpose so a
# paper written for one member cannot drift into another's.
KEYWORDS = {
    "pol": ["political bias", "ideological bias", "sycophancy", "political stance",
            "political leaning", "partisan", "prompt sensitivity"],
    "ir": ["filter bubble", "echo chamber", "retrieval-augmented generation",
           "information diversity", "viewpoint diversity", "selective exposure",
           "result diversification", "personalized search"],
    "web": ["web crawling", "web crawler", "common crawl", "corpus construction",
            "web corpus", "data provenance", "dataset curation",
            {"all": [["large-scale", "large scale", "scalable"],
                     ["data collection", "crawling pipeline"]]},
            {"all": [["reproducible"], ["data collection"]]}],
}


def _paper(title: str, abstract: str, source: str = "conference",
           idx: int = 0) -> Paper:
    """One collected paper.

    ``normalized_title`` is derived from the *same* title that goes into Notion.
    A fixture that let those drift would silently disable the cross-run dedup
    these tests check, since the written index matches on the normalized title.
    """
    return Paper(
        identifiers=PaperIdentifiers(
            doi=f"10.18653/{source}/{idx}",
            normalized_title=normalize_title(title),
        ),
        title=title,
        abstract=abstract,
        authors=["Test Author A", "Test Author B"],
        venue="ACL" if source == "conference" else "TOIS",
        venue_status="published",
        collection_date=datetime.now(timezone.utc).date().isoformat(),
        source=[source],
        url=f"https://doi.org/10.18653/{source}/{idx}",
        published_at="2026-08-01",
    )


def pool_papers() -> List[Paper]:
    return [_paper(t, a, "conference" if i % 2 == 0 else "journal", i)
            for i, (t, a, _) in enumerate(_TOPICS)]


def papers_for(tag: str) -> List[Paper]:
    return [p for p, (_, _, t) in zip(pool_papers(), _TOPICS) if t == tag]


def _note_json() -> str:
    return json.dumps({
        "one_line_summary": "이 논문은 대형 언어 모델의 정치적 편향을 체계적으로 측정합니다.",
        "key_contributions": [
            "정치적 편향을 측정하는 새로운 벤치마크 설계",
            "페르소나에 따른 응답 변화를 정량화",
            "다국어 환경에서의 편향 차이 확인",
        ],
        "method": "여러 프롬프트 템플릿으로 동일한 정치적 질문을 반복 질의하고, "
                  "응답의 이념 좌표를 사람 평가와 비교해 검증합니다.",
        "relevance_to_profile": "내 연구의 핵심인 LLM 정치적 편향 측정과 직접 맞닿아 있고, "
                                "프롬프트 민감도 분석 방법을 그대로 차용할 수 있습니다. "
                                "다만 영어 단일 언어 실험이라 다국어 확장은 추가 작업입니다.",
    })


def fake_llm() -> MagicMock:
    """A provider that ranks everything highly and returns one valid note."""
    def complete(prompt, model, max_tokens=512, system=None):
        if max_tokens <= 512:  # ranking
            return json.dumps([{"id": str(i), "score": 9} for i in range(20)])
        return _note_json()

    provider = MagicMock()
    provider.complete.side_effect = complete
    return provider


# ── Runner ─────────────────────────────────────────────────────────────────────

@pytest.fixture()
def lab(tmp_path, monkeypatch, fake_notion):
    """A three-member lab in a temp cwd, with an in-memory Notion behind it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    members_dir = tmp_path / "members"
    write_member(members_dir, "pol", name="유재범", top_n=5,
                 keywords=KEYWORDS["pol"])
    write_member(members_dir, "ir", name="샘플-검색RAG", top_n=5,
                 keywords=KEYWORDS["ir"])
    write_member(members_dir, "web", name="샘플-웹데이터", top_n=5,
                 keywords=KEYWORDS["web"])

    config_path = write_lab_config(
        tmp_path,
        members=(),  # member files written above, with their own keyword sets
        parent=PARENT_PAGE_RAW,
        days_back=30,
    )
    return {"config": config_path, "notion": fake_notion, "tmp": tmp_path}


def run_weekly_mocked(lab, papers=None, news=None):
    """Run the weekly pipeline and return ``(exit_code, report)``."""
    papers = pool_papers() if papers is None else papers
    conference = [p for p in papers if p.source == ["conference"]]
    journal = [p for p in papers if p.source == ["journal"]]

    news_patches = []
    if news is not None:
        news_patches = [
            patch("paper_digest.news_stage.collect_hackernews_stories",
                  return_value=news),
            patch("paper_digest.news_stage.collect_rss_entries", return_value=[]),
        ]

    from paper_digest.pipeline import run_weekly

    with patch("paper_digest.pipeline.collect_venue_papers",
               side_effect=venue_collector(conference=conference,
                                           journal=journal)), \
         patch("paper_digest.pipeline.create_provider", return_value=fake_llm()):
        for p in news_patches:
            p.start()
        try:
            exit_code = run_weekly(lab["config"])
        finally:
            for p in news_patches:
                p.stop()

    report_path = Path("run-report.json")
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return exit_code, report


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestPerMemberDelivery:
    def test_run_succeeds(self, lab):
        exit_code, report = run_weekly_mocked(lab)
        assert exit_code == 0, report.get("error")

    def test_every_member_gets_their_own_page_and_database(self, lab):
        run_weekly_mocked(lab)
        notion = lab["notion"]

        for name in ("유재범", "샘플-검색RAG", "샘플-웹데이터"):
            page_id = notion.page_named(name, under=ROOT)
            assert page_id, f"no member page for {name}"
            assert notion.database_named(PAPERS_DB_TITLE, under=page_id), (
                f"no papers database inside {name}'s page"
            )

        # Three member databases, and no shared one on the root page.
        member_dbs = [db for db in notion.databases.values()
                      if db["title"] == PAPERS_DB_TITLE]
        assert len(member_dbs) == 3
        assert notion.database_named(PAPERS_DB_TITLE, under=ROOT) is None

    def test_a_members_database_holds_only_their_papers(self, lab):
        run_weekly_mocked(lab)
        notion = lab["notion"]

        for member_name, tag in (("유재범", "pol"), ("샘플-검색RAG", "ir"),
                                 ("샘플-웹데이터", "web")):
            page_id = notion.page_named(member_name, under=ROOT)
            db_id = notion.database_named(PAPERS_DB_TITLE, under=page_id)
            written = set(notion.titles_in(db_id))
            expected = {p.title for p in papers_for(tag)}
            assert written == expected, (
                f"{member_name} got {written - expected} extra and "
                f"{expected - written} missing"
            )

    def test_report_breaks_the_totals_down_per_member(self, lab):
        _, report = run_weekly_mocked(lab)
        rows = {r["member_id"]: r for r in report["members"]}
        assert set(rows) == {"pol", "ir", "web"}
        for row in rows.values():
            assert row["candidates"] == 4
            assert row["created"] == 4
            assert row["error"] is None
        assert report["pages_created"] == 12

    def test_top_n_caps_what_a_member_receives(self, lab):
        # One member's cap is lowered below their candidate count.
        write_member(lab["tmp"] / "members", "pol", name="유재범", top_n=2,
                     keywords=KEYWORDS["pol"])
        _, report = run_weekly_mocked(lab)
        rows = {r["member_id"]: r for r in report["members"]}
        assert rows["pol"]["candidates"] == 4
        assert rows["pol"]["created"] == 2
        assert rows["ir"]["created"] == 4  # unaffected


class TestFaultIsolation:
    def test_one_members_notion_failure_does_not_cost_the_others(self, lab):
        real_ensure = None

        def failing_ensure(parent, member_id, member_name, token):
            if member_id == "ir":
                raise RuntimeError("Notion said no")
            return real_ensure(parent, member_id, member_name, token)

        import paper_digest.pipeline as pipeline
        real_ensure = pipeline.ensure_member_space

        with patch.object(pipeline, "ensure_member_space",
                          side_effect=failing_ensure):
            exit_code, report = run_weekly_mocked(lab)

        rows = {r["member_id"]: r for r in report["members"]}
        assert exit_code == 1, "a failed member must fail the run"
        assert "Notion said no" in rows["ir"]["error"]
        # The other two were still served.
        assert rows["pol"]["created"] == 4
        assert rows["web"]["created"] == 4
        assert "ir" in report["error"] or "샘플-검색RAG" in report["error"]

    def test_a_member_with_no_candidates_is_not_a_failure(self, lab):
        # Only the IR member's papers are collected; the other two match nothing.
        exit_code, report = run_weekly_mocked(lab, papers=papers_for("ir"))
        rows = {r["member_id"]: r for r in report["members"]}
        assert exit_code == 0
        assert rows["pol"]["candidates"] == 0
        assert rows["pol"]["error"] is None
        assert rows["ir"]["created"] == 4


class TestCrossRunDeduplication:
    def test_a_second_run_writes_nothing_new(self, lab):
        _, first = run_weekly_mocked(lab)
        assert first["pages_created"] == 12

        _, second = run_weekly_mocked(lab)
        assert second["pages_created"] == 0
        assert all(r["new"] == 0 for r in second["members"])

    def test_notion_alone_is_enough_to_prevent_duplicates(self, lab):
        """The scoring cache is an optimization, not the safety net.

        This is the 2026-08-15 failure reproduced: the local state file is lost
        between runs. Before the truth layer moved into Notion, that produced a
        duplicate page for every paper.
        """
        run_weekly_mocked(lab)

        scored = Path("state/scored")
        assert list(scored.glob("*.json")), "expected a scoring cache to exist"
        for cache in scored.glob("*.json"):
            cache.unlink()

        _, second = run_weekly_mocked(lab)
        assert second["pages_created"] == 0

    def test_losing_state_json_does_not_duplicate_the_databases(self, lab):
        run_weekly_mocked(lab)
        db_count = len(lab["notion"].databases)

        Path("state.json").unlink()

        run_weekly_mocked(lab)
        assert len(lab["notion"].databases) == db_count, (
            "databases were re-created after state.json was lost"
        )


class TestOverlapReporting:
    def test_a_paper_two_members_receive_is_reported_as_overlap(self, lab):
        # Give the IR member a keyword that also hits a political-bias paper.
        write_member(lab["tmp"] / "members", "ir", name="샘플-검색RAG", top_n=5,
                     keywords=KEYWORDS["ir"] + ["political bias"])
        _, report = run_weekly_mocked(lab)

        overlap = report["overlap"]
        assert len(overlap) == 1
        assert overlap[0]["title"] == "Political Bias in Large Language Models"
        assert sorted(overlap[0]["members"]) == ["샘플-검색RAG", "유재범"]

    def test_nothing_shared_means_no_overlap(self, lab):
        _, report = run_weekly_mocked(lab)
        assert report["overlap"] == []


class TestNews:
    def test_news_is_written_once_to_the_main_page(self, lab, sample_news):
        run_weekly_mocked_with_news(lab, sample_news)
        notion = lab["notion"]

        news_db = notion.database_named(NEWS_DB_TITLE, under=ROOT)
        assert news_db, "news database should sit on the main page"
        assert len(notion.titles_in(news_db)) == 2

        # Not duplicated into anyone's paper database.
        for name in ("유재범", "샘플-검색RAG", "샘플-웹데이터"):
            page_id = notion.page_named(name, under=ROOT)
            member_db = notion.database_named(PAPERS_DB_TITLE, under=page_id)
            assert notion.database_named(NEWS_DB_TITLE, under=page_id) is None
            for title in notion.titles_in(member_db):
                assert "OpenAI" not in title

    def test_news_is_not_rewritten_on_the_next_run(self, lab, sample_news):
        run_weekly_mocked_with_news(lab, sample_news)
        news_db = lab["notion"].database_named(NEWS_DB_TITLE, under=ROOT)
        assert len(lab["notion"].titles_in(news_db)) == 2

        run_weekly_mocked_with_news(lab, sample_news)
        assert len(lab["notion"].titles_in(news_db)) == 2


@pytest.fixture()
def sample_news() -> List[Paper]:
    stories = [
        ("OpenAI ships a new reasoning model",
         "The company says the model improves on benchmarks. AI news."),
        ("OpenAI faces a copyright suit over training data",
         "Publishers allege their content was used as training data for an LLM."),
    ]
    out = []
    for i, (title, summary) in enumerate(stories):
        url = f"https://news.example.com/{i}"
        out.append(Paper(
            identifiers=PaperIdentifiers(normalized_title=normalize_title(title),
                                         url=url),
            title=title,
            abstract=summary,
            venue="Hacker News",
            content_type="news",
            collection_date=datetime.now(timezone.utc).date().isoformat(),
            source=["hackernews"],
            url=url,
            points=250 - i,
            published_at="2026-08-18",
        ))
    return out


def run_weekly_mocked_with_news(lab, news):
    """Same runner, with news enabled in the config."""
    cfg_path = Path(lab["config"])
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8")
        .replace("enabled: false", "enabled: true"),
        encoding="utf-8",
    )
    return run_weekly_mocked(lab, news=news)


class TestSourceToggles:
    """Which venue classes get collected, and that a toggle is honoured."""

    def _collect_calls(self, lab, conferences=True, journals=True):
        write_lab_config(
            lab["tmp"], members=(), parent=PARENT_PAGE_RAW, days_back=30,
            conferences=conferences, journals=journals,
        )
        collector = MagicMock(return_value=[])
        from paper_digest.pipeline import run_weekly

        with patch("paper_digest.pipeline.collect_venue_papers", collector), \
             patch("paper_digest.pipeline.create_provider",
                   return_value=fake_llm()):
            run_weekly(lab["config"])

        return {call.kwargs["source_label"] for call in collector.call_args_list}

    def test_both_by_default(self, lab):
        assert self._collect_calls(lab) == {"conference", "journal"}

    def test_journals_off_leaves_conferences_alone(self, lab):
        assert self._collect_calls(lab, journals=False) == {"conference"}

    def test_conferences_off_leaves_journals_alone(self, lab):
        assert self._collect_calls(lab, conferences=False) == {"journal"}

    def test_both_off_collects_nothing(self, lab):
        assert self._collect_calls(lab, conferences=False, journals=False) == set()


class TestBudgetRefusal:
    def test_a_run_over_the_note_limit_never_collects(self, lab):
        write_lab_config(lab["tmp"], members=(), parent=PARENT_PAGE_RAW, days_back=30,
                         max_notes=5)   # three members at top_n 5 = 15
        collector = MagicMock(return_value=[])
        from paper_digest.pipeline import run_weekly

        with patch("paper_digest.pipeline.collect_venue_papers", collector):
            exit_code = run_weekly(lab["config"])

        assert exit_code == 1
        collector.assert_not_called()
        report = json.loads(Path("run-report.json").read_text())
        assert "max_notes_per_run" in report["error"]


class TestSingleMemberRun:
    def test_only_flag_serves_one_member(self, lab):
        from paper_digest.pipeline import run_weekly

        papers = pool_papers()
        with patch("paper_digest.pipeline.collect_venue_papers",
                   side_effect=venue_collector(
                       conference=[p for p in papers if p.source == ["conference"]],
                       journal=[p for p in papers if p.source == ["journal"]])), \
             patch("paper_digest.pipeline.create_provider",
                   return_value=fake_llm()):
            exit_code = run_weekly(lab["config"], only="web")

        assert exit_code == 0
        report = json.loads(Path("run-report.json").read_text())
        assert [r["member_id"] for r in report["members"]] == ["web"]
        assert lab["notion"].page_named("유재범", under=ROOT) is None

    def test_an_unknown_member_id_is_an_error(self, lab):
        from paper_digest.pipeline import run_weekly

        assert run_weekly(lab["config"], only="nobody") == 1
        report = json.loads(Path("run-report.json").read_text())
        assert "nobody" in report["error"]
