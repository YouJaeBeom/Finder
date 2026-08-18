"""Venue names should read as CIKM, not as a 90-character proceedings title.

OpenAlex reports venues by full registered title, which is unreadable in a
Notion column and impossible to sort or group by.
"""
from __future__ import annotations

import pytest

from paper_digest.venues import (
    load_venues,
    normalize_venue,
    select_venues,
    venue_aliases_from_list,
)


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


class TestVenueKinds:
    """Conferences and journals are selected separately.

    Their scores mean different things — the conference numbers come from a
    community ranking, the journal ones are hand-set — and the pipeline fetches
    them in separate calls so one failing cannot cost the other.
    """

    def test_journals_and_conferences_are_both_in_the_shipped_list(self):
        kinds = {v.kind for v in load_venues()}
        assert kinds == {"conference", "journal"}

    def test_kind_narrows_the_selection(self):
        conferences = select_venues(min_score=0.5, kind="conference")
        journals = select_venues(min_score=0.5, kind="journal")

        assert conferences and journals
        assert not set(conferences) & set(journals)
        assert set(select_venues(min_score=0.5)) == set(conferences) | set(journals)

    def test_the_journals_worth_reading_are_there(self):
        journals = set(select_venues(min_score=0.5, kind="journal").values())
        # A sample across the areas the digest covers: IR, NLP, data, HCI and
        # the computational social science venues that publish on media bias.
        for abbr in ("TOIS", "TACL", "TKDE", "TPAMI", "JASIST", "PolComm"):
            assert abbr in journals, f"{abbr} dropped out of the journal list"

    def test_an_unknown_kind_is_an_error_not_an_empty_result(self):
        """Silently returning nothing would look like a quiet week forever."""
        with pytest.raises(ValueError, match="preprint"):
            select_venues(kind="preprint")


class TestLabCriticalVenues:
    """The venues this lab's topic cannot do without.

    The scores in venues.csv are a normalized Korean CS ranking, which weights
    systems and theory heavily and rates NLP, IR and computational social science
    venues low. At the old 0.5 cut that quietly removed ICWSM, NAACL, EACL,
    COLING, RecSys and CoNLL — and FAccT, AIES and ECIR were never in the ranking
    at all, so they had to be added by hand.

    The allowlist *is* the coverage: a venue missing here is a venue no member
    can ever receive a paper from. So this pins the ones that matter, against a
    future regeneration of the table from the ranking silently dropping them.
    """

    # Every one of these is measured against the live API, not guessed.
    REQUIRED = [
        "FAccT", "AIES", "ECIR",          # hand-added: fairness / ethics / IR
        "ICWSM", "NAACL", "EACL", "COLING", "CoNLL", "RecSys",  # below 0.5
        "ACL", "EMNLP", "SIGIR", "CIKM", "WSDM", "WWW",          # already high
    ]

    def test_each_one_is_in_the_table_and_collectable(self):
        by_abbr = {v.abbr: v for v in load_venues()}
        missing = [a for a in self.REQUIRED if a not in by_abbr]
        assert not missing, f"absent from venues.csv: {missing}"

        uncollectable = [a for a in self.REQUIRED if not by_abbr[a].collectable]
        assert not uncollectable, (
            f"present but papers=0, so silently never queried: {uncollectable}"
        )

    def test_the_shipped_min_score_actually_selects_them(self):
        """A venue in the table but under the configured cut is still invisible."""
        from paper_digest.config import load_config

        cfg = load_config("config.yaml")
        selected = set(select_venues(
            min_score=cfg.conferences.min_score,
            include=cfg.conferences.include,
            exclude=cfg.conferences.exclude,
            kind="conference",
        ).values())

        missing = [a for a in self.REQUIRED if a not in selected]
        assert not missing, (
            f"conferences.min_score={cfg.conferences.min_score} excludes {missing}"
        )

    def test_the_hand_added_venues_carry_what_s2_answers_with(self):
        """`name` has to be the exact string S2 returns, or the label lookup fails.

        normalize_venue maps the full registered name onto the abbreviation. A
        `name` that does not match byte-for-byte leaves papers filed under the
        long form — or, for a batch, dropped as "a venue we never asked for".
        """
        by_abbr = {v.abbr: v for v in load_venues()}
        measured = {
            "FAccT": "Conference on Fairness, Accountability and Transparency",
            "AIES": "AAAI/ACM Conference on AI, Ethics, and Society",
            "ECIR": "European Conference on Information Retrieval",
        }
        aliases = venue_aliases_from_list()
        for abbr, name in measured.items():
            assert by_abbr[abbr].name == name, f"{abbr} name drifted"
            # The same call the collector makes when it has to recover an
            # abbreviation from what a batch answered with.
            assert normalize_venue(name, aliases=aliases) == abbr

    def test_naacl_is_not_labelled_as_findings_only(self):
        """One S2 venue covers the main conference and Findings alike.

        The row arrived from the ranking as "NAACL Findings", which would have
        stamped that on every NAACL paper — 2,000+ of them, most from the main
        conference.
        """
        by_abbr = {v.abbr: v for v in load_venues()}
        assert "NAACL" in by_abbr
        assert "NAACL Findings" not in by_abbr
