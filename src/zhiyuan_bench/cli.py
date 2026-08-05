"""Command-line interface for explicit suite runs and live monitoring."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from zhiyuan_bench.events import monitor
from zhiyuan_bench.registry import BRIDGES, SUITES
from zhiyuan_bench.runner import create_run, parse_candidate, run_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhiyuan-bench")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list-suites", help="List explicit evaluation suites")
    commands.add_parser("list-bridges", help="List bridge capabilities")

    run = commands.add_parser("run", help="Create and execute a new run")
    run.add_argument("--suite", required=True)
    run.add_argument("--bridge", default="auto")
    run.add_argument("--candidate", action="append", required=True)
    run.add_argument("--workspace", type=Path, required=True)
    run.add_argument("--output-root", type=Path, default=Path(".zhiyuan-bench"))
    run.add_argument("--limit", type=int)
    run.add_argument("--no-health-checks", action="store_true", help=argparse.SUPPRESS)

    resume = commands.add_parser("resume", help="Resume incomplete phases")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--no-health-checks", action="store_true", help=argparse.SUPPRESS)

    watch = commands.add_parser("monitor", help="Read prompt-free JSONL progress")
    watch.add_argument("run_dir", type=Path)
    watch.add_argument("--follow", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list-suites":
            for suite in SUITES:
                capabilities = ",".join(sorted(suite.required_capabilities))
                print(f"{suite.id}\t{suite.description}\t{capabilities}")
            return 0
        if args.command == "list-bridges":
            for bridge in BRIDGES:
                print(
                    f"{bridge.id}\t{bridge.description}\t"
                    + ",".join(sorted(bridge.capabilities))
                )
            return 0
        if args.command == "monitor":
            monitor(args.run_dir, follow=args.follow)
            return 0
        if args.command == "run":
            if args.limit is not None and args.limit <= 0:
                raise ValueError("--limit must be positive")
            candidates = [parse_candidate(value) for value in args.candidate]
            run_dir = create_run(
                suite_id=args.suite,
                bridge_id=args.bridge,
                candidates=candidates,
                workspace=args.workspace,
                output_root=args.output_root,
                limit=args.limit,
            )
            print(f"Run directory: {run_dir}", flush=True)
            run_manifest(run_dir, health_checks=not args.no_health_checks)
            return 0
        run_manifest(args.run_dir, health_checks=not args.no_health_checks)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"zhiyuan-bench: {error}", file=sys.stderr)
        return 1

