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
class VenueSourceConfig:
    """One venue class — conferences or journals — collected via Semantic Scholar.

    The venue list ships with the tool (paper_digest/data/venues.csv); *kind*
    selects the rows this source draws from. *min_score* is the quality floor
    within that kind, and include/exclude adjust the result by abbreviation.

    Both classes are allowlisted by construction. That is the whole quality
    mechanism: a digest that trusts a topic filter instead ends up reading
    predatory journals, which is exactly what the OpenAlex source did before it
    was removed.
    """

    enabled: bool = True
    min_score: float = 0.5
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    # Generous on purpose. Collection costs requests, not money — the bill is
    # set by max_papers_to_rank, which caps ranking after the keyword filter has
    # already cut the pool. A tighter limit truncates in venue order, which
    # silently drops whole conferences in the week their proceedings land: a
    # measured SIGIR week collected 474 papers against a limit of 400.
    max_results: int = 3000


@dataclass
class LimitsConfig:
    """Hard ceilings on what one run may spend.

    The lab shares one API key, so a member file with ``top_n: 300`` is not that
    person's bill — it is everyone's. These are refused rather than clamped:
    quietly doing something other than what the file says is worse than a stop
    that names the file.
    """

    max_members: int = 15
    max_top_n_per_member: int = 30
    # Whole-run guard, checked after every member's cut is known. Catches the
    # case no single member's limit can: fifteen people at the maximum.
    max_notes_per_run: int = 400


@dataclass
class Config:
    """Lab-wide configuration — everything that is *not* per member.

    Three fields here belong to a member rather than to the lab and are left as
    injection points rather than YAML keys: *research_profile*, *keywords* and
    *top_n*. :func:`paper_digest.members.effective_config` fills them in per
    person, which is what lets ranking.py and notes.py stay unaware that this
    tool serves more than one researcher.
    """

    # Notion settings — the workspace page everything is created under
    notion_parent_page_id: str = ""

    # Where the per-member YAML files live
    members_dir: str = "members"

    # ── Injected per member, never read from config.yaml ──
    keywords: List[str] = field(default_factory=list)
    research_profile: str = ""

    # The lab's shared context, used for the *news* briefing only. News is
    # written once for everybody, so there is no member whose profile it could
    # be written against — and the note's "why this matters" section is useless
    # without one. Left empty, the news stage falls back to joining the members'
    # own profiles, so an unset key degrades rather than breaking.
    lab_profile: str = ""

    # Extra "full name fragment" -> acronym mappings for the Venue column, on
    # top of the built-in table in paper_digest/venues.py.
    venue_aliases: Dict[str, str] = field(default_factory=dict)

    # Collection settings
    days_back: int = 7

    # Cost controls
    max_papers_to_rank: int = 1500
    top_n: int = 20            # injected per member
    limits: LimitsConfig = field(default_factory=LimitsConfig)

    # LLM configuration
    llm: LLMConfig = field(default_factory=LLMConfig)

    # IT news collection (opt-in)
    news: NewsConfig = field(default_factory=NewsConfig)

    # The two paper sources, both allowlisted from the shipped venue table.
    conferences: VenueSourceConfig = field(default_factory=VenueSourceConfig)
    journals: VenueSourceConfig = field(default_factory=VenueSourceConfig)

    # Secrets — loaded from environment at runtime
    notion_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    def parent_page_id(self) -> str:
        """The parent page ID, accepting a pasted Notion URL. "" if unusable."""
        if self.notion_parent_page_id in PLACEHOLDERS:
            return ""
        return extract_notion_id(self.notion_parent_page_id)

    def llm_api_key(self) -> str:
        """The API key for the configured provider."""
        return (
            self.anthropic_api_key
            if self.llm.provider == "anthropic"
            else self.openai_api_key
        )

    def llm_key_looks_wrong(self) -> Optional[str]:
        """Why the configured key cannot belong to the configured provider.

        The two vendors stamp their keys with distinct prefixes, so a key pasted
        into the wrong variable is recognisable on sight. Worth checking because
        of *when* the alternative surfaces: the key is not used until the first
        ranking call, which is after collection has run and after the pages of
        the previous stage exist. A 401 there reads as a flaky API rather than
        as a typo in a secret.

        Only mismatches this is sure about are reported — an unrecognised prefix
        stays silent, since neither vendor promises these forever.
        """
        key = self.llm_api_key()
        if self.llm.provider == "openai" and key.startswith("sk-ant-"):
            return (
                "OPENAI_API_KEY holds an Anthropic key (it starts with "
                "'sk-ant-'), so OpenAI will reject it. Either put an OpenAI key "
                "there, or set llm.provider to 'anthropic' in config.yaml."
            )
        if self.llm.provider == "anthropic" and key.startswith("sk-proj-"):
            return (
                "ANTHROPIC_API_KEY holds an OpenAI key (it starts with "
                "'sk-proj-'), so Anthropic will reject it. Either put an "
                "Anthropic key there, or set llm.provider to 'openai'."
            )
        return None

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

    def venue_source(key: str, default_max: int) -> VenueSourceConfig:
        raw = data.get(key, {}) or {}
        return VenueSourceConfig(
            enabled=raw.get("enabled", True),
            min_score=float(raw.get("min_score", 0.5)),
            include=raw.get("include", []) or [],
            exclude=raw.get("exclude", []) or [],
            max_results=int(raw.get("max_results", default_max)),
        )

    conf_cfg = venue_source("conferences", 3000)
    journal_cfg = venue_source("journals", 1000)

    limits_data = data.get("limits", {}) or {}
    limits_cfg = LimitsConfig(
        max_members=int(limits_data.get("max_members", 15)),
        max_top_n_per_member=int(limits_data.get("max_top_n_per_member", 30)),
        max_notes_per_run=int(limits_data.get("max_notes_per_run", 400)),
    )

    cfg = Config(
        # Kept raw so the error message can quote what the user actually wrote;
        # normalized on access via parent_page_id() / database_id().
        notion_parent_page_id=data.get("notion_parent_page_id", "") or "",
        members_dir=data.get("members_dir", "members") or "members",
        lab_profile=data.get("lab_profile", "") or "",
        venue_aliases=data.get("venue_aliases", {}) or {},
        days_back=data.get("days_back", 7),
        max_papers_to_rank=data.get("max_papers_to_rank", 1500),
        limits=limits_cfg,
        llm=llm_cfg,
        news=news_cfg,
        conferences=conf_cfg,
        journals=journal_cfg,
        # Secrets always come from the environment, never from the config file
        notion_token=os.environ.get("NOTION_TOKEN", ""),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
    )

    return cfg
