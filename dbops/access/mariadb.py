"""MariaDB: mysql.global_priv for accounts, information_schema for grants, server_audit for logins.

Reviewed on the primary and on the replica separately: accounts created with
replication turned off for the session exist on one side only.
Connects as root. MariaDB records when a password changed, but not the last
login: that needs the server_audit plugin logging CONNECT events, read here
from the audit file in the data directory.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pymysql

from ..config import targets
from . import Account, Instance

ACCOUNTS = "SELECT user, host, priv FROM mysql.global_priv"
GLOBAL = "SELECT grantee, privilege_type, is_grantable FROM information_schema.user_privileges"
SCHEMA = "SELECT grantee, table_schema, privilege_type FROM information_schema.schema_privileges"
ROLES = "SELECT user, host, role FROM mysql.roles_mapping"

ADMIN_PRIVS = {"SUPER", "CREATE USER", "SHUTDOWN", "FILE", "RELOAD", "SET USER", "CONNECTION ADMIN",
               "REPLICATION MASTER ADMIN", "BINLOG ADMIN", "READ_ONLY ADMIN", "FEDERATED ADMIN"}
DDL_PRIVS = {"CREATE", "DROP", "ALTER", "INDEX", "CREATE VIEW", "CREATE ROUTINE", "ALTER ROUTINE", "TRIGGER", "EVENT"}
PASSWORDLESS_OK = {"unix_socket", "gssapi", "pam"}      # the OS or Kerberos authenticates


def _grantee(user: str, host: str) -> str:
    return f"'{user}'@'{host}'"


def parse_audit_log(text: str) -> tuple[dict[str, datetime], dict[str, int], datetime | None]:
    """server_audit CSV -> ({user: last successful login}, {user: failed logins}, first event).

    20261117 16:02:11,mariadb-primary,shop_app,172.18.0.1,12,0,CONNECT,shop,,0
    20261117 16:02:15,mariadb-primary,shop_app,172.18.0.1,13,0,FAILED_CONNECT,,,1045
    """
    last: dict[str, datetime] = {}
    failed: dict[str, int] = {}
    first = None
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) < 10:
            continue
        try:
            ts = datetime.strptime(parts[0], "%Y%m%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        first = first or ts
        user, op, retcode = parts[2], parts[6], parts[-1].strip()
        if op == "CONNECT" and retcode == "0":
            last[user] = max(last.get(user, ts), ts)
        elif op == "FAILED_CONNECT" or (op == "CONNECT" and retcode != "0"):
            failed[user] = failed.get(user, 0) + 1
    return last, failed, first


def collect(env: dict) -> list[Instance]:
    return [_instance(t, env) for t in targets(env) if t.engine == "mariadb"]


def _instance(t, env: dict) -> Instance:
    inst = Instance("mariadb", t.name)
    try:
        conn = pymysql.connect(host=t.host, port=t.port, user="root", password=env["MARIADB_ROOT_PASSWORD"],
                               connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        inst.error = str(e)[:160]
        return inst

    with conn, conn.cursor() as cur:
        cur.execute(GLOBAL)
        global_privs: dict[str, dict[str, bool]] = {}
        for grantee, priv, grantable in cur.fetchall():
            global_privs.setdefault(grantee, {})[priv] = grantable == "YES"
        cur.execute(SCHEMA)
        schema_privs: dict[str, dict[str, set[str]]] = {}
        for grantee, schema, priv in cur.fetchall():
            schema_privs.setdefault(grantee, {}).setdefault(schema, set()).add(priv)
        cur.execute(ROLES)
        roles: dict[str, list[str]] = {}
        for user, host, role in cur.fetchall():
            roles.setdefault(_grantee(user, host), []).append(role)
        cur.execute("SHOW GLOBAL VARIABLES LIKE 'server_audit%'")
        audit = dict(cur.fetchall())
        cur.execute(ACCOUNTS)
        rows = cur.fetchall()

    logging_on = audit.get("server_audit_logging") == "ON" and "CONNECT" in audit.get("server_audit_events", "")
    last, failed = {}, {}
    if logging_on:
        path = audit.get("server_audit_file_path") or "server_audit.log"
        path = path if path.startswith("/") else f"/var/lib/mysql/{path}"
        log = subprocess.run(["docker", "exec", t.container, "sh", "-c", f"cat {path}.[0-9]* {path} 2>/dev/null"],
                             capture_output=True, text=True, timeout=60).stdout
        last, failed, inst.audit_since = parse_audit_log(log)
        inst.login_source = "server_audit"
    else:
        inst.audit_gaps.append(("logins are not recorded: server_audit is not logging CONNECT events",
                                "plugin_load_add = server_audit, server_audit_events = CONNECT, server_audit_logging = ON"))

    for user, host, priv_json in rows:
        priv = json.loads(priv_json or "{}")
        if priv.get("is_role"):
            continue
        g = _grantee(user, host)
        gp = global_privs.get(g, {})
        grantable = any(gp.values())
        sp = schema_privs.get(g, {})
        changed = priv.get("password_last_changed")
        plugin = priv.get("plugin", "")
        privileges = (["ALL PRIVILEGES ON *.*" + (" WITH GRANT OPTION" if grantable else "")] if len(gp) > 25
                      else sorted(p for p in gp if p != "USAGE"))
        privileges += [f"{', '.join(sorted(ps))} ON {schema}.*" for schema, ps in sorted(sp.items())]
        inst.accounts.append(Account(
            "mariadb", t.name, f"{user}@{host}",
            can_login=not priv.get("is_role"),
            locked=bool(priv.get("account_locked")),
            builtin=user == "mariadb.sys",
            admin=bool(set(gp) & ADMIN_PRIVS) or grantable,
            ddl=bool(set(gp) & DDL_PRIVS) or any(ps & DDL_PRIVS for ps in sp.values()),
            any_host=host == "%",
            no_password=not priv.get("authentication_string") and plugin not in PASSWORDLESS_OK,
            roles=sorted(roles.get(g, [])),
            privileges=privileges,
            password_changed=datetime.fromtimestamp(changed, timezone.utc) if changed else None,
            last_login=last.get(user),
            failed_logins=failed.get(user) if logging_on else None,
        ))
    return inst
