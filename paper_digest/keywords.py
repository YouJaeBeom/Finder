"""Keyword matching shared by the paper and news stages.

A flat list of keywords is pure OR, and OR alone is too blunt: adding "LLM"
matches most of cs.CL, which is exactly the precision-over-recall failure the
requirements warn about. So a keyword entry may also be a rule combining terms.

    keywords:
      - "instruction tuning"                # this phrase appears

      - all: ["LLM", "alignment"]           # both appear

      - any: ["RLHF", "DPO"]                # at least one appears

      - all:                                # a nested list is "any of these",
          - ["LLM", "large language model"] #   so this reads: a model term
          - ["alignment", "safety"]         #   AND a topic term
        not: ["survey"]                     # ...and none of these appear

Entries are OR-ed together: an item is kept when any one rule matches. Within a
rule, every ``all`` requirement must be satisfied — a string by appearing, a
nested list by at least one of its terms appearing — plus at least one ``any``
term, and no ``not`` term.

Lives in its own module because both the paper pipeline and the LLM-free news
selection need it, and news importing it from ``pipeline`` would be circular.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Sequence, Tuple

from .models import Paper

logger = logging.getLogger(__name__)

# One requirement: at least one of these terms must appear. A plain keyword is
# simply a group of one.
Group = Tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    """One keyword rule: every group must be satisfied, no exclusion may hit."""

    groups: Tuple[Group, ...] = ()
    not_terms: Group = ()


def _group(value: Any) -> Group:
    """Coerce one ``all`` element into a group of alternatives."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if str(v))
    return (str(value),)


def _groups_from(value: Any) -> Tuple[Group, ...]:
    """Coerce an ``all`` value into groups; a bare string is a single group."""
    if value is None:
        return ()
    if isinstance(value, str):
        return ((value,),)
    return tuple(g for g in (_group(v) for v in value) if g)


def _flat(value: Any) -> Group:
    """Coerce an ``any``/``not`` value into a flat tuple of terms."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value if str(v))


def compile_rules(keywords: Sequence[Any]) -> List[Rule]:
    """Turn the config's keyword list into rules, skipping unusable entries.

    A malformed entry is logged and dropped rather than raised: one bad line in
    config.yaml should cost that line, not the whole week's digest.
    """
    rules: List[Rule] = []

    for entry in keywords or []:
        if isinstance(entry, str):
            rules.append(Rule(groups=((entry,),)))
            continue

        if not isinstance(entry, dict):
            logger.warning("Ignoring keyword entry that is neither text nor a "
                           "rule: %r", entry)
            continue

        unknown = set(entry) - {"all", "any", "not"}
        if unknown:
            logger.warning("Ignoring unknown keys %s in keyword rule %r "
                           "(expected all/any/not)", sorted(unknown), entry)

        groups = _groups_from(entry.get("all"))
        if any_terms := _flat(entry.get("any")):
            groups += (any_terms,)

        if not groups:
            # A rule with only exclusions would match everything it doesn't
            # exclude — almost certainly not what was meant.
            logger.warning("Ignoring keyword rule with no 'all' or 'any' "
                           "terms: %r", entry)
            continue

        rules.append(Rule(groups=groups, not_terms=_flat(entry.get("not"))))

    return rules


def _rule_hits(text: str, rule: Rule) -> List[str]:
    """Terms that matched, or [] when the rule does not apply to this text."""
    if any(term.lower() in text for term in rule.not_terms):
        return []

    hits: List[str] = []
    for group in rule.groups:
        found = [term for term in group if term.lower() in text]
        if not found:
            return []  # an unsatisfied requirement fails the whole rule
        hits.extend(found)

    return hits


def _matched_labels(text: str, rules: Sequence[Rule]) -> List[str]:
    """Terms from every matching rule, in the config's own spelling."""
    matched: List[str] = []
    for rule in rules:
        for term in _rule_hits(text, rule):
            if term not in matched:
                matched.append(term)
    return matched


def matches_keywords(paper: Paper, keywords: Sequence[Any]) -> List[str]:
    """Return the keyword terms matching this item's title or body text."""
    text = f"{paper.title} {paper.abstract or ''}".lower()
    return _matched_labels(text, compile_rules(keywords))


def filter_by_keywords(papers: List[Paper], keywords: Sequence[Any]) -> List[Paper]:
    """Keep items matching at least one rule, recording which terms matched.

    An empty *keywords* list matches nothing, which is the literal reading and
    keeps the paper pipeline safe: papers must never bypass the keyword gate,
    since collection is scoped by those same keywords. The news stage treats an
    empty list as "keep everything" and skips this call entirely — that choice
    belongs to the caller, not here.
    """
    # Compiled once, not once per paper: a weekly run filters ~1500 of them.
    rules = compile_rules(keywords)

    result: List[Paper] = []
    for paper in papers:
        text = f"{paper.title} {paper.abstract or ''}".lower()
        matched = _matched_labels(text, rules)
        if matched:
            paper.matched_keywords = matched
            result.append(paper)
    return result
