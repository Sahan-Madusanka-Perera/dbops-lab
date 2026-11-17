"""dbops: the lab's operations CLI.

    dbops health                       every engine, human-readable
    dbops health --engine oracle       one engine (repeatable)
    dbops health --json                one JSON document, for monitoring
    dbops health --watch 30            re-run every 30 seconds

Exit codes follow the Nagios plugin convention:
    0 OK   1 WARN   2 CRIT   3 UNKNOWN (including a usage error)
"""

from __future__ import annotations

import argparse
import sys
import time

from .config import ENGINES
from .health import collect
from .model import Status, worst
from .render import as_json, as_text


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # a bad command line is UNKNOWN, not CRIT (argparse would exit 2)
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(Status.UNKNOWN)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="dbops", description="Operations toolkit for the DBOps Lab.",
                     epilog="Exit codes: 0 OK, 1 WARN, 2 CRIT, 3 UNKNOWN.")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    health = sub.add_parser("health", help="check every instance and report one verdict")
    health.add_argument("--engine", action="append", choices=ENGINES,
                        help="limit to one engine; repeat for several (default: all)")
    output = health.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="print one JSON document")
    output.add_argument("--watch", type=int, metavar="SECONDS", help="re-run every SECONDS until Ctrl-C")
    health.add_argument("--no-colour", action="store_true", help="plain text even on a terminal")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.watch is not None and args.watch < 1:
        _parser().error("--watch needs at least 1 second")

    colour = sys.stdout.isatty() and not args.no_colour
    while True:
        checks = collect(args.engine)
        if args.json:
            print(as_json(checks))
        else:
            if args.watch:
                print("\033[2J\033[H", end="")   # clear the screen between runs
            print(as_text(checks, colour=colour))
        status = worst(c.status for c in checks)
        if not args.watch:
            return int(status)
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return int(status)


if __name__ == "__main__":
    sys.exit(main())
