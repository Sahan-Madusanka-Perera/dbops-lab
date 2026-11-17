"""Text for people, JSON for machines."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .model import Check, Status, summarize, worst

_COLOURS = {Status.OK: "32", Status.WARN: "33", Status.CRIT: "31", Status.UNKNOWN: "35"}


def as_json(checks: list[Check], at: datetime | None = None) -> str:
    at = at or datetime.now(timezone.utc)
    return json.dumps({
        "timestamp": at.isoformat(timespec="seconds"),
        "status": worst(c.status for c in checks).name,
        "summary": summarize(checks),
        "checks": [c.to_dict() for c in checks],
    }, indent=2, default=str)


def as_text(checks: list[Check], colour: bool = False, at: datetime | None = None) -> str:
    at = at or datetime.now().astimezone()

    def tag(status: Status) -> str:
        label = f"{status.name:<7}"
        return f"\033[{_COLOURS[status]}m{label}\033[0m" if colour else label

    lines = [f"DBOps health  {at:%Y-%m-%d %H:%M:%S}"]
    current = None
    for c in checks:
        if c.target != current:
            current = c.target
            lines += ["", c.target]
        value = "" if c.value is None else f"{c.value}{c.unit}"
        lines.append(f"  {tag(c.status)} {c.name:<26} {value:<14} {c.detail}".rstrip())

    counts = summarize(checks)
    overall = worst(c.status for c in checks)
    lines += ["", f"{tag(overall)} " + ", ".join(f"{n} {s}" for s, n in counts.items())]
    return "\n".join(lines)
