import json
import os
from datetime import datetime, timezone

import pytest

from dbops.cli import main
from dbops.config import _ENV_PREFIXES, Target, load_env, targets
from dbops.host import host_checks, parse_df, parse_docker_stats
from dbops.model import Check, Status
from dbops.render import as_json, as_text

STATS = (
    '{"Name":"dbops-oracle","CPUPerc":"250.00%","MemPerc":"31.40%","MemUsage":"2.4GiB / 7.6GiB"}\n'
    '{"Name":"dbops-postgres","CPUPerc":"0.05%","MemPerc":"0.50%","MemUsage":"40MiB / 7.6GiB"}\n'
)


def test_docker_cpu_is_spread_over_all_cores():
    stats = parse_docker_stats(STATS, ncpu=10)
    assert stats["dbops-oracle"]["cpu"] == pytest.approx(25.0)
    assert stats["dbops-oracle"]["mem"] == pytest.approx(31.4)


def test_parse_df():
    out = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
           "/dev/vda1        473994372  15466180 434319828       4% /var/lib/mysql\n")
    pct, detail = parse_df(out)
    assert pct == 4.0
    assert detail == "14.7 GB of 452.0 GB"


def test_stopped_container_is_critical():
    target = Target("mongo2", "mongo", "dbops-mongo2", "/data/db", "h", 1, "", "")
    [check] = host_checks(target, parse_docker_stats(STATS))
    assert check.status is Status.CRIT and "not running" in check.detail


def test_env_file_and_environment(tmp_path, monkeypatch):
    # `make` exports .env into the environment; start from a clean one.
    for key in [k for k in os.environ if k.startswith(_ENV_PREFIXES)]:
        monkeypatch.delenv(key)
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nPG_PORT=6000\nMONITOR_PASSWORD=from-file\n")
    monkeypatch.setenv("MONITOR_PASSWORD", "from-env")
    env = load_env(env_file)
    assert env["PG_PORT"] == "6000" and env["MONITOR_PASSWORD"] == "from-env"
    pg = next(t for t in targets(env) if t.engine == "postgres")
    assert (pg.port, pg.password) == (6000, "from-env")


CHECKS = [
    Check("oracle", "reachable", Status.OK, detail="Oracle 23"),
    Check("oracle", "tablespace APP_DATA", Status.WARN, 88.0, "%", "18 MB of 20 MB"),
    Check("rs0", "primary", Status.CRIT, 0),
]
AT = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def test_json_document():
    doc = json.loads(as_json(CHECKS, at=AT))
    assert doc["status"] == "CRIT"
    assert doc["summary"] == {"OK": 1, "WARN": 1, "CRIT": 1, "UNKNOWN": 0}
    assert doc["checks"][1] == {"target": "oracle", "name": "tablespace APP_DATA", "status": "WARN",
                                "value": 88.0, "unit": "%", "detail": "18 MB of 20 MB"}


def test_text_groups_by_instance():
    text = as_text(CHECKS, at=AT)
    assert "\noracle\n" in text and "\nrs0\n" in text
    assert "88.0%" in text
    assert text.splitlines()[-1].startswith("CRIT")
    assert "\033[" not in text


def test_bad_arguments_exit_unknown(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["health", "--engine", "db2"])
    assert exc.value.code == Status.UNKNOWN
