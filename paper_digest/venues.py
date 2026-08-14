"""Turning a source's name into the venue acronym people actually use.

OpenAlex reports venues by their full registered title — "Proceedings of the
32nd ACM International Conference on Information and Knowledge Management",
not "CIKM". A Notion column of those is unreadable and unsortable, so names are
normalized to the short form: CIKM, WSDM, SIGIR, ACL, EMNLP, TKDE.

Resolution order, most reliable first:

    1. venue_aliases in config.yaml — the user's own overrides
    2. the built-in table below
    3. an acronym in parentheses in the title, e.g. "... (RecSys '25)"
    4. the source's own abbreviated title, when it looks like an acronym
    5. the full name, trimmed of "Proceedings of the ..." boilerplate

The table is deliberately small and hand-checked rather than exhaustive: a
wrong acronym is worse than a long name, because it silently files a paper
under the wrong venue.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

# (acronym, distinguishing substrings). Order matters — the first match wins,
# so entries whose full name contains another entry's name come first (NAACL
# and EACL both contain "Association for Computational Linguistics").
_KNOWN_VENUES: List[Tuple[str, Tuple[str, ...]]] = [
    # ── NLP ──
    ("NAACL", ("north american chapter of the association for computational",)),
    ("EACL", ("european chapter of the association for computational",)),
    ("TACL", ("transactions of the association for computational linguistics",)),
    ("CoNLL", ("computational natural language learning",)),
    ("ACL", ("annual meeting of the association for computational linguistics",
             "association for computational linguistics",)),
    ("EMNLP", ("empirical methods in natural language processing",)),
    ("COLING", ("international conference on computational linguistics",)),
    ("LREC", ("language resources and evaluation",)),

    # ── Information retrieval and data mining ──
    ("SIGIR", ("research and development in information retrieval",
               "special interest group on information retrieval",)),
    ("ECIR", ("european conference on information retrieval",)),
    ("CIKM", ("information and knowledge management",)),
    ("WSDM", ("web search and data mining",)),
    ("KDD", ("knowledge discovery and data mining",)),
    ("RecSys", ("conference on recommender systems", "recommender systems",)),
    ("TOIS", ("transactions on information systems",)),
    # "The Web Conference" and "ACM Web Conference" are both in use; WWW is
    # what people still call it.
    ("WWW", ("web conference", "world wide web",)),

    # ── Databases ──
    ("SIGMOD", ("international conference on management of data",
                "management of data",)),
    ("VLDB", ("very large data bases", "very large databases",)),
    ("ICDE", ("international conference on data engineering",)),
    ("TKDE", ("transactions on knowledge and data engineering",)),

    # ── Machine learning and AI ──
    ("NeurIPS", ("neural information processing systems",)),
    ("ICML", ("international conference on machine learning",)),
    ("ICLR", ("international conference on learning representations",)),
    ("AISTATS", ("artificial intelligence and statistics",)),
    ("AAAI", ("aaai conference on artificial intelligence",)),
    ("IJCAI", ("international joint conference on artificial intelligence",)),
    ("JMLR", ("journal of machine learning research",)),
    ("TMLR", ("transactions on machine learning research",)),

    # ── Vision ──
    ("TPAMI", ("transactions on pattern analysis and machine intelligence",)),
    ("CVPR", ("computer vision and pattern recognition",)),
    ("ICCV", ("international conference on computer vision",)),
    ("ECCV", ("european conference on computer vision",)),

    # ── Preprint servers ──
    ("arXiv", ("arxiv",)),
]

# "... (CIKM '24)" / "(NeurIPS 2025)" — the acronym conferences put in their
# own proceedings titles.
_PARENTHESISED = re.compile(r"\(([A-Za-z][A-Za-z0-9\-]{1,11})\s*(?:['’]?\d{2,4})?\)")

# Boilerplate that carries no information once the acronym is gone.
_BOILERPLATE = re.compile(
    r"^(?:proceedings of|proceedings|the\s+)?\s*(?:the\s+)?"
    r"(?:\d+(?:st|nd|rd|th)\s+)?(?:annual\s+)?",
    re.IGNORECASE,
)

# The operating institution repositories are registered under — OpenAlex stores
# "Zenodo (CERN European Organization for Nuclear Research)".
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")

_MAX_VENUE_LEN = 100  # Notion select option limit


def _looks_like_acronym(text: str) -> bool:
    """Short, and carrying enough capitals to be a name rather than a phrase."""
    if not (2 <= len(text) <= 12) or " " in text.strip():
        return False
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and sum(c.isupper() for c in letters) >= 2


def _from_aliases(name: str, aliases: Dict[str, str]) -> Optional[str]:
    """User-configured overrides, matched as case-insensitive substrings."""
    lowered = name.lower()
    for pattern, acronym in aliases.items():
        if pattern.lower() in lowered:
            return acronym
    return None


def _from_table(name: str) -> Optional[str]:
    lowered = name.lower()
    for acronym, patterns in _KNOWN_VENUES:
        if any(pattern in lowered for pattern in patterns):
            return acronym
    return None


def normalize_venue(
    display_name: str,
    abbreviated_title: Optional[str] = None,
    alternate_titles: Optional[Sequence[str]] = None,
    aliases: Optional[Dict[str, str]] = None,
) -> str:
    """Return the short venue name for a source, or a trimmed full name."""
    name = (display_name or "").strip()
    if not name:
        return ""

    candidates = [name, *(alternate_titles or [])]
    aliases = aliases or {}

    for candidate in candidates:
        if hit := _from_aliases(candidate, aliases):
            return hit[:_MAX_VENUE_LEN]

    # Already short and capitalised — OpenAlex sometimes stores "SIGIR" itself.
    if _looks_like_acronym(name):
        return name

    for candidate in candidates:
        if hit := _from_table(candidate):
            return hit

    for candidate in candidates:
        if match := _PARENTHESISED.search(candidate):
            token = match.group(1)
            if _looks_like_acronym(token):
                return token

    if abbreviated_title and _looks_like_acronym(abbreviated_title.strip()):
        return abbreviated_title.strip()

    # Nothing recognised. Drop the "Proceedings of the 32nd Annual" prefix and
    # the trailing institution, so at least the distinguishing part of the name
    # is visible in a narrow column.
    trimmed = _TRAILING_PARENTHETICAL.sub("", name).strip()
    trimmed = _BOILERPLATE.sub("", trimmed).strip()
    return (trimmed or name)[:_MAX_VENUE_LEN]
