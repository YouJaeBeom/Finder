"""Config parsing and the preflight gate.

Both exist because of one real failure: a scheduled run died with exit 1 and an
empty artifact, and the cause was a config field nobody could see was wrong.
Every case here turns a silent or late failure into a sentence that says what
to fix.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from paper_digest.config import (
    Config,
    LimitsConfig,
    LLMConfig,
    NewsConfig,
    extract_notion_id,
)
from paper_digest.members import Member
from paper_digest.pipeline import check_budget, preflight
from paper_digest.reporter import write_failure_report

PAGE_ID = "3bc1256e056180898608c39506c43463"
DASHED = "3bc1256e-0561-8089-8608-c39506c43463"


class TestExtractNotionId:
    @pytest.mark.parametrize("raw", [
        # What "Copy link" actually puts on the clipboard — the form that broke
        # the first live run.
        "https://app.notion.com/p/Finder-3bc1256e056180898608c39506c43463?source=copy_link",
        "https://www.notion.so/myworkspace/Finder-3bc1256e056180898608c39506c43463",
        "https://notion.so/3bc1256e056180898608c39506c43463",
        PAGE_ID,
        DASHED,
        f"  {DASHED}  ",
    ])
    def test_accepts_every_form_notion_hands_you(self, raw):
        assert extract_notion_id(raw) == DASHED

    @pytest.mark.parametrize("raw", [
        "", "REPLACE_WITH_YOUR_NOTION_PAGE_ID", "my-notion-page", "12345",
    ])
    def test_rejects_what_is_not_an_id(self, raw):
        assert extract_notion_id(raw) == ""

    def test_case_is_normalized(self):
        assert extract_notion_id(PAGE_ID.upper()) == DASHED


def _cfg(**kw) -> Config:
    base = dict(
        notion_parent_page_id=PAGE_ID,
        notion_token="tok",
        anthropic_api_key="key",
        llm=LLMConfig(provider="anthropic"),
    )
    base.update(kw)
    return Config(**base)


def _member(member_id: str, top_n: int) -> Member:
    return Member(
        member_id=member_id,
        name=member_id,
        research_profile="profile",
        top_n=top_n,
    )


class TestPreflight:
    def test_a_complete_config_passes(self):
        assert preflight(_cfg()) is None

    def test_missing_notion_token_names_the_secret(self):
        problem = preflight(_cfg(notion_token=""))
        assert problem and "NOTION_TOKEN" in problem

    def test_missing_llm_key_names_the_right_provider_secret(self):
        problem = preflight(_cfg(anthropic_api_key=""))
        assert problem and "ANTHROPIC_API_KEY" in problem

    def test_openai_provider_asks_for_the_openai_key(self):
        problem = preflight(_cfg(
            llm=LLMConfig(provider="openai"), anthropic_api_key="", openai_api_key=""
        ))
        assert problem and "OPENAI_API_KEY" in problem

    def test_openai_provider_ignores_a_missing_anthropic_key(self):
        assert preflight(_cfg(
            llm=LLMConfig(provider="openai"), anthropic_api_key="", openai_api_key="k"
        )) is None

    def test_untouched_placeholder_is_called_out_by_name(self):
        problem = preflight(_cfg(
            notion_parent_page_id="REPLACE_WITH_YOUR_NOTION_PAGE_ID"
        ))
        assert problem and "placeholder" in problem

    def test_garbage_page_id_is_quoted_back(self):
        problem = preflight(_cfg(notion_parent_page_id="my-notion-page"))
        assert problem and "my-notion-page" in problem

    def test_a_pasted_url_is_accepted_not_rejected(self):
        """The URL is what people paste; it must pass, not fail with advice."""
        assert preflight(_cfg(
            notion_parent_page_id=f"https://app.notion.com/p/F-{PAGE_ID}?source=copy_link"
        )) is None


class TestBudgetGate:
    """The lab shares one API key, so the run refuses rather than clamps.

    A clamp would quietly serve someone fewer papers than their file asks for,
    and the person reading the digest has no way to tell that happened.
    """

    def test_a_run_within_the_limit_passes(self):
        cfg = _cfg(limits=LimitsConfig(max_notes_per_run=100))
        assert check_budget(cfg, [_member("a", 20), _member("b", 20)]) is None

    def test_the_sum_over_members_is_what_is_checked(self):
        # No single member is over the per-member limit; together they are over
        # the run limit. This is the case max_top_n_per_member cannot catch.
        cfg = _cfg(limits=LimitsConfig(max_top_n_per_member=30,
                                       max_notes_per_run=50))
        problem = check_budget(cfg, [_member(str(i), 30) for i in range(3)])
        assert problem and "90 notes" in problem and "50" in problem

    def test_news_counts_toward_the_limit_when_enabled(self):
        cfg = _cfg(limits=LimitsConfig(max_notes_per_run=10),
                   news=NewsConfig(enabled=True, top_n=5))
        assert check_budget(cfg, [_member("a", 10)]) is not None

    def test_news_costs_nothing_when_disabled(self):
        cfg = _cfg(limits=LimitsConfig(max_notes_per_run=10),
                   news=NewsConfig(enabled=False, top_n=5))
        assert check_budget(cfg, [_member("a", 10)]) is None

    def test_init_mode_does_not_require_an_llm_key(self):
        """init only ever talks to Notion."""
        assert preflight(_cfg(anthropic_api_key=""), needs_llm=False) is None


class TestFailureReport:
    def test_report_exists_even_when_the_run_never_started(self, tmp_path, monkeypatch):
        """Otherwise the Actions artifact is empty on exactly the failing runs."""
        monkeypatch.chdir(tmp_path)

        write_failure_report("monthly", "NOTION_TOKEN is not set.")

        report = json.loads(Path("run-report.json").read_text(encoding="utf-8"))
        assert report["status"] == "failed"
        assert report["error"] == "NOTION_TOKEN is not set."
        assert report["pages_created"] == 0


class TestKeyBelongsToProvider:
    """A key pasted into the wrong variable, caught before anything is spent.

    The LLM key is not used until the first ranking call, which happens after
    collection and after the news pages are written. A 401 there reads like a
    flaky vendor rather than a typo in a secret, and the run has already cost
    time and Notion writes by then.
    """

    def test_an_anthropic_key_under_openai_is_refused(self):
        cfg = _cfg(llm=LLMConfig(provider="openai"),
                   openai_api_key="sk-ant-api03-xxxx")
        problem = preflight(cfg)
        assert problem is not None
        assert "OPENAI_API_KEY" in problem
        assert "sk-ant-" in problem

    def test_an_openai_key_under_anthropic_is_refused(self):
        cfg = _cfg(llm=LLMConfig(provider="anthropic"),
                   anthropic_api_key="sk-proj-xxxx")
        problem = preflight(cfg)
        assert problem is not None
        assert "ANTHROPIC_API_KEY" in problem

    def test_a_matching_key_passes(self):
        cfg = _cfg(llm=LLMConfig(provider="openai"),
                   openai_api_key="sk-proj-xxxx")
        assert preflight(cfg) is None

    def test_an_unfamiliar_prefix_is_left_alone(self):
        """Neither vendor promises these prefixes forever.

        Guessing wrong here would block a run over a key that works, which is
        worse than the late 401 this check exists to prevent.
        """
        cfg = _cfg(llm=LLMConfig(provider="openai"),
                   openai_api_key="some-new-format-2027")
        assert preflight(cfg) is None

    def test_init_does_not_care_about_the_llm_key(self):
        """init only talks to Notion, so a bad LLM key must not block setup."""
        cfg = _cfg(llm=LLMConfig(provider="openai"),
                   openai_api_key="sk-ant-wrong")
        assert preflight(cfg, needs_llm=False) is None
