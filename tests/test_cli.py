"""The command line: what each subcommand dispatches to, and what it exits with.

Thin by design, but it is the only surface a lab member touches directly —
``members validate`` is the step that catches a typo before Monday — so the
argument wiring is worth pinning. In particular, ``members`` must run with no
secrets set at all: it never reaches Notion or an LLM, and requiring a token to
check a YAML file would push people into editing blind.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from paper_digest.__main__ import main
from tests.conftest import write_lab_config, write_member


def _run(*argv) -> int:
    with patch("sys.argv", ["paper_digest", *argv]):
        with pytest.raises(SystemExit) as exc:
            main()
    return exc.value.code


class TestMembersCommands:
    def test_list_prints_every_member_and_the_run_total(self, tmp_path, capsys):
        config = write_lab_config(
            tmp_path,
            members=(("jaebeom", "유재범", 10), ("minsu", "김민수", 15)),
        )
        assert _run("members", "list", "--config", config) == 0

        out = capsys.readouterr().out
        assert "jaebeom" in out and "유재범" in out
        assert "minsu" in out and "김민수" in out
        assert "Up to 25 paper notes" in out, "the per-run total is the point of the view"

    def test_validate_succeeds_quietly(self, tmp_path, capsys):
        config = write_lab_config(tmp_path)
        assert _run("members", "validate", "--config", config) == 0
        assert "no problems found" in capsys.readouterr().out

    def test_validate_reports_the_problem_and_exits_one(self, tmp_path, capsys):
        config = write_lab_config(tmp_path)
        (tmp_path / "members" / "broken.yaml").write_text(
            'name: "이름만"\n', encoding="utf-8"
        )
        assert _run("members", "validate", "--config", config) == 1

        err = capsys.readouterr().err
        assert "broken.yaml" in err
        assert "research_profile" in err and "keywords" in err

    def test_it_needs_no_secrets(self, tmp_path, monkeypatch):
        """Checking a YAML file must not require a token."""
        for var in ("NOTION_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
            monkeypatch.delenv(var, raising=False)
        config = write_lab_config(tmp_path)
        assert _run("members", "validate", "--config", config) == 0

    def test_a_missing_config_is_reported_not_traced(self, tmp_path, capsys):
        assert _run("members", "list", "--config", str(tmp_path / "nope.yaml")) == 1
        assert "could not read" in capsys.readouterr().err

    def test_a_disabled_member_is_left_out_of_the_listing(self, tmp_path, capsys):
        config = write_lab_config(tmp_path, members=(("here", "있는사람", 10),))
        write_member(tmp_path / "members", "gone", name="졸업생", enabled=False)

        assert _run("members", "list", "--config", config) == 0
        out = capsys.readouterr().out
        assert "있는사람" in out and "졸업생" not in out

    def test_members_requires_a_subcommand(self):
        with patch("sys.argv", ["paper_digest", "members"]):
            with pytest.raises(SystemExit) as exc:
                main()
        assert exc.value.code == 2


class TestRunDispatch:
    def test_monthly_passes_the_config_and_no_member(self):
        with patch("paper_digest.pipeline.run_monthly", return_value=0) as run:
            assert _run("run", "--mode", "monthly", "--config", "c.yaml") == 0
        run.assert_called_once_with("c.yaml", only=None)

    def test_monthly_forwards_the_member_filter(self):
        with patch("paper_digest.pipeline.run_monthly", return_value=0) as run:
            _run("run", "--mode", "monthly", "--member", "jaebeom")
        run.assert_called_once_with("config.yaml", only="jaebeom")

    def test_the_pipelines_exit_code_is_the_processs_exit_code(self):
        with patch("paper_digest.pipeline.run_monthly", return_value=1):
            assert _run("run", "--mode", "monthly") == 1

    def test_backfill_forwards_every_option(self):
        with patch("paper_digest.pipeline.run_backfill", return_value=0) as run:
            _run("run", "--mode", "backfill", "--days", "180", "--limit", "50",
                 "--sources", "conferences", "--member", "newbie")
        run.assert_called_once_with("config.yaml", 180, 50, "conferences",
                                    only="newbie")

    def test_backfill_defaults_are_the_documented_ones(self):
        with patch("paper_digest.pipeline.run_backfill", return_value=0) as run:
            _run("run", "--mode", "backfill")
        run.assert_called_once_with("config.yaml", 365, 200, "both", only=None)

    def test_an_unknown_mode_is_refused_by_the_parser(self):
        assert _run("run", "--mode", "daily") == 2

    def test_the_old_weekly_mode_is_gone(self):
        """The cadence moved to monthly; a stale cron must fail loudly, not quietly."""
        assert _run("run", "--mode", "weekly") == 2

    def test_an_unknown_backfill_source_is_refused_by_the_parser(self):
        """Rejected here rather than collecting nothing and reporting success."""
        assert _run("run", "--mode", "backfill", "--sources", "preprints") == 2

    def test_batch_mode_is_gone(self):
        """No preprint source remains, so stamping an accepted venue has no job."""
        assert _run("run", "--mode", "batch") == 2


class TestInit:
    def test_init_dispatches_with_the_config(self):
        with patch("paper_digest.pipeline.run_init", return_value=0) as run:
            assert _run("init", "--config", "lab.yaml") == 0
        run.assert_called_once_with("lab.yaml")


class TestNoCommand:
    def test_bare_invocation_shows_usage(self):
        assert _run() == 2
