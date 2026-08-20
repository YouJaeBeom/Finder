"""The first-pass query — the one file in this repo a non-programmer writes.

Two things are being pinned here. One is the semantics: AND/OR/NOT, precedence,
truncation, and what a paper is tagged with when it matches. The other is the
refusals, which matter just as much — a query that silently means something
looser or narrower than it reads costs a member papers for a month before
anyone notices, so every ambiguous spelling has to fail loudly at registration.
"""
from __future__ import annotations

import pytest

from paper_digest.models import Paper, PaperIdentifiers
from paper_digest.query import (
    QueryError,
    filter_papers,
    parse,
    select_papers,
)


def _paper(title: str, abstract: str = "") -> Paper:
    return Paper(identifiers=PaperIdentifiers(), title=title, abstract=abstract)


def _hits(query: str, title: str, abstract: str = ""):
    return parse(query).match_paper(_paper(title, abstract))


class TestPhrases:
    def test_adjacent_words_are_one_phrase_not_an_implicit_and(self):
        assert _hits("political bias", "Political Bias in LLMs") == ["political bias"]
        # The words are both present and the phrase is not — an implicit AND
        # would match here, which is the whole reason there isn't one.
        assert _hits("political bias", "Bias in political science") is None

    def test_quoting_a_phrase_means_the_same_thing(self):
        assert _hits('"political bias"', "Political bias measured") == ["political bias"]

    def test_case_is_ignored_when_matching(self):
        assert _hits("llm", "Scaling LLM Alignment") == ["llm"]

    def test_the_abstract_counts_too(self):
        assert _hits("RLHF", "A title with nothing", "we study RLHF in depth") == ["RLHF"]

    def test_a_phrase_may_span_a_line_break(self):
        assert _hits("political bias", "Measuring political\nbias in models")


class TestOperators:
    def test_or_takes_either_side(self):
        query = '"political bias" OR partisan'
        assert _hits(query, "A partisan model") == ["partisan"]
        assert _hits(query, "Political bias in ranking") == ["political bias"]
        assert _hits(query, "Sourdough baking") is None

    def test_and_requires_both(self):
        query = "LLM AND alignment"
        assert _hits(query, "LLM alignment via RLHF") == ["LLM", "alignment"]
        assert _hits(query, "LLM inference speedups") is None
        assert _hits(query, "Alignment of robot arms") is None

    def test_and_may_be_satisfied_across_title_and_abstract(self):
        assert _hits("LLM AND alignment", "A study of LLM behaviour",
                     "we focus on alignment")

    def test_not_excludes(self):
        query = "LLM AND NOT survey"
        assert _hits(query, "LLM agent design") == ["LLM"]
        assert _hits(query, "A survey of LLM agent design") is None

    def test_not_is_also_accepted_as_a_binary_operator(self):
        """`A NOT B` is how Web of Science spells `A AND NOT B`."""
        assert _hits("LLM NOT survey", "LLM agent design") == ["LLM"]
        assert _hits("LLM NOT survey", "A survey of LLM design") is None

    def test_and_binds_tighter_than_or(self):
        query = "LLM AND bias OR retrieval"
        assert parse(query).describe() == "(LLM AND bias) OR retrieval"
        assert _hits(query, "Retrieval for open-domain QA") == ["retrieval"]
        assert _hits(query, "LLM inference speedups") is None

    def test_parentheses_override_precedence(self):
        query = "LLM AND (bias OR retrieval)"
        assert _hits(query, "Retrieval for open-domain QA") is None
        assert _hits(query, "LLM retrieval augmentation") == ["LLM", "retrieval"]

    def test_the_synonym_shape_a_real_member_file_uses(self):
        query = ('(LLM OR "large language model") AND '
                 '(bias OR fairness OR evaluation)')
        assert _hits(query, "Large language model fairness")
        assert _hits(query, "LLM bias benchmarks")
        assert _hits(query, "LLM inference speedups") is None
        assert _hits(query, "Fairness in hiring decisions") is None


