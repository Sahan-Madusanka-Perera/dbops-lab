"""Where each instance lives and how to reach it, read from .env."""

from __future__ import annotations

import os
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


_ENV_PREFIXES = ("DBOPS_", "MONITOR_", "ORACLE_", "MARIADB_", "MONGO", "PG_")


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


ENGINES = ("oracle", "mariadb", "mongo", "postgres")


def targets(env: dict[str, str] | None = None) -> list[Target]:
    env = load_env() if env is None else env
    pw = env.get("MONITOR_PASSWORD", "")
    host = env.get("DBOPS_HOST", "127.0.0.1")
    port = lambda key, default: int(env.get(key, default))  # noqa: E731

    return [
        Target("oracle", "oracle", "dbops-oracle", "/opt/oracle/oradata",
               host, port("ORACLE_PORT", 1521), MONITOR_USER, pw, database="FREEPDB1"),
        Target("mariadb-primary", "mariadb", "dbops-mariadb-primary", "/var/lib/mysql",
               host, port("MARIADB_PRIMARY_PORT", 3316), MONITOR_USER, pw, role="primary"),
        Target("mariadb-replica", "mariadb", "dbops-mariadb-replica", "/var/lib/mysql",
               host, port("MARIADB_REPLICA_PORT", 3317), MONITOR_USER, pw, role="replica"),
        *(
            Target(f"mongo{i}", "mongo", f"dbops-mongo{i}", "/data/db",
                   host, port(f"MONGO{i}_PORT", 27016 + i), MONITOR_USER, pw, database="admin", role="member")
            for i in (1, 2, 3)
        ),
        Target("postgres", "postgres", "dbops-postgres", "/var/lib/postgresql/data",
               host, port("PG_PORT", 5442), MONITOR_USER, pw, database="shop"),
    ]
