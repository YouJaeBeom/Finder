"""Lab members: one YAML file per person, one research topic each.

A member file is the entire definition of a person in this system::

    members/jaebeom.yaml
        name: "유재범"
        enabled: true
        top_n: 20
        research_profile: |
          ...
        keywords:
          - "political bias"
          - all: [["LLM", "language model"], ["robustness"]]

The filename stem is the member ID. It never appears in Notion — it names the
member's scoring-cache file and identifies them on the command line, so it has
to be filesystem-safe, while *name* is free to be anything Notion accepts.

**One research topic per member.** A member has one profile and one keyword set,
not a list of topics. Multiple topics per person would mean either mixing them
into one relevance judgement (which defeats the point of separating them) or
splitting a person across several databases — both are decisions this design
deliberately defers until someone actually needs it.

Everything is validated up front and *every* problem is reported at once.
Finding out about the second member's typo only after fixing the first, on a
monthly schedule, is how a run gets skipped for a month.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Sequence

import yaml

from .keywords import compile_rules

if TYPE_CHECKING:  # avoids a config <-> members import cycle at runtime
    from .config import Config

logger = logging.getLogger(__name__)

MEMBERS_DIR = "members"

# Member IDs name files on disk and get passed on the command line, so they are
# restricted rather than trusted: a stem with a path separator or a leading dot
# would write the scoring cache somewhere nobody expects.
_ALLOWED_ID = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


class MemberConfigError(Exception):
    """The member files cannot be used. The message lists every problem found."""


@dataclass(frozen=True)
class Member:
    """One person's research interest, as loaded from their YAML file."""

    member_id: str
    name: str
    research_profile: str
    keywords: List[Any]     # loose first pass; empty = score everything
    top_n: Optional[int]   # None = every paper that clears the relevance cutoff
    min_relevance: Optional[int] = None   # None = the lab default
    enabled: bool = True
    source_path: str = ""

    def scored_cache_path(self, root: str = "state/scored") -> str:
        """Where this member's "already ranked and rejected" cache lives."""
        return str(Path(root) / f"{self.member_id}.json")


def _read_one(path: Path, problems: List[str]) -> Member | None:
    """Parse and validate one member file, appending any problems found.

    Returns None when the file cannot yield a usable member. Validation
    continues on the remaining files either way — see the module docstring.
    """
    member_id = path.stem

    if not member_id or set(member_id.lower()) - _ALLOWED_ID:
        problems.append(
            f"{path}: filename {member_id!r} is not a usable member ID — "
            "use letters, digits, '-' and '_' only"
        )
        return None

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        problems.append(f"{path}: could not be read ({exc})")
        return None

    if not isinstance(raw, dict):
        problems.append(f"{path}: expected a YAML mapping, got {type(raw).__name__}")
        return None

    name = str(raw.get("name") or "").strip()
    if not name:
        problems.append(f"{path}: 'name' is required — it becomes the Notion page title")

    profile = str(raw.get("research_profile") or "").strip()
    if not profile:
        problems.append(
            f"{path}: 'research_profile' is required — relevance scoring and the "
            "'내 연구와의 연결점' note are both written against it"
        )

    # The member's loose first pass. Optional — left out, every collected paper
    # is scored against their profile, which costs about four times as much.
    #
    # "Loose" is the operative word and it is not a style preference: rules
    # written for precision were measured dropping papers the relevance model
    # scored 8 and 9. Rules that catch the member's *field* keep those and still
    # remove three quarters of the pool.
    keywords = raw.get("keywords")
    keywords_ok = True
    if keywords is None:
        keywords = []
    elif not isinstance(keywords, list) or not keywords:
        problems.append(
            f"{path}: 'keywords' must be a non-empty list when present — leave "
            "the key out entirely to score every collected paper"
        )
        keywords_ok = False
        keywords = []
    else:
        # compile_rules drops malformed entries with a warning rather than
        # raising, which is right at runtime and wrong at registration time: a
        # rule silently dropped here is a member quietly receiving less.
        usable = len(compile_rules(keywords))
        if usable < len(keywords):
            problems.append(
                f"{path}: {len(keywords) - usable} of {len(keywords)} keyword "
                "entries are malformed and would be ignored — see the warnings above"
            )
            keywords_ok = False

    # Absent means unlimited. A cap is the wrong default here: the point of the
    # digest is not to miss things, and the relevance cutoff already decides what
    # is worth writing up. Ranking by relevance and then keeping the best twenty
    # silently discards the twenty-first, which nobody ever finds out about.
    cutoff = raw.get("min_relevance")
    if cutoff is not None and not (isinstance(cutoff, int)
                                   and not isinstance(cutoff, bool)
                                   and 0 <= cutoff <= 10):
        problems.append(
            f"{path}: 'min_relevance' must be a whole number from 0 to 10, "
            f"got {cutoff!r}"
        )
        cutoff = None

    top_n = raw.get("top_n")
    top_n_ok = top_n is None or (
        isinstance(top_n, int) and not isinstance(top_n, bool) and top_n >= 1
    )
    if not top_n_ok:
        problems.append(
            f"{path}: 'top_n' must be a positive integer, or absent for no limit "
            f"— got {top_n!r}"
        )

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append(f"{path}: 'enabled' must be true or false, got {enabled!r}")
        enabled = False

    if not (name and profile and keywords_ok and top_n_ok):
        return None

    return Member(
        member_id=member_id,
        name=name,
        research_profile=profile,
        keywords=keywords,
        top_n=top_n,
        min_relevance=cutoff,
        enabled=enabled,
        source_path=str(path),
    )


