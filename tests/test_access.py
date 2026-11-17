"""dbops access: the review rules, the policy file, and the three login-record parsers. No databases needed."""

from datetime import date, datetime, timedelta, timezone

from dbops.access import (Account, AcceptedRisk, Approved, Instance, Policy, evaluate, load_policy, verdict)
from dbops.access.mariadb import parse_audit_log
from dbops.access.mongo import parse_mongod_log
from dbops.access.postgres import parse_jsonlog
from dbops.model import Status

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
POLICY = Policy(accounts=[
    Approved("shop_app", "app", "the shop application", "Shop team", ["mariadb", "postgres"]),
    Approved("root", "admin", "DBA", "DBA team", ["mariadb"]),
    Approved("shop", "owner", "schema owner", "Shop team", ["mariadb"], instances=["mariadb-primary"]),
    Approved("dbops_monitor", "service", "monitoring", "DBA team", ["postgres"], max_password_age_days=365),
])


def review(*accounts, policy=POLICY, **instance):
    engine, name = accounts[0].engine, accounts[0].instance
    inst = Instance(engine, name, accounts=list(accounts), **instance)
    return {(f.rule, f.account): f for f in evaluate([inst], policy, NOW)}


def pg(name, **kw):
    return Account("postgres", "postgres", name, **kw)


def maria(name, instance="mariadb-primary", **kw):
    return Account("mariadb", instance, name, **kw)


# ---------------------------------------------------------------- who may exist


def test_unapproved_account_is_warn_and_crit_when_it_is_an_admin():
    found = review(pg("temp_contractor"), pg("old_dba", admin=True))
    assert found[("unapproved-account", "temp_contractor")].status == Status.WARN
    assert found[("unapproved-account", "old_dba")].status == Status.CRIT


def test_builtin_locked_and_nologin_accounts_are_not_findings():
    found = review(pg("SYS", builtin=True, admin=True), pg("ghost", locked=True, admin=True),
                   pg("group_role", can_login=False))
    assert [k for k in found if k[0] != "missing-account"] == []


def test_missing_approved_account_respects_instances():
    on_replica = review(maria("root@%", instance="mariadb-replica", admin=True))
    assert ("missing-account", "shop_app") in on_replica
    assert ("missing-account", "shop") not in on_replica          # approved for the primary only


def test_mariadb_policy_name_matches_every_host_of_that_user():
    found = review(maria("root@localhost", admin=True), maria("root@%", admin=True),
                   maria("shop_app@%"), maria("shop@%", ddl=True))
    assert not any(rule == "unapproved-account" for rule, _ in found)


# ---------------------------------------------------------------- what they may do


def test_app_account_with_ddl_or_admin_is_excess_privilege():
    assert review(pg("shop_app", ddl=True))[("excess-privilege", "shop_app")].status == Status.WARN
    assert review(pg("shop_app", admin=True))[("excess-privilege", "shop_app")].status == Status.CRIT
    assert ("excess-privilege", "shop") not in review(maria("shop@%", ddl=True))   # an owner may change the schema


def test_admin_from_any_host():
    assert ("admin-any-host", "root@%") in review(maria("root@%", admin=True, any_host=True))
    assert ("admin-any-host", "root@localhost") not in review(maria("root@localhost", admin=True))


def test_no_password_and_default_password_are_crit():
    found = review(pg("shop_app", no_password=True), pg("dbops_monitor", default_password=True))
    assert found[("no-password", "shop_app")].status == Status.CRIT
    assert found[("default-password", "dbops_monitor")].status == Status.CRIT


# ---------------------------------------------------------------- age and use


def test_password_age_uses_the_accounts_own_limit():
    found = review(pg("shop_app", password_changed=NOW - timedelta(days=91)),
                   pg("dbops_monitor", password_changed=NOW - timedelta(days=200)))
    assert "91 days" in found[("password-age", "shop_app")].detail
    assert ("password-age", "dbops_monitor") not in found          # service account: 365 days


def test_unused_needs_a_long_enough_record():
    old = review(pg("shop_app", last_login=NOW - timedelta(days=120)), login_source="log")
    assert ("unused-account", "shop_app") in old
    # never seen, but logins have only been recorded for 3 days: not enough to say
    short = review(pg("shop_app"), login_source="log", audit_since=NOW - timedelta(days=3))
    assert ("unused-account", "shop_app") not in short
    # Oracle-style: recorded since the account was created, 100 days ago
    oracle = review(pg("shop_app", created=NOW - timedelta(days=100)), login_source="LAST_LOGIN")
    assert "100 days" in oracle[("unused-account", "shop_app")].detail
    # nothing records logins: the rule cannot run (login-audit-off says so instead)
    assert ("unused-account", "shop_app") not in review(pg("shop_app", created=NOW - timedelta(days=100)))


