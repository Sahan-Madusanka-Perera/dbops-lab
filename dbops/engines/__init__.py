"""One module per engine. Each exposes run(targets) -> list[Check].

Rules every engine follows:
- a connection failure is one CRIT "reachable" check for that instance, not a crash;
- every other check runs on its own, so one failing query reports UNKNOWN and the
  rest still run.
"""

from __future__ import annotations

from collections.abc import Callable

from ..config import Target
from ..model import Check, Status

CONNECT_TIMEOUT_S = 5


def unreachable(target: Target, error: Exception) -> Check:
    return Check(target.name, "reachable", Status.CRIT, detail=_short(error))


def guarded(target: Target, name: str, fn: Callable[[], Check | list[Check]]) -> list[Check]:
    """Run one check; turn an unexpected error into UNKNOWN instead of losing the report."""
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001 - any driver error becomes a reported UNKNOWN
        return [Check(target.name, name, Status.UNKNOWN, detail=_short(e))]
    return result if isinstance(result, list) else [result]


def _short(error: Exception) -> str:
    text = str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__
    return text[:160]
