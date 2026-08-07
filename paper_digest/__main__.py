"""Entry point for ``python -m paper_digest``."""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="paper_digest",
        description="Weekly arXiv/OpenAlex → Notion research digest",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # run
    run_p = sub.add_parser("run", help="Run the collection pipeline")
    run_p.add_argument(
        "--mode",
        choices=["weekly", "batch"],
        required=True,
        help="Pipeline mode",
    )
    run_p.add_argument(
        "--venue",
        default=None,
        help="Venue label for batch mode (e.g. 'ACL 2026')",
    )
    run_p.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )

    # init
    init_p = sub.add_parser("init", help="Create the Notion database on first run")
    init_p.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )

    args = parser.parse_args()

    from .pipeline import run_batch, run_init, run_weekly

    if args.command == "run":
        if args.mode == "weekly":
            sys.exit(run_weekly(args.config))
        else:
            sys.exit(run_batch(args.config, args.venue))
    elif args.command == "init":
        sys.exit(run_init(args.config))


if __name__ == "__main__":
    main()
