"""Against the running lab. Skipped unless DBOPS_LIVE=1 (make test-live)."""

import os

import pytest

from dbops.health import collect
from dbops.model import Status

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("DBOPS_LIVE") != "1", reason="set DBOPS_LIVE=1 with the lab running"),
]


@pytest.fixture(scope="module")
def checks():
    return collect()


def test_every_instance_is_reachable(checks):
    reachable = {c.target: c.status for c in checks if c.name == "reachable"}
    assert reachable == {name: Status.OK for name in
                         ["oracle", "mariadb-primary", "mariadb-replica", "mongo1", "mongo2", "mongo3", "postgres"]}


def test_no_check_is_unknown(checks):
    assert [c for c in checks if c.status is Status.UNKNOWN] == []


def test_replication_is_running(checks):
    by_key = {(c.target, c.name): c for c in checks}
    assert by_key[("mariadb-replica", "replication")].status is Status.OK
    assert by_key[("mariadb-primary", "replicas connected")].value >= 1
    assert by_key[("rs0", "primary")].status is Status.OK


def test_app_tablespace_is_reported(checks):
    assert any(c.target == "oracle" and c.name == "tablespace APP_DATA" for c in checks)
