"""Keyword rules — AND, OR and exclusion.

A flat list is pure OR, and OR alone is too blunt: "LLM" on its own matches most
of cs.CL, which is the precision-over-recall failure the requirements call out.
"""
from __future__ import annotations

from paper_digest.keywords import compile_rules, filter_by_keywords, matches_keywords
from paper_digest.models import Paper, PaperIdentifiers


def _paper(title: str, abstract: str = "") -> Paper:
    return Paper(identifiers=PaperIdentifiers(), title=title, abstract=abstract)


class TestPlainStrings:
    def test_a_bare_string_still_matches_on_its_own(self):
        """The old flat-list config must keep working untouched."""
        kept = filter_by_keywords(
            [_paper("Instruction tuning at scale"), _paper("Sourdough baking")],
            ["instruction tuning"],
        )
        assert [p.title for p in kept] == ["Instruction tuning at scale"]

    def test_case_insensitive(self):
        assert matches_keywords(_paper("Scaling LLM Alignment"), ["llm"]) == ["llm"]

    def test_matches_the_abstract_too(self):
        paper = _paper("A title with nothing", "we study RLHF in depth")
        assert matches_keywords(paper, ["RLHF"]) == ["RLHF"]

    def test_empty_list_matches_nothing(self):
        assert filter_by_keywords([_paper("anything")], []) == []


class TestAllRule:
    """`all` is the point of the exercise: LLM alone is too wide a net."""

    RULES = [{"all": ["LLM", "alignment"]}]

    def test_requires_every_term(self):
        assert matches_keywords(_paper("LLM alignment via RLHF"), self.RULES)

    def test_one_term_alone_is_not_enough(self):
        assert matches_keywords(_paper("LLM inference speedups"), self.RULES) == []
        assert matches_keywords(_paper("alignment of robot arms"), self.RULES) == []

    def test_terms_may_be_split_across_title_and_abstract(self):
        paper = _paper("A study of LLM behaviour", "we focus on alignment")
        assert matches_keywords(paper, self.RULES)


class TestAnyRule:
    RULES = [{"any": ["RLHF", "DPO"]}]

    def test_one_hit_is_enough(self):
        assert matches_keywords(_paper("DPO beats PPO"), self.RULES) == ["DPO"]

    def test_records_every_hit(self):
        assert matches_keywords(_paper("RLHF and DPO compared"), self.RULES) == [
            "RLHF", "DPO"
        ]

    def test_no_hits_means_no_match(self):
        assert matches_keywords(_paper("Sourdough baking"), self.RULES) == []


class TestCombinedRule:
    RULES = [{"all": ["LLM"], "any": ["agent", "tool use"], "not": ["survey"]}]

    def test_all_plus_any(self):
        assert matches_keywords(_paper("LLM agent benchmarks"), self.RULES)

    def test_any_must_still_hit(self):
        assert matches_keywords(_paper("LLM quantization"), self.RULES) == []

    def test_exclusion_wins_over_a_match(self):
        assert matches_keywords(_paper("A survey of LLM agent methods"), self.RULES) == []


class TestRulesAreOredTogether:
    RULES = ["instruction tuning", {"all": ["LLM", "alignment"]}]

    def test_either_rule_matches(self):
        assert matches_keywords(_paper("Instruction tuning at scale"), self.RULES)
        assert matches_keywords(_paper("LLM alignment methods"), self.RULES)

    def test_neither_rule_matches(self):
        assert matches_keywords(_paper("Sourdough baking"), self.RULES) == []

    def test_tags_collect_terms_from_every_matching_rule(self):
        paper = _paper("Instruction tuning for LLM alignment")
        assert matches_keywords(paper, self.RULES) == [
            "instruction tuning", "LLM", "alignment"
        ]


class TestTagLabels:
    def test_notion_tags_use_the_config_spelling(self):
        """Matching lowercases; the tag should not."""
        kept = filter_by_keywords([_paper("scaling llm alignment")],
                                  [{"all": ["LLM", "Alignment"]}])
        assert kept[0].matched_keywords == ["LLM", "Alignment"]


