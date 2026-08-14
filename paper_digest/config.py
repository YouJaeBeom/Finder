"""Configuration loading from YAML file with environment variable injection."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

import yaml


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
        notion_parent_page_id=data.get("notion_parent_page_id", ""),
        notion_database_id=data.get("notion_database_id", "") or "",
        keywords=data.get("keywords", []),
        tracked_venues=data.get("tracked_venues", []),
        research_profile=data.get("research_profile", ""),
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
