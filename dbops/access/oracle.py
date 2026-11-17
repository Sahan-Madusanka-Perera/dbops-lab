"""Oracle (FREEPDB1): DBA_USERS, roles granted directly and through other roles, system privileges.

Connects as SYSTEM: reading every grant and the unified audit trail is a DBA's job.
LAST_LOGIN is recorded by Oracle for every account since it was created, so the
"unused" rule works without any auditing set up. Failed logins need the
ORA_LOGON_FAILURES unified audit policy.
"""

from __future__ import annotations

from datetime import timezone

import oracledb

from . import Account, Instance

USERS = """
SELECT username, account_status, oracle_maintained, authentication_type,
       created, password_change_date, SYS_EXTRACT_UTC(last_login)   -- LAST_LOGIN is in the session's time zone
FROM dba_users
"""

# Every role a user holds, directly or through another role.
ROLES = """
WITH r (grantee, role, direct) AS (
  SELECT grantee, granted_role, 1 FROM dba_role_privs
  UNION ALL
  SELECT r.grantee, p.granted_role, 0 FROM r JOIN dba_role_privs p ON p.grantee = r.role
)
SELECT DISTINCT grantee, role, direct FROM r
"""

SYS_PRIVS = "SELECT grantee, privilege FROM dba_sys_privs"
SCHEMA_PRIVS = "SELECT grantee, privilege, schema FROM dba_schema_privs"
OWNERS = "SELECT owner, COUNT(*) FROM dba_objects WHERE object_type IN ('TABLE', 'VIEW', 'INDEX') GROUP BY owner"
DEFAULT_PW = "SELECT username FROM dba_users_with_defpwd"
AUDIT_POLICIES = "SELECT policy_name FROM audit_unified_enabled_policies"
FAILED = """
SELECT dbusername, COUNT(*) FROM unified_audit_trail
WHERE action_name = 'LOGON' AND return_code <> 0 AND event_timestamp > SYSTIMESTAMP - INTERVAL '30' DAY
GROUP BY dbusername
"""

ADMIN_ROLES = {"DBA", "PDB_DBA", "IMP_FULL_DATABASE", "DATAPUMP_IMP_FULL_DATABASE", "EM_EXPRESS_ALL"}
ADMIN_PRIVS = {"CREATE USER", "ALTER USER", "DROP USER", "ALTER SYSTEM", "ALTER DATABASE", "GRANT ANY ROLE",
               "GRANT ANY PRIVILEGE", "GRANT ANY OBJECT PRIVILEGE", "AUDIT SYSTEM", "SYSDBA", "SYSOPER"}
DDL_PRIVS = {"CREATE TABLE", "CREATE VIEW", "CREATE PROCEDURE", "CREATE ANY TABLE", "DROP ANY TABLE",
             "ALTER ANY TABLE", "CREATE ANY INDEX", "DROP ANY INDEX", "CREATE ANY VIEW", "DROP ANY VIEW"}


def _utc(value):
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def collect(env: dict) -> list[Instance]:
    inst = Instance("oracle", "oracle", login_source="DBA_USERS.LAST_LOGIN")
    host = env.get("DBOPS_HOST", "127.0.0.1")
    try:
        conn = oracledb.connect(user="system", password=env["ORACLE_PASSWORD"],
                                dsn=f"{host}:{env.get('ORACLE_PORT', 1521)}/FREEPDB1")
    except Exception as e:  # noqa: BLE001 - reported as UNKNOWN
        inst.error = str(e).splitlines()[0][:160]
        return [inst]

    with conn:
        cur = conn.cursor()
        roles: dict[str, set[str]] = {}
        direct_roles: dict[str, list[str]] = {}
        for grantee, role, direct in cur.execute(ROLES):
            roles.setdefault(grantee, set()).add(role)
            if direct:
                direct_roles.setdefault(grantee, []).append(role)
        sys_privs: dict[str, set[str]] = {}
        for grantee, priv in cur.execute(SYS_PRIVS):
            sys_privs.setdefault(grantee, set()).add(priv)
        schema_privs: dict[str, list[str]] = {}
        for grantee, priv, schema in cur.execute(SCHEMA_PRIVS):
            schema_privs.setdefault(grantee, []).append(f"{priv} ON SCHEMA {schema}")
        owners = dict(cur.execute(OWNERS).fetchall())
        default_pw = {u for (u,) in cur.execute(DEFAULT_PW)}
        policies = {p for (p,) in cur.execute(AUDIT_POLICIES)}
        failed = dict(cur.execute(FAILED).fetchall()) if "ORA_LOGON_FAILURES" in policies else {}

        for name, status, maintained, auth, created, pw_changed, last_login in cur.execute(USERS).fetchall():
            held = roles.get(name, set())
            # privileges held directly and through every role
            privs = set(sys_privs.get(name, set())).union(*(sys_privs.get(r, set()) for r in held))
            sp = schema_privs.get(name, [])
            inst.accounts.append(Account(
                "oracle", "oracle", name,
                can_login=auth != "NONE" and "CREATE SESSION" in privs,
                locked="LOCKED" in status or "EXPIRED" in status,
                builtin=maintained == "Y",
                admin=bool(held & ADMIN_ROLES or privs & ADMIN_PRIVS),
                ddl=bool(owners.get(name) or privs & DDL_PRIVS
                         or any(p.split(" ON ")[0].split()[0] in ("CREATE", "ALTER", "DROP") for p in sp)),
                default_password=name in default_pw,
                roles=sorted(direct_roles.get(name, [])),
                privileges=sorted(sys_privs.get(name, set())) + sp
                           + ([f"owns {owners[name]} objects"] if owners.get(name) else []),
                created=_utc(created), password_changed=_utc(pw_changed), last_login=_utc(last_login),
                failed_logins=failed.get(name) if failed else None,
            ))
    if "ORA_LOGON_FAILURES" not in policies:
        inst.audit_gaps.append(("failed logins are not audited (no ORA_LOGON_FAILURES policy)",
                                "AUDIT POLICY ORA_LOGON_FAILURES;"))
    return [inst]
