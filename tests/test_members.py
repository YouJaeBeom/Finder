"""Member files: loading, validation, and the limits that are refused not clamped.

Registration is the step an operator does by hand for someone else, so this is
where a mistake is cheapest to catch and most expensive to miss — a member whose
file is subtly wrong receives a quietly worse digest every week, with nothing in
the run log saying so.
"""
from __future__ import annotations

import pytest

from paper_digest.members import Member, MemberConfigError, load_members
from tests.conftest import write_member


def _dir(tmp_path):
    return str(tmp_path / "members")


class TestLoading:
    def test_members_are_ordered_by_id(self, tmp_path):
        for member_id in ("zoe", "adam", "mia"):
            write_member(_dir(tmp_path), member_id)

        ids = [m.member_id for m in load_members(_dir(tmp_path))]
        assert ids == ["adam", "mia", "zoe"], (
            "a run that reorders itself week to week makes its own logs unreadable"
        )

    def test_the_filename_is_the_member_id(self, tmp_path):
        write_member(_dir(tmp_path), "jaebeom", name="유재범")
        member = load_members(_dir(tmp_path))[0]
        assert member.member_id == "jaebeom"
        assert member.name == "유재범"

    def test_disabled_members_are_left_out(self, tmp_path):
        write_member(_dir(tmp_path), "active")
        write_member(_dir(tmp_path), "graduated", enabled=False)

        assert [m.member_id for m in load_members(_dir(tmp_path))] == ["active"]

    def test_the_scoring_cache_is_per_member(self, tmp_path):
        write_member(_dir(tmp_path), "a")
        write_member(_dir(tmp_path), "b")
        a, b = load_members(_dir(tmp_path))
        assert a.scored_cache_path() != b.scored_cache_path()
        assert a.scored_cache_path().endswith("a.json")

    def test_yaml_extension_is_accepted_too(self, tmp_path):
        write_member(_dir(tmp_path), "a")
        (tmp_path / "members" / "b.yml").write_text(
            'name: b\ntop_n: 5\nresearch_profile: "p"\nkeywords: ["LLM"]\n',
            encoding="utf-8",
        )
        assert len(load_members(_dir(tmp_path))) == 2

    def test_anchor_keys_are_not_mistaken_for_settings(self, tmp_path):
        """YAML anchors have to live as real keys, so unknown keys are allowed."""
        (tmp_path / "members").mkdir(parents=True)
        (tmp_path / "members" / "a.yaml").write_text(
            '_model: &model ["LLM", "language model"]\n'
            'name: "가"\n'
            'top_n: 5\n'
            'research_profile: "프로필"\n'
            "keywords:\n"
            '  - all: [*model, ["bias"]]\n',
            encoding="utf-8",
        )
        member = load_members(_dir(tmp_path))[0]
        assert len(member.keywords) == 1


class TestValidation:
    def test_a_missing_directory_says_so(self, tmp_path):
        with pytest.raises(MemberConfigError, match="No members directory"):
            load_members(str(tmp_path / "nope"))

    def test_an_empty_directory_says_so(self, tmp_path):
        (tmp_path / "members").mkdir()
        with pytest.raises(MemberConfigError, match="No member files"):
            load_members(_dir(tmp_path))

    @pytest.mark.parametrize("body,expected", [
        ('top_n: 5\nresearch_profile: "p"\nkeywords: ["LLM"]\n', "'name' is required"),
        ('name: "가"\ntop_n: 5\nkeywords: ["LLM"]\n', "'research_profile' is required"),
        ('name: "가"\ntop_n: 5\nresearch_profile: "p"\nkeywords: []\n',
         "'keywords' must be a non-empty list"),
        ('name: "가"\ntop_n: 0\nresearch_profile: "p"\nkeywords: ["LLM"]\n',
         "'top_n' must be a positive integer"),
        ('name: "가"\ntop_n: "many"\nresearch_profile: "p"\nkeywords: ["LLM"]\n',
         "'top_n' must be a positive integer"),
        ('name: "가"\nenabled: "yes"\ntop_n: 5\nresearch_profile: "p"\n'
         'keywords: ["LLM"]\n', "'enabled' must be true or false"),
        ("- not a mapping\n", "expected a YAML mapping"),
    ])
    def test_each_missing_or_wrong_field_is_named(self, tmp_path, body, expected):
        (tmp_path / "members").mkdir()
        (tmp_path / "members" / "a.yaml").write_text(body, encoding="utf-8")

        with pytest.raises(MemberConfigError, match=expected):
            load_members(_dir(tmp_path))

    def test_every_problem_is_reported_at_once(self, tmp_path):
        """Fixing them one monthly run at a time is how a month gets skipped."""
        (tmp_path / "members").mkdir()
        (tmp_path / "members" / "a.yaml").write_text("top_n: 5\n", encoding="utf-8")
        (tmp_path / "members" / "b.yaml").write_text('name: "나"\n', encoding="utf-8")

        with pytest.raises(MemberConfigError) as exc:
            load_members(_dir(tmp_path))

        message = str(exc.value)
        assert "a.yaml" in message and "b.yaml" in message
        assert "problem(s)" in message

    def test_a_malformed_keyword_rule_is_a_problem_not_a_silent_drop(self, tmp_path):
        """compile_rules drops bad entries at runtime — right then, wrong here."""
        (tmp_path / "members").mkdir()
        (tmp_path / "members" / "a.yaml").write_text(
            'name: "가"\ntop_n: 5\nresearch_profile: "p"\n'
            "keywords:\n"
            '  - "political bias"\n'
            '  - not: ["survey"]\n',       # exclusions only: matches everything
            encoding="utf-8",
        )
        with pytest.raises(MemberConfigError, match="malformed"):
            load_members(_dir(tmp_path))

    def test_two_members_may_not_share_a_display_name(self, tmp_path):
        """Pages resolve by title, so duplicates merge two people's digests."""
        write_member(_dir(tmp_path), "a", name="유재범")
        write_member(_dir(tmp_path), "b", name="유재범")

        with pytest.raises(MemberConfigError, match="already used by"):
            load_members(_dir(tmp_path))

    def test_a_filename_that_is_not_a_usable_id_is_rejected(self, tmp_path):
        (tmp_path / "members").mkdir()
        (tmp_path / "members" / "who is this.yaml").write_text(
            'name: "가"\ntop_n: 5\nresearch_profile: "p"\nkeywords: ["LLM"]\n',
            encoding="utf-8",
        )
        with pytest.raises(MemberConfigError, match="not a usable member ID"):
            load_members(_dir(tmp_path))


