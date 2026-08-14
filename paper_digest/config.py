"""Configuration loading from YAML file with environment variable injection."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import yaml

# Notion IDs are UUIDs that appear with or without dashes depending on where you
# copied them from.
_NOTION_ID_RE = re.compile(
    r"([0-9a-fA-F]{8})-?([0-9a-fA-F]{4})-?([0-9a-fA-F]{4})-?"
    r"([0-9a-fA-F]{4})-?([0-9a-fA-F]{12})"
)

# What the shipped config files say before a user fills them in.
PLACEHOLDERS = frozenset({
    "REPLACE_WITH_YOUR_NOTION_PAGE_ID",
    "YOUR_NOTION_PAGE_ID_HERE",
})


def extract_notion_id(raw: str) -> str:
    """Pull a Notion ID out of whatever the user pasted.

    "Copy link" in Notion yields a full URL, and that is what lands in
    config.yaml far more often than a bare ID — so accept both rather than
    failing on the input the UI actually hands people.

        https://app.notion.com/p/Finder-3bc1256e05618089…?source=copy_link
        https://www.notion.so/workspace/Title-3bc1256e05618089…
        3bc1256e-0561-8089-8608-c39506c43463
        3bc1256e056180898608c39506c43463

    Returns "" when there is no ID in the string, which the caller reports as a
    configuration error.
    """
    if not raw:
        return ""
    # Drop the query string first: ?source=copy_link and friends can't contain
    # the ID, and excluding them keeps the "last match wins" rule honest.
    matches = _NOTION_ID_RE.findall(raw.split("?")[0])
    if not matches:
        return ""
    # Last match: page URLs put the ID at the end, after the title slug.
    return "-".join(matches[-1]).lower()


@dataclass
class LLMConfig:
    """LLM provider and model configuration."""

    provider: str = "anthropic"  # "anthropic" | "openai"
    ranking_model: str = "claude-haiku-4-5"
    notes_model: str = "claude-opus-5"


@dataclass
class NewsConfig:
    """IT news collection settings.

    News is off by default so an existing paper-only config keeps working
    unchanged after upgrading.
    """

    enabled: bool = False
    hacker_news_enabled: bool = True
    hacker_news_min_points: int = 100
    rss_feeds: List[str] = field(default_factory=list)
    top_n: int = 5
    # Empty means no filter: keep everything collected. The sources are already
    # curated (HN score floor, hand-picked feeds), so "summarise all of it" is a
    # legitimate setting rather than a misconfiguration.
    keywords: List[str] = field(default_factory=list)


@dataclass
class Config:
    """Full application configuration."""

    # Notion settings
    notion_parent_page_id: str = ""
    # Optional. Pin the database every run writes to. Leave empty and the tool
    # finds or creates it under the parent page.
    notion_database_id: str = ""

    # Research parameters
    keywords: List[str] = field(default_factory=list)
    tracked_venues: List[str] = field(default_factory=list)
    research_profile: str = ""

    # Extra "full name fragment" -> acronym mappings for the Venue column, on
    # top of the built-in table in paper_digest/venues.py.
    venue_aliases: Dict[str, str] = field(default_factory=dict)

    # Venues to drop entirely. None means "use the built-in list of unmoderated
    # deposit archives"; an explicit list (including []) overrides it.
    excluded_venues: Optional[List[str]] = None

    # Collection settings
    arxiv_categories: List[str] = field(
        default_factory=lambda: ["cs.CL", "cs.AI", "cs.LG"]
    )
    days_back: int = 7

    # Cost controls
    max_papers_to_rank: int = 1500
    top_n: int = 10

    # LLM configuration
    llm: LLMConfig = field(default_factory=LLMConfig)

    # IT news collection (opt-in)
    news: NewsConfig = field(default_factory=NewsConfig)

    # Secrets — loaded from environment at runtime
    notion_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    def parent_page_id(self) -> str:
        """The parent page ID, accepting a pasted Notion URL. "" if unusable."""
        if self.notion_parent_page_id in PLACEHOLDERS:
            return ""
        return extract_notion_id(self.notion_parent_page_id)

    def database_id(self) -> str:
        """The pinned database ID, accepting a pasted Notion URL. "" if unset."""
        return extract_notion_id(self.notion_database_id)

    def llm_api_key(self) -> str:
        """The API key for the configured provider."""
        return (
            self.anthropic_api_key
            if self.llm.provider == "anthropic"
            else self.openai_api_key
        )

    def llm_key_env_var(self) -> str:
        """The environment variable name the configured provider reads."""
        return (
            "ANTHROPIC_API_KEY"
            if self.llm.provider == "anthropic"
            else "OPENAI_API_KEY"
        )


def load_config(path: str = "config.yaml") -> Config:
    """Load configuration from YAML file and inject secrets from environment."""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    llm_data = data.get("llm", {})
    llm_cfg = LLMConfig(
        provider=llm_data.get("provider", "anthropic"),
        ranking_model=llm_data.get("ranking_model", "claude-haiku-4-5"),
        notes_model=llm_data.get("notes_model", "claude-opus-5"),
    )

    news_data = data.get("news", {}) or {}
    hn_data = news_data.get("hacker_news", {}) or {}
    news_cfg = NewsConfig(
        enabled=news_data.get("enabled", False),
        hacker_news_enabled=hn_data.get("enabled", True),
        hacker_news_min_points=hn_data.get("min_points", 100),
        rss_feeds=news_data.get("rss_feeds", []) or [],
        top_n=news_data.get("top_n", 5),
        keywords=news_data.get("keywords", []) or [],
    )

    cfg = Config(
        # Kept raw so the error message can quote what the user actually wrote;
        # normalized on access via parent_page_id() / database_id().
        notion_parent_page_id=data.get("notion_parent_page_id", "") or "",
        notion_database_id=data.get("notion_database_id", "") or "",
        keywords=data.get("keywords", []),
        tracked_venues=data.get("tracked_venues", []),
        research_profile=data.get("research_profile", ""),
        venue_aliases=data.get("venue_aliases", {}) or {},
        excluded_venues=data.get("excluded_venues"),
        arxiv_categories=data.get("arxiv_categories", ["cs.CL", "cs.AI", "cs.LG"]),
        days_back=data.get("days_back", 7),
        max_papers_to_rank=data.get("max_papers_to_rank", 1500),
        top_n=data.get("top_n", 10),
        llm=llm_cfg,
        news=news_cfg,
        # Secrets always come from the environment, never from the config file
        notion_token=os.environ.get("NOTION_TOKEN", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
    )

    return cfg
