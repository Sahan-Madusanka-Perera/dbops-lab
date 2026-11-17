"""MongoDB: usersInfo for accounts and their effective privileges, the mongod log for logins.

Users live in admin.system.users and replicate, so they are read once, from the
replica set. Whether access control is on is a per-member setting, so every
member is asked. MongoDB Community has no audit log; mongod logs each
successful and failed authentication, read here with `docker logs`. That record
lasts as long as the container: ship the logs somewhere in production.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

from ..config import targets
from . import Account, Instance

ADMIN_ROLES = {"root", "userAdminAnyDatabase", "userAdmin", "dbOwner", "clusterAdmin", "clusterManager",
               "hostManager", "dbAdminAnyDatabase", "restore", "__system"}
ADMIN_ACTIONS = {"createUser", "grantRole", "changeOwnPassword", "dropUser", "shutdown", "replSetReconfig"}
DDL_ACTIONS = {"createCollection", "dropCollection", "createIndex", "dropIndex", "collMod", "dropDatabase",
               "renameCollectionSameDB", "convertToCapped"}
AUTH_OK, AUTH_FAILED = 5286306, 20249            # mongod log ids


def parse_mongod_log(text: str) -> tuple[dict[str, datetime], dict[str, int], datetime | None]:
    """mongod JSON log lines -> ({user: last login}, {user: failed logins}, first event)."""
    last: dict[str, datetime] = {}
    failed: dict[str, int] = {}
    first = None
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
            ts = datetime.fromisoformat(entry["t"]["$date"]).astimezone(timezone.utc)
        except (ValueError, KeyError, TypeError):
            continue
        first = first or ts
        user = (entry.get("attr") or {}).get("user")
        if entry.get("id") == AUTH_OK and user:
            last[user] = max(last.get(user, ts), ts)
        elif entry.get("id") == AUTH_FAILED and user:
            failed[user] = failed.get(user, 0) + 1
    return last, failed, first


def _client(t, env: dict, auth: bool) -> MongoClient:
    kwargs = dict(directConnection=True, serverSelectionTimeoutMS=5000)
    if auth:
        kwargs |= dict(username="dbops_admin", password=env.get("MONGO_ADMIN_PASSWORD", ""), authSource="admin")
    return MongoClient(t.host, t.port, **kwargs)


def collect(env: dict) -> list[Instance]:
    members = [t for t in targets(env) if t.engine == "mongo"]
    inst = Instance("mongo", "rs0")
    open_members, users, primary = [], None, None
    for t in members:
        try:
            with _client(t, env, auth=False) as c:
                if c.admin.command("hello").get("isWritablePrimary"):    # hello needs no login
                    primary = t
                opts = c.admin.command("getCmdLineOpts")["parsed"]
        except PyMongoError as e:
            if "requires authentication" in str(e) or isinstance(e, OperationFailure):
                opts = {"security": {"authorization": "enabled"}}       # it refused us: access control is on
            else:
                inst.error = f"{t.name}: {str(e)[:120]}"
                return [inst]
        security = opts.get("security", {})
        if security.get("authorization") != "enabled" and not security.get("keyFile"):
            open_members.append(t.name)

    if open_members:
        inst.auth_enabled = False
        inst.auth_detail = (f"access control is off on {', '.join(open_members)}: anyone who can reach port 27017 "
                            f"can read, change or drop every database")
        return [inst]

    try:
        with _client(primary or members[0], env, auth=True) as c:
            listed = c.admin.command({"usersInfo": {"forAllDBs": True}})["users"]
            # privileges and restrictions are only shown for one named user at a time
            users = [c.admin.command({"usersInfo": {"user": u["user"], "db": u["db"]}, "showPrivileges": True,
                                      "showAuthenticationRestrictions": True})["users"][0] for u in listed]
    except PyMongoError as e:
        inst.error = f"usersInfo as dbops_admin: {str(e)[:120]}"
        return [inst]

    text = "\n".join(subprocess.run(["docker", "logs", t.container], capture_output=True, text=True,
                                    timeout=120).stdout for t in members)
    last, failed, inst.audit_since = parse_mongod_log(text)
    inst.login_source = "mongod log (docker logs)"

    for u in users:
        roles = {r["role"] for r in u.get("inheritedRoles", u.get("roles", []))}
        actions = {a for p in u.get("inheritedPrivileges", []) for a in p.get("actions", [])}
        inst.accounts.append(Account(
            "mongo", "rs0", u["user"],
            admin=bool(roles & ADMIN_ROLES or actions & ADMIN_ACTIONS - {"changeOwnPassword"}),
            ddl=bool(actions & DDL_ACTIONS),
            any_host=not u.get("authenticationRestrictions"),
            roles=sorted(f"{r['role']}@{r['db']}" for r in u.get("roles", [])),
            privileges=sorted(actions)[:12] + (["..."] if len(actions) > 12 else []),
            last_login=last.get(u["user"]),
            failed_logins=failed.get(u["user"], 0),
        ))
    return [inst]