class TestLimits:
    def test_top_n_over_the_lab_limit_is_refused(self, tmp_path):
        write_member(_dir(tmp_path), "greedy", top_n=300)

        with pytest.raises(MemberConfigError, match="exceeds the lab limit of 30"):
            load_members(_dir(tmp_path), max_top_n=30)

    def test_the_limit_is_not_silently_clamped(self, tmp_path):
        """A clamp would serve fewer papers than the file asks for, invisibly."""
        write_member(_dir(tmp_path), "greedy", top_n=300)
        with pytest.raises(MemberConfigError):
            load_members(_dir(tmp_path), max_top_n=30)

    def test_too_many_members_is_refused(self, tmp_path):
        for i in range(4):
            write_member(_dir(tmp_path), f"m{i}")

        with pytest.raises(MemberConfigError, match="exceeds the lab limit of 3"):
            load_members(_dir(tmp_path), max_members=3)

    def test_disabled_members_do_not_count_toward_the_cap(self, tmp_path):
        for i in range(3):
            write_member(_dir(tmp_path), f"m{i}")
        write_member(_dir(tmp_path), "gone", enabled=False)

        assert len(load_members(_dir(tmp_path), max_members=3)) == 3


class TestEffectiveConfig:
    def test_member_values_replace_the_lab_placeholders(self):
        from paper_digest.config import Config
        from paper_digest.members import effective_config

        lab = Config(days_back=30, max_papers_to_rank=1500, top_n=99)
        member = Member(member_id="a", name="가", research_profile="내 프로필",
                        keywords=["political bias"], top_n=7)

        merged = effective_config(lab, member)
        assert merged.research_profile == "내 프로필"
        assert merged.top_n == 7
        assert merged.keywords == ["political bias"]
        # Lab-wide fields survive untouched.
        assert merged.days_back == 30
        assert merged.max_papers_to_rank == 1500
        # And the lab config is not mutated.
        assert lab.top_n == 99
        assert lab.research_profile == ""


class TestNoPerPersonLimit:
    """A member who sets no top_n receives everything above the relevance cutoff.

    Capping by count and capping by relevance are different promises. A count
    keeps the best twenty and drops the twenty-first without telling anyone,
    which is exactly the silent miss the digest exists to prevent — so the
    cutoff, not a number, is what decides.
    """

    def test_an_absent_top_n_means_no_limit(self, tmp_path):
        write_member(tmp_path / "m", "a", top_n=None)
        assert load_members(str(tmp_path / "m"))[0].top_n is None

    def test_an_explicit_top_n_is_still_honoured(self, tmp_path):
        write_member(tmp_path / "m", "a", top_n=7)
        assert load_members(str(tmp_path / "m"))[0].top_n == 7

    def test_an_unlimited_member_is_not_measured_against_the_lab_cap(self, tmp_path):
        """max_top_n_per_member bounds a number; there is no number to bound."""
        write_member(tmp_path / "m", "a", top_n=None)
        assert load_members(str(tmp_path / "m"), max_top_n=3)[0].top_n is None

    def test_an_explicit_top_n_over_the_lab_cap_is_still_refused(self, tmp_path):
        write_member(tmp_path / "m", "a", top_n=99)
        with pytest.raises(MemberConfigError, match="exceeds the lab limit"):
            load_members(str(tmp_path / "m"), max_top_n=30)

    @pytest.mark.parametrize("bad", [0, -1, "many", 3.5, True])
    def test_a_top_n_that_is_present_but_nonsense_is_refused(self, tmp_path, bad):
        write_member(tmp_path / "m", "a", top_n=bad)
        with pytest.raises(MemberConfigError, match="top_n"):
            load_members(str(tmp_path / "m"))