def load_members(
    directory: str = MEMBERS_DIR,
    max_members: int = 15,
    max_top_n: int = 30,
) -> List[Member]:
    """Load every member file in *directory*, or raise with all the problems.

    Returns only enabled members, ordered by member ID so a run processes people
    in the same sequence every week — a run that reorders itself makes its own
    logs unreadable.

    *max_members* and *max_top_n* are refused rather than clamped. On a shared
    API key one person's ``top_n: 300`` is everyone's bill, and a clamp that
    quietly does something other than what the file says is worse than a stop.
    """
    root = Path(directory)
    if not root.is_dir():
        raise MemberConfigError(
            f"No members directory at {directory!r}. Create it and add one YAML "
            "file per person — see members/jaebeom.yaml for the shape."
        )

    paths = sorted(p for p in root.glob("*.y*ml") if p.is_file())
    if not paths:
        raise MemberConfigError(f"No member files in {directory!r} (looked for *.yaml)")

    problems: List[str] = []
    members = [m for p in paths if (m := _read_one(p, problems)) is not None]

    _check_unique_names(members, problems)

    active = [m for m in members if m.enabled]
    for member in active:
        if member.top_n is not None and member.top_n > max_top_n:
            problems.append(
                f"{member.source_path}: top_n {member.top_n} exceeds the lab limit "
                f"of {max_top_n} (limits.max_top_n_per_member in config.yaml)"
            )

    if len(active) > max_members:
        problems.append(
            f"{len(active)} enabled members exceeds the lab limit of {max_members} "
            "(limits.max_members in config.yaml)"
        )

    if problems:
        raise MemberConfigError(
            f"{len(problems)} problem(s) in the member configuration:\n  - "
            + "\n  - ".join(problems)
        )

    logger.info(
        "Loaded %d member(s): %s",
        len(active), ", ".join(f"{m.name} ({m.member_id})" for m in active),
    )
    return active


def _check_unique_names(members: Sequence[Member], problems: List[str]) -> None:
    """Two members sharing a display name would share one Notion page.

    Pages are resolved by title under the parent, so duplicate names do not
    collide loudly — they silently merge two people's digests into one database.
    """
    seen: dict[str, str] = {}
    for member in members:
        if first := seen.get(member.name):
            problems.append(
                f"{member.source_path}: name {member.name!r} is already used by "
                f"{first} — Notion pages are resolved by title, so two members "
                "with the same name would share one database"
            )
        else:
            seen[member.name] = member.source_path


def effective_config(cfg: "Config", member: Member) -> "Config":
    """The lab config as one member sees it.

    This is the seam that keeps the rest of the pipeline single-tenant.
    ``ranking.py`` and ``notes.py`` read ``cfg.research_profile`` and
    ``cfg.top_n`` and have no idea that more than one researcher exists; handing
    them a config with this member's values substituted is cheaper and far less
    invasive than threading a member through every signature.
    """
    from dataclasses import replace

    overrides = dict(
        research_profile=member.research_profile,
        keywords=member.keywords,
        top_n=member.top_n,
    )
    if member.min_relevance is not None:
        overrides["min_relevance"] = member.min_relevance
    return replace(cfg, **overrides)
