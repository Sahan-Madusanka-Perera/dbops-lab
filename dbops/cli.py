"""dbops: the lab's operations CLI.

    dbops health                       every engine, human-readable
    dbops health --engine oracle       one engine (repeatable)
    dbops health --json                one JSON document, for monitoring
    dbops health --watch 30            re-run every 30 seconds
    dbops backups                      every backup on disk and its restore test result
    dbops workload --seconds 30        run the sample application queries against every engine
    dbops slow                         worst statements per engine, their plans, index suggestions
    dbops access                       every account and privilege, checked against policy/access-policy.toml
    dbops access --markdown            the same as an evidence report for auditors

Exit codes follow the Nagios plugin convention:
    0 OK   1 WARN   2 CRIT   3 UNKNOWN (including a usage error)
"""

from __future__ import annotations

import argparse
import sys
import time

from .backups import as_table, list_backups
from .config import ENGINES
from .health import collect
from .model import Status, worst
from .render import as_json, as_text
from . import workload
from .access import load_policy
from .access import report as access_report
from .slow import report as slow_report


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

    sub.add_parser("backups", help="list backups and their restore test results (make backup takes them)")

    wl = sub.add_parser("workload", help="run a fixed mix of application queries, report latency per query")
    wl.add_argument("--engine", action="append", choices=ENGINES, help="limit to one engine (repeatable)")
    wl.add_argument("--seconds", type=float, default=30, help="how long to run (default 30)")

    slow = sub.add_parser("slow", help="rank statements by total time, read their plans, suggest indexes")
    slow.add_argument("--engine", action="append", choices=ENGINES, help="limit to one engine (repeatable)")
    slow.add_argument("--top", type=int, default=3, help="statements per engine (default 3)")
    out = slow.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="print JSON")
    out.add_argument("--ddl", action="store_true", help="print only the suggested fixes, as a script to review")
    out.add_argument("--reset", action="store_true", help="clear the collected statistics, to measure from now")

    access = sub.add_parser("access", help="review accounts, roles and privileges against the approved list")
    access.add_argument("--engine", action="append", choices=ENGINES, help="limit to one engine (repeatable)")
    fmt = access.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="print JSON, including locked accounts")
    fmt.add_argument("--markdown", action="store_true", help="print an evidence report with a sign-off table")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "backups":
        print(as_table(list_backups()))
        return 0
    if args.command == "slow":
        if args.reset:
            print("\n".join(slow_report.reset(args.engine)))
            return 0
        results = slow_report.collect(args.engine, args.top)
        print(slow_report.as_json(results) if args.json else
              slow_report.as_ddl(results) if args.ddl else slow_report.as_text(results, args.top))
        return 1 if any(src.startswith("error") for _, _, src in results) else 0
    if args.command == "access":
        policy = load_policy()
        instances, findings = access_report.review(args.engine, policy)
        print(access_report.as_json(instances, findings) if args.json else
              access_report.as_markdown(instances, findings, policy) if args.markdown else
              access_report.as_text(instances, findings, policy))
        return int(access_report.verdict(findings))
    if args.command == "workload":
        results = workload.run(args.engine, args.seconds)
        print(workload.as_text(results, args.seconds))
        return 1 if any(r.error for r in results) else 0
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
