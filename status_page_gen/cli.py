"""Command line: status-page-gen check|build -c config.yml"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .build import build, record
from .checks import DOWN, run_all
from .config import ConfigError, load_config


def _table(config, results) -> str:
    names = {c.id: c.name for c in config.checks}
    rows = [("CHECK", "STATUS", "LATENCY", "DETAIL")]
    for r in results:
        latency = "-" if r.latency_ms is None else f"{r.latency_ms} ms"
        rows.append((names.get(r.check_id, r.check_id), r.status.upper(), latency, r.detail))
    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    return "\n".join(
        f"{a:<{widths[0]}}  {b:<{widths[1]}}  {c:>{widths[2]}}  {d}" for a, b, c, d in rows
    )


def cmd_check(args) -> int:
    config = load_config(args.config)
    results = run_all(config.checks, config.concurrency)
    record(config, results)
    if not args.quiet:
        print(_table(config, results))
    if args.build:
        for path in build(config):
            if not args.quiet:
                print("wrote", path)
    return 1 if any(r.status == DOWN for r in results) else 0


def cmd_build(args) -> int:
    config = load_config(args.config)
    for path in build(config):
        print("wrote", path)
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="status-page-gen", description="Static status page generator (run from cron)."
    )
    parser.add_argument("--version", action="version", version=f"status-page-gen {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="run all checks and append the results to the history")
    p.add_argument("-c", "--config", required=True, help="config file (.yml, .yaml or .json)")
    p.add_argument("-q", "--quiet", action="store_true", help="no output")
    p.add_argument("--build", action="store_true", help="also rebuild the page, even when a check is down")
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("build", help="render index.html, status.json and feed.xml from the history")
    p.add_argument("-c", "--config", required=True, help="config file (.yml, .yaml or .json)")
    p.set_defaults(func=cmd_build)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
