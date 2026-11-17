import json
from datetime import datetime, timedelta, timezone

from dbops.backups import as_table, backup_checks, list_backups
from dbops.model import Status

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _write(root, engine, hours_ago, status="PASSED", backup_id=None):
    finished = NOW - timedelta(hours=hours_ago)
    backup_id = backup_id or finished.strftime("%Y%m%dT%H%M%SZ")
    d = root / engine / backup_id
    d.mkdir(parents=True)
    manifest = {"engine": engine, "id": backup_id, "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size_bytes": 5 * 1024 ** 2, "method": "test", "position": "pos"}
    if status:
        manifest["verify"] = {"status": status, "detail": f"restore {status.lower()}"}
    (d / "manifest.json").write_text(json.dumps(manifest))


def _check(root, engine="postgres"):
    [c] = backup_checks([engine], now=NOW, root=root)
    return c


def test_no_backup_warns(tmp_path):
    c = _check(tmp_path)
    assert c.status is Status.WARN and c.target == "postgres"


def test_fresh_verified_backup_is_ok(tmp_path):
    _write(tmp_path, "postgres", hours_ago=3)
    c = _check(tmp_path)
    assert (c.status, c.value, c.unit) == (Status.OK, 3.0, "h")


def test_age_is_graded(tmp_path):
    _write(tmp_path, "mariadb", hours_ago=30)
    assert _check(tmp_path, "mariadb").status is Status.WARN
    _write(tmp_path, "mongo", hours_ago=72)
    assert _check(tmp_path, "mongo").status is Status.CRIT


def test_failed_restore_test_is_critical_even_with_older_good_backup(tmp_path):
    _write(tmp_path, "oracle", hours_ago=24)
    _write(tmp_path, "oracle", hours_ago=1, status="FAILED")
    c = _check(tmp_path, "oracle")
    assert c.status is Status.CRIT and "FAILED" in c.detail


def test_unverified_newest_falls_back_to_last_verified(tmp_path):
    _write(tmp_path, "postgres", hours_ago=5)
    _write(tmp_path, "postgres", hours_ago=0.5, status=None)
    c = _check(tmp_path)
    assert c.status is Status.OK and c.value == 5.0 and "not verified yet" in c.detail


def test_never_verified_is_critical(tmp_path):
    _write(tmp_path, "postgres", hours_ago=1, status=None)
    assert _check(tmp_path).status is Status.CRIT


def test_unreadable_manifest_is_skipped(tmp_path):
    bad = tmp_path / "postgres" / "20261117T000000Z"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{not json")
    assert list_backups(tmp_path)["postgres"] == []


def test_table_lists_newest_first(tmp_path):
    _write(tmp_path, "postgres", hours_ago=10)
    _write(tmp_path, "postgres", hours_ago=1)
    ids = [row.split()[1] for row in as_table(list_backups(tmp_path)).splitlines()[1:]]
    assert ids == sorted(ids, reverse=True) and len(ids) == 2
