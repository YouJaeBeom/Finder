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
class Config:
    """Full application configuration."""

    # Notion settings
    notion_parent_page_id: str = ""

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

    cfg = Config(
        notion_parent_page_id=data.get("notion_parent_page_id", ""),
        keywords=data.get("keywords", []),
        tracked_venues=data.get("tracked_venues", []),
        research_profile=data.get("research_profile", ""),
        arxiv_categories=data.get("arxiv_categories", ["cs.CL", "cs.AI", "cs.LG"]),
        days_back=data.get("days_back", 7),
        max_papers_to_rank=data.get("max_papers_to_rank", 1500),
        top_n=data.get("top_n", 10),
        llm=llm_cfg,
        # Secrets always come from the environment, never from the config file
        notion_token=os.environ.get("NOTION_TOKEN", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
    )

    return cfg