class TestTruncation:
    def test_a_trailing_star_covers_the_rest_of_the_word(self):
        for title in ("Measuring polarisation", "A polarized electorate",
                      "Polarization dynamics"):
            assert _hits("polari*", title), title

    def test_it_is_still_anchored_at_the_start_of_a_word(self):
        assert _hits("politic*", "Metropolitan search behaviour") is None

    def test_it_may_sit_inside_a_phrase(self):
        assert _hits("politic* bias", "Political bias in LLMs")

    def test_a_leading_star_is_refused(self):
        with pytest.raises(QueryError, match=r"cannot start a term"):
            parse("*bias")


class TestComments:
    def test_a_hash_comments_out_the_rest_of_the_line(self):
        query = ('# 이 분야 고유 표현\n'
                 '"political bias" OR partisan   # 정치 쪽\n'
                 'OR sycophancy')
        assert _hits(query, "Sycophancy in assistants") == ["sycophancy"]
        assert parse(query).terms() == ["political bias", "partisan", "sycophancy"]

    def test_a_comment_cannot_swallow_the_next_line(self):
        assert _hits("# nothing here\nLLM", "LLM agents") == ["LLM"]


class TestSpellingRobustness:
    """Papers hyphenate the phrases this tool searches for."""

    def test_hyphenated_papers_match_unhyphenated_queries(self):
        assert _hits("retrieval augmented generation",
                     "Retrieval-Augmented Generation for Open-Domain QA")

    def test_unhyphenated_papers_match_hyphenated_queries(self):
        assert _hits("retrieval-augmented generation",
                     "Retrieval Augmented Generation at Scale")

    def test_llm_as_a_judge_in_both_spellings(self):
        for title in ("LLM-as-a-Judge: Evaluating Assistants",
                      "Using an LLM as a judge for evaluation"):
            assert _hits("LLM-as-a-judge", title), title

    def test_en_dashes_are_handled_too(self):
        assert _hits("human-ai interaction", "Human–AI Interaction Patterns")


class TestWordBoundaries:
    """A short term must not hide inside a longer word."""

    def test_rag_does_not_match_storage_or_paragraph(self):
        for title in ("Efficient Storage for Vector Databases",
                      "Paragraph-level Retrieval", "Average Precision Revisited"):
            assert _hits("RAG", title) is None, title

    def test_rag_still_matches_the_real_thing(self):
        assert _hits("RAG", "A RAG pipeline for QA") == ["RAG"]

    def test_suffixes_and_plurals_still_match(self):
        assert _hits("LLM", "LLMs are biased evaluators")
        assert _hits("bias", "Measuring biases in search")


class TestTags:
    """What lands in the Notion Tags column."""

    def test_tags_keep_the_spelling_the_member_wrote(self):
        kept = filter_papers([_paper("scaling llm alignment")],
                             parse("LLM AND Alignment"))
        assert kept[0].matched_keywords == ["LLM", "Alignment"]

    def test_every_matching_alternative_is_recorded(self):
        assert _hits("RLHF OR DPO", "RLHF and DPO compared") == ["RLHF", "DPO"]

    def test_an_unsatisfied_branch_contributes_nothing(self):
        query = '"instruction tuning" OR (LLM AND alignment)'
        assert _hits(query, "Instruction tuning at scale") == ["instruction tuning"]

    def test_but_two_satisfied_branches_both_do(self):
        query = '"instruction tuning" OR (LLM AND alignment)'
        assert _hits(query, "Instruction tuning for LLM alignment") == [
            "instruction tuning", "LLM", "alignment"
        ]

    def test_an_exclusion_is_never_a_tag(self):
        assert _hits("LLM NOT survey", "LLM agent design") == ["LLM"]

    def test_a_term_matching_twice_is_tagged_once(self):
        assert _hits("(LLM OR bias) AND (LLM OR safety)", "LLM safety") == [
            "LLM", "safety"
        ]


