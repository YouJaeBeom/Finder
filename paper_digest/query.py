"""Search queries — the loose first pass, written the way people search.

A member's first pass is one query string, in the syntax every library database
uses (Scopus, Web of Science, PubMed, Google Scholar's advanced form)::

    query: >
      "political bias" OR "ideological bias" OR partisan
      OR ((LLM OR "large language model") AND (bias OR fairness OR audit))

The whole syntax:

``AND`` ``OR`` ``NOT``
    Operators only in capitals. Written in lower case they are ordinary words,
    which is what lets ``"search and rescue"`` mean the phrase. ``NOT`` binds
    tightest, then ``AND``, then ``OR`` — so ``a AND b OR c`` is ``(a AND b) OR
    c``, and parentheses override that. ``A NOT B`` is accepted as shorthand for
    ``A AND NOT B``, as in Web of Science.

phrases
    Adjacent words are one phrase: ``political bias`` matches the phrase and is
    identical to ``"political bias"``. There is no implicit AND, so a query can
    never quietly mean something looser than it reads. Quotes are needed only
    for a phrase containing parentheses or a capitalised AND/OR/NOT.

``*``
    Truncation. ``polari*`` covers polarization and polarisation, ``politic*``
    covers political, politics and politician. It stands for the rest of a
    word, so it cannot start a term.

``#``
    The rest of the line is a comment. A query is configuration and deserves
    the same annotation as the rest of the file.

Matching is over the title and abstract together, case-insensitively, with
hyphens and line breaks flattened to spaces (papers write "retrieval-augmented
generation" and "retrieval augmented generation" interchangeably, and an
abstract wraps mid-phrase). Terms match at a word boundary on the left only, so
``RAG`` does not hide inside "sto*rag*e" while ``bias`` still matches "biased".

Parse errors carry the offending line and a caret. This file is written by
researchers, not programmers, and "line 3, column 18" is not something anyone
should have to count out by hand.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern, Sequence, Tuple, Union

from .models import Paper

# Papers hyphenate the phrases this tool searches for — "retrieval-augmented
# generation", "LLM-as-a-judge", "data-centric AI" are the standard spellings.
# Matching them literally means missing the majority of hits, so dashes on both
# sides are flattened to spaces before comparison, as are the line breaks an
# abstract wraps a phrase across.
_DASHES = re.compile(r"[-‐-―]")
_SPACES = re.compile(r"\s+")

_OPERATORS = ("AND", "OR", "NOT")
_OPEN_QUOTES = "\"“"
_CLOSE_QUOTES = {"\"": "\"”", "“": "”\""}


def normalize(text: str) -> str:
    """The form both queries and paper text are compared in."""
    return _SPACES.sub(" ", _DASHES.sub(" ", text.lower())).strip()


class QueryError(ValueError):
    """A query that cannot be read, with the spot that stopped the reader."""

    def __init__(self, message: str, source: str, pos: int):
        self.source = source
        self.pos = pos
        super().__init__(f"{message}\n{_point_at(source, pos)}")


def _point_at(source: str, pos: int) -> str:
    """The offending line with a caret under *pos*, indented for a message."""
    pos = max(0, min(pos, len(source)))
    start = source.rfind("\n", 0, pos) + 1
    end = source.find("\n", pos)
    line = source[start:(len(source) if end < 0 else end)]
    stripped = line.lstrip()
    column = pos - start - (len(line) - len(stripped))
    return f"      {stripped}\n      {' ' * max(0, column)}^"


# ── The tree ──────────────────────────────────────────────────────────────────
#
# Four node kinds, each answering the same question: does this text satisfy me,
# and which of the member's own words are why. The second half is what fills the
# Notion Tags column, so a node reports labels only from the branch that
# actually carried it — an OR names the alternatives that hit, and an exclusion
# names nothing, since "this paper matched *not* survey" is not a tag.


@dataclass(frozen=True)
class Term:
    """One word or phrase: how it is matched, and how it was spelled."""

    label: str
    pattern: Pattern = field(compare=False, repr=False)

    def evaluate(self, text: str) -> Tuple[bool, List[str]]:
        if self.pattern.search(text):
            return True, [self.label]
        return False, []

    def describe(self) -> str:
        return f'"{self.label}"' if " " in self.label else self.label


@dataclass(frozen=True)
class All:
    """Every part must be satisfied."""

    parts: Tuple["Node", ...]

    def evaluate(self, text: str) -> Tuple[bool, List[str]]:
        hits: List[str] = []
        for part in self.parts:
            ok, found = part.evaluate(text)
            if not ok:
                return False, []
            hits.extend(found)
        return True, hits

    def describe(self) -> str:
        return "(" + " AND ".join(p.describe() for p in self.parts) + ")"


@dataclass(frozen=True)
class Any_:
    """At least one part must be satisfied."""

    parts: Tuple["Node", ...]

    def evaluate(self, text: str) -> Tuple[bool, List[str]]:
        # Every branch is evaluated rather than short-circuited: the tags are
        # worth more than the microseconds, and a paper that matched three of a
        # member's terms should say so.
        hits: List[str] = []
        matched = False
        for part in self.parts:
            ok, found = part.evaluate(text)
            if ok:
                matched = True
                hits.extend(found)
        return matched, hits

    def describe(self) -> str:
        return "(" + " OR ".join(p.describe() for p in self.parts) + ")"


@dataclass(frozen=True)
class Without:
    """The part must *not* be satisfied."""

    part: "Node"

    def evaluate(self, text: str) -> Tuple[bool, List[str]]:
        ok, _ = self.part.evaluate(text)
        return not ok, []

    def describe(self) -> str:
        return f"NOT {self.part.describe()}"


Node = Union[Term, All, Any_, Without]


@dataclass(frozen=True)
class Query:
    """A parsed query, plus the text it was written as."""

    root: Node
    source: str

    def match(self, text: str) -> Optional[List[str]]:
        """The terms that matched *text*, or None when the query does not apply.

        None rather than an empty list, because those are different answers: a
        query whose only satisfied branch is an exclusion matches and has
        nothing to name.
        """
        ok, hits = self.root.evaluate(normalize(text))
        if not ok:
            return None
        return list(dict.fromkeys(hits))

    def match_paper(self, paper: Paper) -> Optional[List[str]]:
        return self.match(f"{paper.title} {paper.abstract or ''}")

    def terms(self) -> List[str]:
        """Every term the query can be matched *by*, exclusions aside."""
        return _positive_terms(self.root)

    def describe(self) -> str:
        """The query as the parser read it, fully parenthesised.

        What ``members validate`` prints. Precedence bugs are invisible in the
        source and obvious here.
        """
        described = self.root.describe()
        if described.startswith("(") and described.endswith(")"):
            return described[1:-1]
        return described

    def is_negative_only(self) -> bool:
        """True when this query is satisfied by papers for lacking something.

        ``NOT survey`` reads like a filter and behaves like none at all: it
        admits every paper in the pool that is not a survey. Refused at
        registration rather than at runtime — see ``members.py``.
        """
        return _is_negative_only(self.root)


def _positive_terms(node: Node) -> List[str]:
    if isinstance(node, Term):
        return [node.label]
    if isinstance(node, Without):
        return []
    return [label for part in node.parts for label in _positive_terms(part)]


def _is_negative_only(node: Node) -> bool:
    if isinstance(node, Term):
        return False
    if isinstance(node, Without):
        return True
    if isinstance(node, Any_):
        # One open branch opens the whole query.
        return any(_is_negative_only(part) for part in node.parts)
    return all(_is_negative_only(part) for part in node.parts)


# ── Reading one ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Token:
    kind: str    # word | phrase | ( | ) | AND | OR | NOT
    text: str
    pos: int


def _tokenize(source: str) -> List[_Token]:
    tokens: List[_Token] = []
    i, size = 0, len(source)

    while i < size:
        char = source[i]

        if char.isspace():
            i += 1
        elif char == "#":
            end = source.find("\n", i)
            i = size if end < 0 else end
        elif char in "()":
            tokens.append(_Token(char, char, i))
            i += 1
        elif char in _OPEN_QUOTES:
            end = _closing_quote(source, i)
            tokens.append(_Token("phrase", source[i + 1:end], i))
            i = end + 1
        else:
            start = i
            while i < size and not source[i].isspace() and source[i] not in "()#" \
                    and source[i] not in _OPEN_QUOTES:
                i += 1
            word = source[start:i]
            kind = word if word in _OPERATORS else "word"
            tokens.append(_Token(kind, word, start))

    return tokens


def _closing_quote(source: str, opened_at: int) -> int:
    closers = _CLOSE_QUOTES[source[opened_at]]
    for i in range(opened_at + 1, len(source)):
        if source[i] in closers:
            return i
    raise QueryError("this quote is never closed", source, opened_at)


class _Reader:
    """Recursive descent over the tokens. One instance reads one query."""

    def __init__(self, source: str):
        self.source = source
        self.tokens = _tokenize(source)
        self.at = 0

    # ── position ──
    def peek(self) -> Optional[_Token]:
        return self.tokens[self.at] if self.at < len(self.tokens) else None

    def take(self) -> _Token:
        token = self.tokens[self.at]
        self.at += 1
        return token

    def fail(self, message: str, token: Optional[_Token] = None) -> "QueryError":
        pos = token.pos if token else len(self.source)
        return QueryError(message, self.source, pos)

    # ── grammar ──
    def read(self) -> Query:
        if not self.tokens:
            raise QueryError("the query is empty", self.source, 0)

        root = self.any_of()
        if (extra := self.peek()) is not None:
            if extra.kind == ")":
                raise self.fail("this ')' closes nothing", extra)
            raise self.fail(
                f"{extra.text!r} follows a complete expression — put AND, OR or "
                "NOT between them, or quote the whole phrase",
                extra,
            )
        return Query(root=root, source=self.source)

    def any_of(self) -> Node:
        parts = [self.all_of()]
        while (token := self.peek()) is not None and token.kind == "OR":
            self.take()
            parts.append(self.all_of())
        return parts[0] if len(parts) == 1 else Any_(tuple(parts))

    def all_of(self) -> Node:
        parts = [self.maybe_without()]
        while (token := self.peek()) is not None and token.kind in ("AND", "NOT"):
            self.take()
            operand = self.maybe_without()
            # "A NOT B" is Web of Science shorthand for "A AND NOT B".
            parts.append(Without(operand) if token.kind == "NOT" else operand)
        return parts[0] if len(parts) == 1 else All(tuple(parts))

    def maybe_without(self) -> Node:
        token = self.peek()
        if token is None:
            raise self.fail("the query ends on an operator, with nothing after it")
        if token.kind == "NOT":
            self.take()
            return Without(self.maybe_without())
        return self.single(token)

    def single(self, token: _Token) -> Node:
        if token.kind == "(":
            self.take()
            inner = self.any_of()
            closing = self.peek()
            if closing is None:
                raise self.fail("this '(' is never closed", token)
            if closing.kind != ")":
                raise self.fail(
                    f"expected ')' or an operator before {closing.text!r}",
                    closing,
                )
            self.take()
            return inner

        if token.kind == ")":
            raise self.fail("there is nothing inside these parentheses", token)

        if token.kind in ("AND", "OR"):
            raise self.fail(f"{token.text} needs something on its left", token)

        if token.kind == "phrase":
            self.take()
            return self.term(token.text, token.pos)

        # A run of bare words is one phrase. There is no implicit AND, so
        # `political bias` cannot silently mean something looser than it looks.
        words = [self.take()]
        while (following := self.peek()) is not None and following.kind == "word":
            words.append(self.take())

        for word in words:
            if word.text.upper() in _OPERATORS:
                raise self.fail(
                    f"{word.text!r} is an operator only in capitals — write "
                    f"{word.text.upper()}, or quote the phrase if you meant the word",
                    word,
                )

        return self.term(" ".join(word.text for word in words), words[0].pos)

    def term(self, label: str, pos: int) -> Term:
        text = normalize(label)
        if not text:
            raise self.fail("there is nothing to match here", _Token("word", label, pos))
        if text.startswith("*"):
            raise self.fail(
                "'*' stands for the rest of a word, so it cannot start a term — "
                "polari* matches polarization and polarisation",
                _Token("word", label, pos),
            )
        # Anchored at the start of a word so a short term cannot hide inside a
        # longer one — "RAG" was matching "sto*rag*e" and "pa*rag*raph". Only the
        # start is anchored, so plurals and suffixes ("LLMs", "biased") match.
        pattern = r"\b" + r"\w*".join(re.escape(part) for part in text.split("*"))
        return Term(label=label.strip(), pattern=re.compile(pattern))


def parse(source: str) -> Query:
    """Read one query, or raise :class:`QueryError` pointing at what stopped it."""
    return _Reader(source).read()


# ── Applying one ──────────────────────────────────────────────────────────────


def filter_papers(papers: Sequence[Paper], query: Query) -> List[Paper]:
    """Keep the papers the query matches, tagging each with what matched.

    Tags are written onto the papers themselves, which is right for the news
    stage — one query, one pass, no other reader — and wrong for members. See
    :func:`select_papers`.
    """
    kept: List[Paper] = []
    for paper in papers:
        if (matched := query.match_paper(paper)) is not None:
            paper.matched_keywords = matched
            kept.append(paper)
    return kept


def select_papers(papers: Sequence[Paper], query: Query) -> List[Paper]:
    """Like :func:`filter_papers`, but returns independent copies.

    This is the multi-member entry point. One collected pool is filtered once
    per person, and tagging the shared objects in place would give every member
    the *last* member's tags — and then, further down the pipeline, the last
    member's relevance score and the last member's note, since ranking and note
    generation also write onto the item.

    Copies are shallow apart from the three mutable fields that actually diverge
    per member. ``identifiers`` is shared on purpose: it is an identity, nothing
    downstream writes to it, and copying it would only cost memory.
    """
    from dataclasses import replace

    selected: List[Paper] = []
    for paper in papers:
        if (matched := query.match_paper(paper)) is not None:
            selected.append(replace(
                paper,
                matched_keywords=matched,
                authors=list(paper.authors),
                source=list(paper.source),
            ))
    return selected
