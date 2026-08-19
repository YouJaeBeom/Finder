"""IT news collection, ranking, note shape and the news pipeline stage.

These tests call the news stage directly rather than going through run_monthly,
and never read run-report.json — the pipeline writes that file CWD-relative, so
report-based assertions are order-sensitive across tests.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from paper_digest.collectors.hackernews import _story_to_paper
from paper_digest.collectors.rss import _strip_html
from paper_digest.config import Config, NewsConfig
from paper_digest.models import Paper, PaperIdentifiers, ResearchNote, normalize_title
from paper_digest.news_select import select_news
from paper_digest.notes import generate_note
from paper_digest.notion_query import WrittenIndex
from paper_digest.news_stage import run_news
from paper_digest.ranking import _is_rankable


NEWS_DB = "news-db"


def _run_news_with(cfg, provider, already_written=(), members=()):
    """Call the news stage with a stand-in for what the news database holds.

    The stage asks Notion what it already has rather than trusting a file — see
    paper_digest.notion_query — so a test controls that by supplying the index.
    """
    index = WrittenIndex()
    for item in already_written:
        index.add(item)
    with patch("paper_digest.news_stage.written_index", return_value=index):
        return run_news(cfg, provider, NEWS_DB, None, members)


def _news_item(
    title: str,
    url: str,
    summary: str | None = None,
    venue: str = "Hacker News",
    points: int | None = None,
) -> Paper:
    return Paper(
        identifiers=PaperIdentifiers(
            arxiv_id=None, doi=None, normalized_title=normalize_title(title), url=url
        ),
        title=title,
        abstract=summary,
        venue=venue,
        venue_status="published",
        collection_date="2026-08-15",
        source=["hackernews"],
        content_type="news",
        url=url,
        points=points,
    )


# ── Collectors ────────────────────────────────────────────────────────────────

class TestHackerNewsCollector:
    def test_story_becomes_news_item(self):
        paper = _story_to_paper(
            {"type": "story", "title": "Anthropic ships X",
             "url": "https://example.com/a", "by": "alice", "score": 300},
            "2026-08-15",
        )
        assert paper is not None
        assert paper.content_type == "news"
        assert paper.venue == "Hacker News"
        assert paper.identifiers.url == "https://example.com/a"
        # HN serves no article body; it must stay None rather than be invented.
        assert paper.abstract is None

    def test_text_post_without_url_is_skipped(self):
        """Ask HN / Show HN text posts have no article and no identity key."""
        assert _story_to_paper(
            {"type": "story", "title": "Ask HN: what do you use?", "by": "bob"},
            "2026-08-15",
        ) is None


class TestRSSCollector:
    def test_strip_html_unescapes_and_collapses(self):
        assert _strip_html("<p>Hello &amp;   <b>world</b></p>") == "Hello & world"

    def test_strip_html_handles_empty(self):
        assert _strip_html("") == ""


# ── Selection (no LLM) ────────────────────────────────────────────────────────

class TestNewsSelection:
    def test_paper_ranking_still_requires_an_abstract(self):
        """The LLM gate is now papers-only, and it still needs a body to judge."""
        paper = Paper(identifiers=PaperIdentifiers(), title="T", abstract=None)
        assert _is_rankable(paper) is False

    def test_empty_keywords_keeps_everything(self):
        """Clearing the list is the documented way to say 'summarise it all'."""
        items = [_news_item(f"Story {i}", f"https://e.com/{i}") for i in range(3)]
        assert len(select_news(items, keywords=[], top_n=10)) == 3

    def test_keywords_filter_and_record_what_matched(self):
        items = [
            _news_item("New LLM benchmark", "https://e.com/1"),
            _news_item("Sourdough starter tips", "https://e.com/2"),
        ]
        selected = select_news(items, keywords=["LLM"], top_n=10)
        assert [item.title for item in selected] == ["New LLM benchmark"]
        assert selected[0].matched_keywords == ["LLM"]

    def test_higher_scoring_story_wins_within_a_source(self):
        low = _news_item("AI story low", "https://e.com/1", points=120)
        high = _news_item("AI story high", "https://e.com/2", points=900)
        assert select_news([low, high], keywords=["AI"], top_n=1) == [high]

    def test_busy_feed_cannot_crowd_out_the_other_sources(self):
        """The whole point of the round-robin: TechCrunch posts ~20 items a day."""
        feed = [
            _news_item(f"AI feed story {i}", f"https://tc.com/{i}", venue="TechCrunch")
            for i in range(10)
        ]
        hn = [_news_item("AI on HN", "https://e.com/hn", points=500)]

        selected = select_news(feed + hn, keywords=["AI"], top_n=4)

        assert len(selected) == 4
        assert hn[0] in selected, "Hacker News must still get a slot"
        assert sum(1 for item in selected if item.venue == "TechCrunch") == 3

    def test_selection_never_calls_the_model(self):
        """News skips the cheap-model relevance gate entirely — that is the point."""
        provider = MagicMock()
        items = [_news_item("AI thing", "https://e.com/1")]

        select_news(items, keywords=["AI"], top_n=5)

        provider.complete.assert_not_called()


# ── Note shape ────────────────────────────────────────────────────────────────

class TestNewsNoteShape:
    def test_news_note_has_three_sections_not_four(self):
        provider = MagicMock()
        provider.complete.return_value = json.dumps({
            "one_line_summary": "요약",
            "key_contributions": ["a", "b", "c"],
            "relevance_to_profile": "연결점",
        })
        note = generate_note(
            _news_item("headline", "https://e.com/1"),
            Config(research_profile="LLM 정렬"),
            provider,
        )
        assert note.content_type == "news"
        assert note.expected_sections() == 3
        assert note.is_complete(), "news note must count as complete without 방법"
        assert note.method == ""

    def test_paper_note_still_expects_four(self):
        note = ResearchNote("s", ["a"], "method", "rel", content_type="paper")
        assert note.expected_sections() == 4
        assert note.is_complete()


# ── News pipeline stage ───────────────────────────────────────────────────────

class TestNewsStage:
    @pytest.fixture(autouse=True)
    def _cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

    def _cfg(self, **news_kw) -> Config:
        defaults = dict(enabled=True, hacker_news_enabled=True,
                        hacker_news_min_points=100, rss_feeds=[], top_n=3,
                        keywords=["AI", "LLM"])
        defaults.update(news_kw)
        return Config(
            keywords=["large language model"],  # paper keywords, unused by news
            research_profile="LLM 정렬 연구",
            notion_token="tok",
            anthropic_api_key="key",
            news=NewsConfig(**defaults),
        )

    def _provider(self) -> MagicMock:
        """A provider that only ever answers note requests — news never ranks."""
        provider = MagicMock()
        provider.complete.return_value = json.dumps({
            "one_line_summary": "요약",
            "key_contributions": ["a", "b", "c"],
            "relevance_to_profile": "연결점",
        })
        return provider

    def test_disabled_news_does_nothing(self):
        written = _run_news_with(self._cfg(enabled=False), MagicMock())
        assert written == []

    def test_writes_matching_stories_to_notion(self):
        stories = [
            _news_item("New LLM benchmark released", "https://e.com/1"),
            _news_item("AI chip startup raises round", "https://e.com/2"),
        ]
        with (
            patch("paper_digest.news_stage.collect_hackernews_stories", return_value=stories),
            patch("paper_digest.news_stage.collect_rss_entries", return_value=[]),
            patch("paper_digest.news_stage.create_page", side_effect=["p1", "p2"]),
        ):
            provider = self._provider()
            written = _run_news_with(self._cfg(), provider)

        assert len(written) == 2
        assert all(item.content_type == "news" for item in written)
        assert [item.notion_page_id for item in written] == ["p1", "p2"]
        # One call per story, for its note. Any extra call means a relevance
        # ranking pass crept back in.
        assert provider.complete.call_count == 2
        assert all(item.relevance_score == 0.0 for item in written)

    def test_stories_missing_every_keyword_are_dropped(self):
        stories = [_news_item("Sourdough starter tips", "https://e.com/bread")]
        with (
            patch("paper_digest.news_stage.collect_hackernews_stories", return_value=stories),
            patch("paper_digest.news_stage.collect_rss_entries", return_value=[]),
            patch("paper_digest.news_stage.create_page") as create,
        ):
            written = _run_news_with(self._cfg(), self._provider())

        assert written == []
        create.assert_not_called()

    def test_already_written_url_is_not_rewritten(self):
        story = _news_item("New LLM benchmark released", "https://e.com/1")

        # Same link, different headline — the URL is the identity for news.
        repost = _news_item("LLM benchmark, now on the front page", "https://e.com/1")
        with (
            patch("paper_digest.news_stage.collect_hackernews_stories", return_value=[repost]),
            patch("paper_digest.news_stage.collect_rss_entries", return_value=[]),
            patch("paper_digest.news_stage.create_page") as create,
        ):
            written = _run_news_with(self._cfg(), self._provider(),
                                     already_written=[story])

        assert written == []
        create.assert_not_called()

    def test_collector_failure_is_contained(self):
        """A dead feed must not fail a run whose papers already landed."""
        with (
            patch("paper_digest.news_stage.collect_hackernews_stories",
                  side_effect=RuntimeError("HN down")),
            patch("paper_digest.news_stage.collect_rss_entries", return_value=[]),
        ):
            assert _run_news_with(self._cfg(), self._provider()) == []


class TestNewsProfile:
    """What the shared briefing is written against.

    News belongs to nobody, so it has no ``research_profile`` — that field is
    per member now. Handing the note prompt an empty profile produced a "왜
    알아둘 만한지" section written against nothing at all, which is how this
    stage regressed when the config was split.
    """

    def _member(self, member_id, name, profile):
        from paper_digest.members import Member

        return Member(member_id=member_id, name=name, research_profile=profile,
                      keywords=["AI"], top_n=5)

    def test_every_member_is_represented(self):
        """Members here work on different things, so all of them have to appear.

        A single shared paragraph used to fill this slot. It had to be vague
        enough to cover everyone, which for a lab whose members genuinely differ
        is the same as saying nothing.
        """
        from paper_digest.config import Config
        from paper_digest.news_stage import news_profile

        profile = news_profile(Config(), [
            self._member("a", "가", "정치적 편향 측정"),
            self._member("b", "나", "검색 다양성"),
        ])
        assert "[가] 정치적 편향 측정" in profile
        assert "[나] 검색 다양성" in profile

    def test_a_member_without_a_profile_is_skipped(self):
        from paper_digest.config import Config
        from paper_digest.news_stage import news_profile

        profile = news_profile(Config(), [
            self._member("a", "가", "정치적 편향 측정"),
            self._member("b", "나", "   "),
        ])
        assert "[나]" not in profile

    def test_no_members_yields_an_empty_profile(self):
        from paper_digest.config import Config
        from paper_digest.news_stage import news_profile

        assert news_profile(Config(), []) == ""

    def test_the_note_prompt_actually_receives_it(self):
        """The regression was upstream of the prompt, so assert on the prompt."""
        from paper_digest.config import Config, NewsConfig

        cfg = Config(notion_token="t",
                     news=NewsConfig(enabled=True, top_n=2, keywords=["LLM"]))
        stories = [_news_item("New LLM benchmark released", "https://e.com/1")]

        prompts = []
        provider = MagicMock()

        def complete(prompt, model, max_tokens=512, system=None):
            prompts.append(prompt)
            return json.dumps({"one_line_summary": "요약",
                               "key_contributions": ["a", "b", "c"],
                               "relevance_to_profile": "연결점"})

        provider.complete.side_effect = complete

        with (
            patch("paper_digest.news_stage.collect_hackernews_stories",
                  return_value=stories),
            patch("paper_digest.news_stage.collect_rss_entries", return_value=[]),
            patch("paper_digest.news_stage.create_page", return_value="p1"),
        ):
            written = _run_news_with(cfg, provider, members=[
                self._member("a", "가", "정치적 편향 측정"),
            ])

        assert len(written) == 1
        assert prompts and "정치적 편향 측정" in prompts[0]