class TestAlternativeGroups:
    """A nested list inside `all` means "any of these" — the synonym case.

    Without it, covering "LLM" / "large language model" / "language model"
    against three topics needs nine separate rules.
    """

    RULES = [{"all": [
        ["LLM", "large language model", "language model"],
        ["alignment", "safety"],
    ]}]

    def test_one_term_from_each_group_is_enough(self):
        assert matches_keywords(_paper("Large language model safety"), self.RULES)
        assert matches_keywords(_paper("LLM alignment"), self.RULES)

    def test_both_groups_must_be_satisfied(self):
        assert matches_keywords(_paper("LLM inference speedups"), self.RULES) == []
        assert matches_keywords(_paper("aircraft safety systems"), self.RULES) == []

    def test_records_the_terms_that_actually_hit(self):
        assert matches_keywords(_paper("LLM alignment"), self.RULES) == [
            "LLM", "alignment"
        ]

    def test_combines_with_exclusions(self):
        rules = [{"all": [["LLM"], ["agent"]], "not": ["survey"]}]
        assert matches_keywords(_paper("LLM agent design"), rules)
        assert matches_keywords(_paper("Survey of LLM agent design"), rules) == []


class TestMalformedRules:
    """One bad line in config.yaml should cost that line, not the whole run."""

    def test_a_rule_with_only_exclusions_is_dropped(self):
        assert compile_rules([{"not": ["survey"]}]) == []

    def test_an_unusable_entry_is_dropped_and_the_rest_survive(self):
        rules = compile_rules([12345, "LLM"])
        assert len(rules) == 1
        assert [t.label for t in rules[0].groups[0]] == ["LLM"]

    def test_unknown_keys_do_not_discard_a_usable_rule(self):
        rules = compile_rules([{"all": ["LLM"], "mode": "strict"}])
        assert len(rules) == 1
        assert [t.label for t in rules[0].groups[0]] == ["LLM"]


class TestSpellingRobustness:
    """Papers hyphenate the phrases this tool searches for."""

    def test_hyphenated_papers_match_unhyphenated_keywords(self):
        paper = _paper("Retrieval-Augmented Generation for Open-Domain QA")
        assert matches_keywords(paper, ["retrieval augmented generation"])

    def test_unhyphenated_papers_match_hyphenated_keywords(self):
        paper = _paper("Retrieval Augmented Generation at Scale")
        assert matches_keywords(paper, ["retrieval-augmented generation"])

    def test_llm_as_a_judge_in_both_spellings(self):
        for title in ("LLM-as-a-Judge: Evaluating Assistants",
                      "Using an LLM as a judge for evaluation"):
            assert matches_keywords(_paper(title), ["LLM-as-a-judge"]), title

    def test_en_dashes_are_handled_too(self):
        assert matches_keywords(_paper("Human–AI Interaction Patterns"),
                                ["human-ai interaction"])


class TestWordBoundaries:
    """A short keyword must not hide inside a longer word."""

    def test_rag_does_not_match_storage_or_paragraph(self):
        for title in ("Efficient Storage for Vector Databases",
                      "Paragraph-level Retrieval", "Average Precision Revisited"):
            assert matches_keywords(_paper(title), ["RAG"]) == [], title

    def test_rag_still_matches_the_real_thing(self):
        assert matches_keywords(_paper("A RAG pipeline for QA"), ["RAG"]) == ["RAG"]

    def test_suffixes_and_plurals_still_match(self):
        assert matches_keywords(_paper("LLMs are biased evaluators"), ["LLM"])
        assert matches_keywords(_paper("Measuring biases in search"), ["bias"])


class TestSharedTermLists:
    """YAML anchors are how a shared term list is reused across rules, and
    combining one with extra terms nests it a level deeper."""

    RULES = [{"all": [
        ["fairness"],
        [["LLM", "large language model"], ["retrieval", "ranking"]],
    ]}]

    def test_a_nested_group_is_flattened_into_alternatives(self):
        assert matches_keywords(_paper("Fairness in LLM evaluation"), self.RULES)
        assert matches_keywords(_paper("Fairness in ranking systems"), self.RULES)

    def test_the_group_is_still_a_requirement(self):
        assert matches_keywords(_paper("Fairness in hiring decisions"),
                                self.RULES) == []

    def test_nesting_does_not_invent_a_stringified_keyword(self):
        rules = compile_rules(self.RULES)
        labels = [t.label for group in rules[0].groups for t in group]
        assert labels == ["fairness", "LLM", "large language model",
                          "retrieval", "ranking"]
