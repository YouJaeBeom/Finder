"""Entry point for ``python -m paper_digest``."""
from __future__ import annotations

import argparse
import sys


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the lab config (default: config.yaml)",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="paper_digest",
        description="Weekly peer-reviewed research digest → Notion, per lab member",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Run the collection pipeline")
    run_p.add_argument(
        "--mode",
        choices=["weekly", "backfill"],
        required=True,
        help="weekly: the scheduled digest. backfill: one-off catch-up",
    )
    run_p.add_argument(
        "--member",
        default=None,
        help="Serve only this member ID (the YAML filename without .yaml). "
             "Backfill a newcomer without re-billing the whole lab, or retry "
             "one member after a failure.",
    )
    run_p.add_argument(
        "--days",
        type=int,
        default=365,
        help="Backfill window in days (default: 365)",
    )
    run_p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Backfill: top-ranked papers to keep per member (default: 200)",
    )
    run_p.add_argument(
        "--sources",
        choices=["conferences", "journals", "both"],
        default="both",
        help="Backfill: which venue classes to catch up on (default: both)",
    )
    _add_config_arg(run_p)

    # init
    init_p = sub.add_parser(
        "init",
        help="Create the Notion structure: news database and every member space",
    )
    _add_config_arg(init_p)

    # members
    members_p = sub.add_parser("members", help="Inspect the member configuration")
    members_sub = members_p.add_subparsers(dest="members_command", required=True)
    for name, helptext in (
        ("list", "Show every configured member"),
        ("validate", "Check every member file and report all problems"),
    ):
        child = members_sub.add_parser(name, help=helptext)
        _add_config_arg(child)

    args = parser.parse_args()

    if args.command == "run":
        from .pipeline import run_backfill, run_weekly

        if args.mode == "weekly":
            sys.exit(run_weekly(args.config, only=args.member))
        sys.exit(run_backfill(args.config, args.days, args.limit, args.sources,
                              only=args.member))

    if args.command == "init":
        from .pipeline import run_init

        sys.exit(run_init(args.config))

    if args.command == "members":
        sys.exit(_members_command(args.members_command, args.config))


def _members_command(action: str, config_path: str) -> int:
    """``members list`` / ``members validate``.

    Both do the same work — loading is what validates — and differ only in what
    they print. Kept out of the pipeline module because neither touches Notion or
    an LLM, so this stays runnable with no secrets set at all.
    """
    from .config import load_config
    from .members import MemberConfigError, load_members

    try:
        cfg = load_config(config_path)
    except Exception as exc:
        print(f"could not read {config_path}: {exc}", file=sys.stderr)
        return 1

    try:
        members = load_members(
            cfg.members_dir,
            max_members=cfg.limits.max_members,
            max_top_n=cfg.limits.max_top_n_per_member,
        )
    except MemberConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if action == "validate":
        print(f"✓ {len(members)} member(s) in {cfg.members_dir}/ — no problems found")
        return 0

    total = 0
    for member in members:
        print(f"{member.member_id:16} {member.name:14} top_n={member.top_n:<4} "
              f"keywords={len(member.keywords):<4} {member.source_path}")
        total += member.top_n
    news = cfg.news.top_n if cfg.news.enabled else 0
    print(f"\n{len(members)} member(s). Up to {total} paper notes + {news} news "
          f"notes per run (lab limit {cfg.limits.max_notes_per_run}).")
    return 0


if __name__ == "__main__":
    main()
