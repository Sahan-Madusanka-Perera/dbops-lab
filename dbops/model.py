"""The result of a health check, and how results roll up into one verdict."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from enum import IntEnum


class Status(IntEnum):
    """Nagios plugin convention, so the exit code drops straight into monitoring."""

    OK = 0
    WARN = 1
    CRIT = 2
    UNKNOWN = 3


# Which status wins when results are combined: a failure beats "could not tell".
_SEVERITY = {Status.OK: 0, Status.UNKNOWN: 1, Status.WARN: 2, Status.CRIT: 3}


def worst(statuses) -> Status:
    return max(statuses, key=_SEVERITY.__getitem__, default=Status.OK)


def grade(value: float, warn: float, crit: float) -> Status:
    """Higher is worse: CRIT at or above crit, WARN at or above warn."""
    if value >= crit:
        return Status.CRIT
    if value >= warn:
        return Status.WARN
    return Status.OK


@dataclass(frozen=True)
class Check:
    target: str          # the instance checked, e.g. "mariadb-replica"
    name: str            # e.g. "replication lag"
    status: Status
    value: float | str | None = None
    unit: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.name
        return d


def summarize(checks: list[Check]) -> dict[str, int]:
    counts = Counter(c.status.name for c in checks)
    return {s.name: counts.get(s.name, 0) for s in Status}
