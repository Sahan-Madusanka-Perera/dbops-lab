"""Backups as recorded by scripts/backup/<engine>.sh, one manifest.json per backup.

A backup only counts once its restore test has PASSED, so the health check is
about the newest *verified* backup, not the newest file on disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import thresholds as t
from .config import REPO_ROOT
from .model import Check, Status, grade

BACKUP_ROOT = REPO_ROOT / "backups"

# Which instance each engine's backups are taken from (and reported against).
SOURCE_TARGET = {"oracle": "oracle", "mariadb": "mariadb-replica", "mongo": "mongo2", "postgres": "postgres"}


@dataclass(frozen=True)
class Backup:
    engine: str
    id: str
    finished_at: datetime
    size_bytes: int
    method: str
    position: str
    verify_status: str | None     # PASSED, FAILED, or None if never verified
    verify_detail: str

    @classmethod
    def from_manifest(cls, path: Path) -> Backup:
        m = json.loads(path.read_text())
        verify = m.get("verify") or {}
        return cls(
            engine=m["engine"], id=m["id"],
            finished_at=datetime.fromisoformat(m["finished_at"].replace("Z", "+00:00")),
            size_bytes=int(m.get("size_bytes", 0)), method=m.get("method", ""),
            position=m.get("position", ""), verify_status=verify.get("status"),
            verify_detail=verify.get("detail", ""),
        )


def list_backups(root: Path = BACKUP_ROOT) -> dict[str, list[Backup]]:
    """Every backup per engine, newest first. Unreadable manifests are skipped."""
    found: dict[str, list[Backup]] = {e: [] for e in SOURCE_TARGET}
    for path in root.glob("*/2*/manifest.json"):
        try:
            b = Backup.from_manifest(path)
        except (ValueError, KeyError, OSError):
            continue
        found.setdefault(b.engine, []).append(b)
    for backups in found.values():
        backups.sort(key=lambda b: b.id, reverse=True)
    return found


def backup_checks(engines: list[str], now: datetime | None = None, root: Path = BACKUP_ROOT) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    found = list_backups(root)
    checks = []
    for engine in engines:
        target = SOURCE_TARGET.get(engine)
        if target is None:
            continue
        backups = found.get(engine, [])
        newest = backups[0] if backups else None
        if newest is None:
            checks.append(Check(target, "last verified backup", Status.WARN, detail="no backup yet: make backup"))
            continue
        if newest.verify_status == "FAILED":
            checks.append(Check(target, "last verified backup", Status.CRIT, newest.id, "",
                                f"newest backup FAILED its restore test: {newest.verify_detail}"))
            continue

        verified = next((b for b in backups if b.verify_status == "PASSED"), None)
        if verified is None:
            checks.append(Check(target, "last verified backup", Status.CRIT, detail="no backup has passed a restore test"))
            continue
        age_h = round((now - verified.finished_at).total_seconds() / 3600, 1)
        note = "" if verified is newest else f"; newer {newest.id} not verified yet"
        checks.append(Check(target, "last verified backup", grade(age_h, *t.BACKUP_AGE_H), age_h, "h",
                            f"{verified.id}, {_size(verified.size_bytes)}, {verified.position}{note}"))
    return checks


def _size(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


def as_table(found: dict[str, list[Backup]]) -> str:
    lines = [f"{'engine':<9} {'backup':<17} {'size':>7}  {'verify':<7} position"]
    for engine, backups in found.items():
        for b in backups:
            lines.append(f"{engine:<9} {b.id:<17} {_size(b.size_bytes):>7}  {b.verify_status or '-':<7} {b.position}")
    return "\n".join(lines) if len(lines) > 1 else "no backups yet: make backup"
