"""dbops on a server (M6): targets from a TOML file, host metrics from /proc. No databases needed."""

from pathlib import Path

from dbops.backups import backup_checks
from dbops.config import REPO_ROOT, backup_root, targets
from dbops.host import parse_meminfo, parse_proc_stat
from dbops.model import Status

TARGETS = """
backup_dir = "/var/backups/dbops"

[[target]]
name = "db-vm1"
engine = "mariadb"
host = "127.0.0.1"
port = 3306
data_dir = "/var/lib/mysql"
backups = true
"""


def test_targets_file_replaces_the_lab(tmp_path):
    f = tmp_path / "targets.toml"
    f.write_text(TARGETS)
    env = {"DBOPS_TARGETS": str(f), "MONITOR_PASSWORD": "pw"}
    [t] = targets(env)
    assert (t.name, t.engine, t.port, t.container, t.user, t.password, t.backups) == (
        "db-vm1", "mariadb", 3306, "", "dbops_monitor", "pw", True)
    assert backup_root(env) == Path("/var/backups/dbops")
    assert backup_root({**env, "DBOPS_BACKUP_DIR": "/srv/b"}) == Path("/srv/b")   # the environment wins


def test_without_a_targets_file_the_lab_is_unchanged():
    lab = targets({"MONITOR_PASSWORD": "pw"})
    assert len(lab) == 7 and all(t.container for t in lab)
    assert {t.name for t in lab if t.backups} == {"oracle", "mariadb-replica", "mongo2", "postgres"}
    assert backup_root({}) == REPO_ROOT / "backups"


def test_backup_checks_report_against_the_named_source(tmp_path):
    [c] = backup_checks(["mariadb"], root=tmp_path, sources={"mariadb": "db-vm1"})
    assert (c.target, c.status) == ("db-vm1", Status.WARN)          # no backup yet


def test_proc_stat_counts_iowait_as_idle():
    before = parse_proc_stat("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 ...")
    after = parse_proc_stat("cpu  130 0 60 850 60 0 0 0 0 0\ncpu0 ...")
    busy, total = after[0] - before[0], after[1] - before[1]
    assert (busy, total) == (40, 100)                               # 40% busy


def test_meminfo_used_is_total_minus_available():
    text = "MemTotal:        8000000 kB\nMemFree:          500000 kB\nMemAvailable:    6000000 kB\n"
    pct, detail = parse_meminfo(text)
    assert pct == 25.0
    assert detail == "1.9 GB of 7.6 GB"
