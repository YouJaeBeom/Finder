"""Venue names should read as CIKM, not as a 90-character proceedings title.

OpenAlex reports venues by full registered title, which is unreadable in a
Notion column and impossible to sort or group by.
"""
from __future__ import annotations

import pytest

from paper_digest.venues import normalize_venue


class TestKnownVenues:
    @pytest.mark.parametrize("full_name,expected", [
        ("Proceedings of the 32nd ACM International Conference on Information "
         "and Knowledge Management", "CIKM"),
        ("Proceedings of the 17th ACM International Conference on Web Search "
         "and Data Mining", "WSDM"),
        ("Proceedings of the 47th International ACM SIGIR Conference on "
         "Research and Development in Information Retrieval", "SIGIR"),
        ("Annual Meeting of the Association for Computational Linguistics", "ACL"),
        ("Conference on Empirical Methods in Natural Language Processing", "EMNLP"),
        ("IEEE Transactions on Knowledge and Data Engineering", "TKDE"),
        ("Neural Information Processing Systems", "NeurIPS"),
        ("International Conference on Learning Representations", "ICLR"),
        ("International Conference on Machine Learning", "ICML"),
        ("Proceedings of the ACM Web Conference 2026", "WWW"),
        ("ACM Transactions on Information Systems", "TOIS"),
        ("Journal of Machine Learning Research", "JMLR"),
    ])
    def test_full_names_become_acronyms(self, full_name, expected):
        assert normalize_venue(full_name) == expected

    def test_naacl_is_not_swallowed_by_acl(self):
        """NAACL's full name contains ACL's — order in the table matters."""
        assert normalize_venue(
            "North American Chapter of the Association for Computational Linguistics"
        ) == "NAACL"

    def test_eacl_is_not_swallowed_by_acl(self):
        assert normalize_venue(
            "European Chapter of the Association for Computational Linguistics"
        ) == "EACL"

    def test_tacl_is_not_swallowed_by_acl(self):
        assert normalize_venue(
            "Transactions of the Association for Computational Linguistics"
        ) == "TACL"


class TestFallbacks:
    def test_an_acronym_is_left_alone(self):
        assert normalize_venue("SIGIR") == "SIGIR"
        assert normalize_venue("arXiv") == "arXiv"

    def test_parenthesised_acronym_is_extracted(self):
        assert normalize_venue(
            "Proceedings of the 19th Conference on Recommendations (RecSys '25)"
        ) == "RecSys"

    def test_abbreviated_title_is_used_when_nothing_else_matches(self):
        assert normalize_venue(
            "Journal of Some Very Specific Subfield",
            abbreviated_title="JSVSF",
        ) == "JSVSF"

    def test_a_prose_abbreviated_title_is_not_mistaken_for_an_acronym(self):
        assert normalize_venue(
            "Journal of Unmapped Studies",
            abbreviated_title="J. Unmapped Stud.",
        ) == "Journal of Unmapped Studies"

    def test_alternate_titles_are_searched_too(self):
        assert normalize_venue(
            "Some Registered Name Nobody Uses",
            alternate_titles=["International Conference on Data Engineering"],
        ) == "ICDE"

    def test_unknown_venue_keeps_its_name_minus_boilerplate(self):
        assert normalize_venue(
            "Proceedings of the 4th Workshop on Niche Topics"
        ) == "Workshop on Niche Topics"

    def test_operating_institution_is_dropped(self):
        """OpenAlex registers repositories with their operator in parentheses."""
        assert normalize_venue(
            "Zenodo (CERN European Organization for Nuclear Research)"
        ) == "Zenodo"
        assert normalize_venue("arXiv (Cornell University)") == "arXiv"

    def test_a_parenthesised_acronym_still_wins_over_stripping(self):
        assert normalize_venue(
            "Proceedings of the Conference on Odd Things (COT '25)"
        ) == "COT"

    def test_empty_name_stays_empty(self):
        assert normalize_venue("") == ""


class TestConfigAliases:
    ALIASES = {"Information and Knowledge Management": "CIKM-KR",
               "Workshop on Niche Topics": "NICHE"}

    def test_aliases_win_over_the_built_in_table(self):
        assert normalize_venue(
            "Proceedings of the 32nd ACM International Conference on "
            "Information and Knowledge Management",
            aliases=self.ALIASES,
        ) == "CIKM-KR"

    def test_aliases_cover_venues_the_table_does_not_know(self):
        assert normalize_venue(
            "Proceedings of the 4th Workshop on Niche Topics",
            aliases=self.ALIASES,
        ) == "NICHE"


class TestNotionLimits:
    def test_result_fits_a_notion_select_option(self):
        """Notion rejects a select option longer than 100 characters."""
        assert len(normalize_venue("Workshop on " + "Very " * 60 + "Long")) <= 100
