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

from paper_digest.config import Config, LLMConfig, extract_notion_id
from paper_digest.pipeline import preflight
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

    def test_unusable_pinned_database_id_is_rejected(self):
        problem = preflight(_cfg(notion_database_id="not-an-id"))
        assert problem and "notion_database_id" in problem

    def test_empty_database_id_is_fine(self):
        assert preflight(_cfg(notion_database_id="")) is None

    def test_init_mode_does_not_require_an_llm_key(self):
        """init only ever talks to Notion."""
        assert preflight(_cfg(anthropic_api_key=""), needs_llm=False) is None


class TestFailureReport:
    def test_report_exists_even_when_the_run_never_started(self, tmp_path, monkeypatch):
        """Otherwise the Actions artifact is empty on exactly the failing runs."""
        monkeypatch.chdir(tmp_path)

        write_failure_report("weekly", "NOTION_TOKEN is not set.")

        report = json.loads(Path("run-report.json").read_text(encoding="utf-8"))
        assert report["status"] == "failed"
        assert report["error"] == "NOTION_TOKEN is not set."
        assert report["pages_created"] == 0
