from datetime import datetime, timedelta

from dbops.config import Target
from dbops.engines.mariadb import replication_checks
from dbops.engines.mongo import replica_set_checks
from dbops.model import Status

REPLICA = Target("mariadb-replica", "mariadb", "c", "/d", "h", 1, "u", "p", role="replica")


def _status(io="Yes", sql="Yes", lag=0, **extra):
    return {"Slave_IO_Running": io, "Slave_SQL_Running": sql, "Seconds_Behind_Master": lag,
            "Last_IO_Error": "", "Last_SQL_Error": "", "Gtid_IO_Pos": "0-1-20", **extra}


def test_healthy_replica():
    checks = replication_checks(REPLICA, _status(lag=0))
    assert [(c.name, c.status) for c in checks] == [("replication", Status.OK), ("replication lag", Status.OK)]


def test_lag_is_graded():
    assert replication_checks(REPLICA, _status(lag=45))[1].status is Status.WARN
    assert replication_checks(REPLICA, _status(lag=600))[1].status is Status.CRIT


def test_stopped_sql_thread_reports_the_error():
    [check] = replication_checks(REPLICA, _status(sql="No", lag=None, Last_SQL_Error="Duplicate entry '7'"))
    assert check.status is Status.CRIT
    assert "Duplicate entry" in check.detail


def test_not_a_replica_is_critical():
    assert replication_checks(REPLICA, None)[0].status is Status.CRIT


def test_unknown_lag_is_critical():
    checks = replication_checks(REPLICA, _status(lag=None))
    assert checks[0].status is Status.CRIT and checks[0].name == "replication lag"


NOW = datetime(2026, 11, 17, 12, 0, 0)


def _member(name, state, behind_s=0, health=1.0):
    return {"name": name, "stateStr": state, "health": health, "optimeDate": NOW - timedelta(seconds=behind_s)}


def test_healthy_replica_set():
    checks = replica_set_checks({"members": [_member("m1", "PRIMARY"), _member("m2", "SECONDARY", 2),
                                             _member("m3", "SECONDARY", 0)]})
    assert all(c.status is Status.OK for c in checks)
    assert [c.value for c in checks] == ["m1", 2, 0]


def test_lagging_secondary_warns():
    checks = replica_set_checks({"members": [_member("m1", "PRIMARY"), _member("m2", "SECONDARY", 45)]})
    assert checks[-1].status is Status.WARN


def test_down_member_and_no_primary_are_critical():
    checks = replica_set_checks({"members": [
        _member("m1", "(not reachable/healthy)", health=0.0),
        _member("m2", "SECONDARY"), _member("m3", "SECONDARY")]})
    by_name = {c.name: c for c in checks}
    assert by_name["primary"].status is Status.CRIT
    assert by_name["member m1"].status is Status.CRIT
    assert by_name["lag m2"].status is Status.UNKNOWN


def test_no_status_at_all_is_critical():
    assert replica_set_checks(None)[0].status is Status.CRIT