class TestRefusals:
    """Every message has to name the spot. Members write these, not engineers."""

    @pytest.mark.parametrize("query, expected", [
        ("LLM and bias", "operator only in capitals"),
        ("(LLM OR GPT) bias", "follows a complete expression"),
        ("LLM AND", "ends on an operator"),
        ("(LLM OR bias", "never closed"),
        ("LLM OR bias)", "closes nothing"),
        ('"unclosed phrase', "quote is never closed"),
        ("()", "nothing inside these parentheses"),
        ("AND bias", "needs something on its left"),
        ("   ", "the query is empty"),
        ("# 주석뿐", "the query is empty"),
    ])
    def test_the_shapes_that_are_refused(self, query, expected):
        with pytest.raises(QueryError, match=expected):
            parse(query)

    def test_the_message_points_at_the_offending_word(self):
        with pytest.raises(QueryError) as exc:
            parse("bias\nLLM and fairness")
        message = str(exc.value)
        assert "LLM and fairness" in message
        assert message.rstrip().endswith("^")
        caret_line, source_line = message.splitlines()[-1], message.splitlines()[-2]
        assert source_line.index("and") == caret_line.index("^")

    def test_lower_case_operators_are_refused_rather_than_read_as_words(self):
        """`LLM and bias` reads as an AND and would silently match neither."""
        with pytest.raises(QueryError, match="write AND"):
            parse("LLM and bias")

    def test_but_quoting_makes_them_ordinary_words_again(self):
        assert _hits('"search and rescue"', "Search and rescue robotics")


class TestNegativeOnly:
    """A query that only excludes admits the whole pool. Caught, not run."""

    @pytest.mark.parametrize("query", [
        "NOT survey",
        "NOT survey AND NOT dataset",
        "LLM OR NOT survey",
    ])
    def test_it_is_reported(self, query):
        assert parse(query).is_negative_only()

    @pytest.mark.parametrize("query", ["LLM NOT survey", "LLM AND NOT survey", "LLM"])
    def test_a_real_query_with_an_exclusion_is_fine(self, query):
        assert not parse(query).is_negative_only()


class TestDescribe:
    """`members validate` prints this. A precedence slip has to be visible."""

    def test_it_parenthesises_what_the_reader_actually_grouped(self):
        assert parse("a OR b AND c").describe() == "a OR (b AND c)"
        assert parse("(a OR b) AND c").describe() == "(a OR b) AND c"

    def test_phrases_come_back_quoted(self):
        assert parse("political bias OR llm").describe() == '"political bias" OR llm'

    def test_a_binary_not_is_shown_as_the_and_not_it_is(self):
        assert parse("LLM NOT survey").describe() == "LLM AND NOT survey"


class TestApplyingIt:
    QUERY = "LLM OR retrieval"

    def test_filter_keeps_the_matches_and_tags_them_in_place(self):
        papers = [_paper("LLM agents"), _paper("Sourdough baking")]
        kept = filter_papers(papers, parse(self.QUERY))
        assert [p.title for p in kept] == ["LLM agents"]
        assert papers[0].matched_keywords == ["LLM"]

    def test_select_returns_copies_so_members_do_not_overwrite_each_other(self):
        """Tagging shared objects would give everyone the last member's tags."""
        pool = [_paper("LLM retrieval augmentation")]
        first = select_papers(pool, parse("LLM"))
        second = select_papers(pool, parse("retrieval"))

        assert first[0].matched_keywords == ["LLM"]
        assert second[0].matched_keywords == ["retrieval"]
        assert pool[0].matched_keywords == []
        assert first[0] is not second[0]

    def test_the_copies_do_not_share_mutable_fields(self):
        pool = [_paper("LLM agents")]
        pool[0].authors = ["Alice"]
        copy = select_papers(pool, parse("LLM"))[0]
        copy.authors.append("Bob")
        assert pool[0].authors == ["Alice"]