def test_failed_logins_threshold():
    assert ("failed-logins", "shop_app") in review(pg("shop_app", failed_logins=10))
    assert ("failed-logins", "shop_app") not in review(pg("shop_app", failed_logins=9))


# ---------------------------------------------------------------- the instance


def test_auth_disabled_is_crit_and_nothing_else_is_reported():
    inst = Instance("mongo", "rs0", auth_enabled=False, accounts=[Account("mongo", "rs0", "x")])
    findings = evaluate([inst], POLICY, NOW)
    assert [(f.rule, f.status) for f in findings] == [("auth-disabled", Status.CRIT)]


def test_audit_gap_and_unreachable():
    found = review(pg("shop_app"), audit_gaps=[("logins are not recorded", "log_connections = on")])
    assert found[("login-audit-off", "-")].status == Status.WARN
    gone = evaluate([Instance("oracle", "oracle", error="ORA-12541")], POLICY, NOW)
    assert [(f.rule, f.status) for f in gone] == [("unreachable", Status.UNKNOWN)]


# ---------------------------------------------------------------- accepted risks


def test_exception_accepts_until_it_expires():
    risk = AcceptedRisk("mariadb", "root@%", "admin-any-host", "lab only", "DBA lead", date(2026, 12, 31))
    policy = Policy(accounts=POLICY.accounts, exceptions=[risk])
    root = maria("root@%", admin=True, any_host=True)
    accepted = review(root, policy=policy)[("admin-any-host", "root@%")]
    assert accepted.accepted.startswith("lab only") and verdict([accepted]) == Status.OK

    risk.expires = date(2026, 11, 1)
    expired = review(root, policy=policy)[("admin-any-host", "root@%")]
    assert not expired.accepted and "expired on 2026-11-01" in expired.detail
    assert verdict([expired]) == Status.WARN


def test_the_repo_policy_file_is_valid():
    policy = load_policy()
    assert {a.type for a in policy.accounts} <= {"admin", "owner", "app", "service"}
    assert all(a.owner and a.purpose for a in policy.accounts)
    assert all(e.reason and e.approved_by and e.expires for e in policy.exceptions)


# ---------------------------------------------------------------- login records


def test_mariadb_server_audit_log():
    log = """\
20261117 15:48:02,mariadb-primary,shop_app,172.18.0.1,12,0,CONNECT,shop,,0
20261117 15:49:10,mariadb-primary,shop_app,172.18.0.1,13,0,FAILED_CONNECT,,,1045
20261117 15:49:11,mariadb-primary,shop_app,172.18.0.1,14,0,FAILED_CONNECT,,,1045
20261117 15:50:00,mariadb-primary,shop_app,172.18.0.1,15,0,CONNECT,shop,,0
20261117 15:50:01,mariadb-primary,shop_app,172.18.0.1,15,0,DISCONNECT,shop,,0
not a log line
"""
    last, failed, first = parse_audit_log(log)
    assert last == {"shop_app": datetime(2026, 11, 17, 15, 50, tzinfo=timezone.utc)}
    assert failed == {"shop_app": 2}
    assert first == datetime(2026, 11, 17, 15, 48, 2, tzinfo=timezone.utc)


def test_postgres_jsonlog():
    log = "\n".join([
        '{"timestamp":"2026-11-17 15:48:30.101 UTC","user":"shop_app","message":"connection authorized: user=shop_app database=shop"}',
        '{"timestamp":"2026-11-17 15:48:31.000 UTC","message":"connection received: host=172.18.0.1 port=50000"}',
        '{"timestamp":"2026-11-17 15:49:00.000 UTC","user":"admin","message":"password authentication failed for user \\"admin\\""}',
    ])
    last, failed, first = parse_jsonlog(log)
    assert last == {"shop_app": datetime(2026, 11, 17, 15, 48, 30, tzinfo=timezone.utc)}
    assert failed == {"admin": 1}
    assert first.minute == 48


def test_mongod_log():
    log = "\n".join([
        '{"t":{"$date":"2026-11-17T15:48:40.000+00:00"},"id":5286306,"msg":"Successfully authenticated",'
        '"attr":{"user":"shop_app","db":"admin"}}',
        '{"t":{"$date":"2026-11-17T15:49:00.000+00:00"},"id":20249,"msg":"Authentication failed","attr":{"user":"x"}}',
        '{"t":{"$date":"2026-11-17T15:49:05.000+00:00"},"id":51800,"msg":"client metadata"}',
        "plain text from the entrypoint",
    ])
    last, failed, first = parse_mongod_log(log)
    assert last == {"shop_app": datetime(2026, 11, 17, 15, 48, 40, tzinfo=timezone.utc)}
    assert failed == {"x": 1}
    assert first.second == 40
