"""Where each instance lives and how to reach it.

In the lab: the seven containers, with ports and passwords from .env.
On a server (M6): DBOPS_TARGETS names a TOML file listing the instances to check,
for example /etc/dbops/targets.toml written by the dbops_jobs Ansible role:

    backup_dir = "/var/backups/dbops"      # where the backup jobs write manifest.json

    [[target]]
    name = "db-vm1"
    engine = "mariadb"
    host = "127.0.0.1"
    port = 3306
    data_dir = "/var/lib/mysql"
    backups = true                         # this instance's backups are checked by dbops health

A target with no container is on this machine: CPU, memory and storage are read
from /proc and df instead of docker stats.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MONITOR_USER = "dbops_monitor"


def load_env(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    """KEY=VALUE lines; real environment variables win over the file."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    values.update({k: v for k, v in os.environ.items() if k.startswith(_ENV_PREFIXES)})
    return values


_ENV_PREFIXES = ("DBOPS_", "MONITOR_", "ORACLE_", "MARIADB_", "MONGO", "PG_", "APP_")


@dataclass(frozen=True)
class Target:
    name: str            # how it is reported, e.g. "mariadb-replica"
    engine: str          # oracle | mariadb | mongo | postgres
    container: str       # Docker container, for CPU / memory / storage
    data_dir: str        # inside the container
    host: str
    port: int
    user: str
    password: str
    database: str = ""
    role: str = ""       # primary | replica | member, where it matters
    backups: bool = False  # backups of this engine are taken from this instance


ENGINES = ("oracle", "mariadb", "mongo", "postgres")


def _targets_file(env: dict[str, str]) -> dict | None:
    path = env.get("DBOPS_TARGETS")
    return tomllib.loads(Path(path).read_text()) if path else None


def targets_from_file(raw: dict, password: str) -> list[Target]:
    return [Target(name=t["name"], engine=t["engine"], container=t.get("container", ""),
                   data_dir=t.get("data_dir", ""), host=t.get("host", "127.0.0.1"), port=int(t["port"]),
                   user=t.get("user", MONITOR_USER), password=password, database=t.get("database", ""),
                   role=t.get("role", ""), backups=bool(t.get("backups", False)))
            for t in raw.get("target", [])]


def backup_root(env: dict[str, str] | None = None) -> Path:
    env = load_env() if env is None else env
    raw = _targets_file(env)
    return Path(env.get("DBOPS_BACKUP_DIR") or (raw or {}).get("backup_dir") or REPO_ROOT / "backups")


def targets(env: dict[str, str] | None = None) -> list[Target]:
    env = load_env() if env is None else env
    pw = env.get("MONITOR_PASSWORD", "")
    raw = _targets_file(env)
    if raw is not None:
        return targets_from_file(raw, pw)
    host = env.get("DBOPS_HOST", "127.0.0.1")
    port = lambda key, default: int(env.get(key, default))  # noqa: E731

    return [
        Target("oracle", "oracle", "dbops-oracle", "/opt/oracle/oradata",
               host, port("ORACLE_PORT", 1521), MONITOR_USER, pw, database="FREEPDB1", backups=True),
        Target("mariadb-primary", "mariadb", "dbops-mariadb-primary", "/var/lib/mysql",
               host, port("MARIADB_PRIMARY_PORT", 3316), MONITOR_USER, pw, role="primary"),
        Target("mariadb-replica", "mariadb", "dbops-mariadb-replica", "/var/lib/mysql",
               host, port("MARIADB_REPLICA_PORT", 3317), MONITOR_USER, pw, role="replica", backups=True),
        *(
            Target(f"mongo{i}", "mongo", f"dbops-mongo{i}", "/data/db",
                   host, port(f"MONGO{i}_PORT", 27016 + i), MONITOR_USER, pw, database="admin", role="member",
                   backups=i == 2)
            for i in (1, 2, 3)
        ),
        Target("postgres", "postgres", "dbops-postgres", "/var/lib/postgresql/data",
               host, port("PG_PORT", 5442), MONITOR_USER, pw, database="shop", backups=True),
    ]
