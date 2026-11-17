"""PostgreSQL: pg_authid for roles, pg_hba_file_rules for where they may connect from, the server log for logins.

Connects as postgres. PostgreSQL does not record when a password changed or
when a role last logged in. The second comes from log_connections, written
as JSON by the logging collector and read here from the data directory.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import psycopg

from . import Account, Instance

ROLES = """
SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolcanlogin, r.rolreplication, r.rolbypassrls,
       r.rolvaliduntil, r.rolpassword IS NULL,
       ARRAY(SELECT b.rolname FROM pg_auth_members m JOIN pg_roles b ON b.oid = m.roleid WHERE m.member = r.oid ORDER BY 1),
       (SELECT count(*) FROM pg_class c WHERE c.relowner = r.oid AND c.relnamespace = 'public'::regnamespace),
       has_schema_privilege(r.rolname, 'public', 'CREATE'),
       ARRAY(SELECT DISTINCT g.privilege_type FROM information_schema.role_table_grants g
             WHERE g.grantee = r.rolname AND g.table_schema = 'public' ORDER BY 1)
FROM pg_authid r
WHERE r.rolname !~ '^pg_'
"""

# A host line that matches every address, and whether it lets a role in without a password.
HBA = """
SELECT user_name, auth_method,
       address = 'all' OR (address IN ('0.0.0.0', '::') AND netmask IN ('0.0.0.0', '::')) AS anywhere,
       address IN ('127.0.0.1', '::1') AS loopback
FROM pg_hba_file_rules WHERE type LIKE 'host%' AND error IS NULL
"""

SETTINGS = "SELECT name, setting FROM pg_settings WHERE name IN ('log_connections', 'logging_collector', " \
           "'log_destination', 'log_directory', 'data_directory')"
ADMIN_ROLES = {"pg_write_server_files", "pg_execute_server_program", "pg_read_server_files", "pg_signal_backend"}
CONTAINER = "dbops-postgres"


def parse_jsonlog(text: str) -> tuple[dict[str, datetime], dict[str, int], datetime | None]:
    """jsonlog lines -> ({user: last login}, {user: failed logins}, first event)."""
    last: dict[str, datetime] = {}
    failed: dict[str, int] = {}
    first = None
    for line in text.splitlines():
        try:
            entry = json.loads(line)
            ts = datetime.strptime(entry["timestamp"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        first = first or ts
        message = entry.get("message", "")
        if message.startswith("connection authorized:") and entry.get("user"):
            last[entry["user"]] = max(last.get(entry["user"], ts), ts)
        elif message.startswith("password authentication failed for user"):
            user = message.split('"')[1] if '"' in message else entry.get("user", "")
            failed[user] = failed.get(user, 0) + 1
    return last, failed, first


def collect(env: dict) -> list[Instance]:
    inst = Instance("postgres", "postgres")
    try:
        conn = psycopg.connect(host=env.get("DBOPS_HOST", "127.0.0.1"), port=int(env.get("PG_PORT", 5442)),
                               dbname="shop", user="postgres", password=env["POSTGRES_PASSWORD"], connect_timeout=5)
    except Exception as e:  # noqa: BLE001
        inst.error = str(e).splitlines()[0][:160]
        return [inst]
    with conn, conn.cursor() as cur:
        rows = cur.execute(ROLES).fetchall()
        hba = cur.execute(HBA).fetchall()
        settings = dict(cur.execute(SETTINGS).fetchall())

    def hba_for(role: str) -> tuple[bool, bool]:
        """(may connect from anywhere, may connect from outside the container without a password)"""
        lines = [h for h in hba if "all" in h[0] or role in h[0]]
        return (any(h[2] for h in lines),
                any(h[1] == "trust" and not h[3] for h in lines))

    logging_on = (settings.get("log_connections") == "on" and settings.get("logging_collector") == "on"
                  and "jsonlog" in settings.get("log_destination", ""))
    last, failed = {}, {}
    if logging_on:
        log_dir = settings["log_directory"]
        log_dir = log_dir if log_dir.startswith("/") else f"{settings['data_directory']}/{log_dir}"
        text = subprocess.run(["docker", "exec", CONTAINER, "sh", "-c", f"cat {log_dir}/*.json 2>/dev/null"],
                              capture_output=True, text=True, timeout=60).stdout
        last, failed, inst.audit_since = parse_jsonlog(text)
        inst.login_source = "log_connections (jsonlog)"
    else:
        inst.audit_gaps.append(("logins are not recorded: log_connections is off, or not written as jsonlog",
                                "log_connections = on, logging_collector = on, log_destination = 'stderr,jsonlog'"))

    for (name, superuser, createrole, createdb, login, replication, bypassrls, valid_until, no_pw,
         member_of, owned, can_create, table_privs) in rows:
        anywhere, trust = hba_for(name)
        attrs = [a for a, on in (("SUPERUSER", superuser), ("CREATEROLE", createrole), ("CREATEDB", createdb),
                                 ("REPLICATION", replication), ("BYPASSRLS", bypassrls)) if on]
        inst.accounts.append(Account(
            "postgres", "postgres", name,
            can_login=login,
            locked=bool(valid_until and valid_until < datetime.now(timezone.utc)),
            admin=superuser or createrole or bypassrls or bool(set(member_of) & ADMIN_ROLES),
            ddl=superuser or owned > 0 or can_create,
            any_host=anywhere,
            # A role with no password cannot pass scram-sha-256; only a trust line lets it in without one.
            no_password=trust,
            roles=list(member_of),
            privileges=attrs + ([f"{', '.join(table_privs)} on public tables"] if table_privs else [])
                       + ([f"owns {owned} objects in public"] if owned else []),
            last_login=last.get(name),
            failed_logins=failed.get(name) if logging_on else None,
        ))
    return [inst]
