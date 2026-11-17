"""Access review: every account, role and privilege, checked against an approved list.

Each engine module returns Instances holding Accounts in one shape. The rules
here compare them with policy/access-policy.toml, the approved accounts with an
owner and a purpose, and turn every gap into a Finding. An exception in the
policy (who accepted the risk, why, until when) turns a finding into ACCEPTED.

What a DBA reviews, the same way in all four engines:

- who can log in at all, and whether authentication is even on;
- accounts nobody approved, and admin rights on accounts that should not have them;
- an application account that can change the schema;
- admin logins allowed from any host;
- passwords not changed for too long, accounts not used for too long;
- whether logins (and failed logins) are recorded, so the last two can be answered.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from ..config import REPO_ROOT
from ..model import Status

POLICY_FILE = REPO_ROOT / "policy" / "access-policy.toml"


@dataclass
class Account:
    engine: str
    instance: str
    name: str                                   # user; user@host in MariaDB
    can_login: bool = True
    locked: bool = False
    builtin: bool = False                       # shipped with the engine (SYS, mariadb.sys)
    admin: bool = False                         # superuser, or can manage users or the server
    ddl: bool = False                           # can create, alter or drop objects
    any_host: bool = False                      # may log in from any address
    no_password: bool = False
    default_password: bool = False
    roles: list[str] = field(default_factory=list)
    privileges: list[str] = field(default_factory=list)
    created: datetime | None = None
    password_changed: datetime | None = None
    last_login: datetime | None = None
    failed_logins: int | None = None            # within the instance's audit window

    @property
    def user(self) -> str:
        return self.name.split("@")[0]

    @property
    def active(self) -> bool:
        return self.can_login and not self.locked


@dataclass
class Instance:
    engine: str
    name: str
    auth_enabled: bool = True
    auth_detail: str = ""
    login_source: str = ""                      # where last login comes from; "" = nowhere
    audit_since: datetime | None = None         # how far back that source goes; None = since each account was created
    audit_gaps: list[tuple[str, str]] = field(default_factory=list)   # (what is not recorded, how to fix)
    accounts: list[Account] = field(default_factory=list)
    error: str = ""


@dataclass
class Finding:
    status: Status
    rule: str
    engine: str
    instance: str
    account: str
    detail: str
    fix: str = ""
    accepted: str = ""                          # exception reason, when the risk was accepted

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = "ACCEPTED" if self.accepted else self.status.name
        return d


# ---------------------------------------------------------------- policy


@dataclass
class Approved:
    name: str
    type: str                                   # admin | owner | app | service
    purpose: str
    owner: str
    engines: list[str]
    instances: list[str] = field(default_factory=list)   # empty = every instance of those engines
    max_password_age_days: int | None = None

    def matches(self, account: Account) -> bool:
        if account.engine not in self.engines:
            return False
        if self.instances and account.instance not in self.instances:
            return False
        return self.name == account.name if "@" in self.name else self.name.lower() == account.user.lower()


@dataclass
class AcceptedRisk:
    engine: str
    account: str
    rule: str
    reason: str
    approved_by: str
    expires: date

    def covers(self, f: Finding) -> bool:
        return (self.engine, self.account, self.rule) == (f.engine, f.account, f.rule)


@dataclass
class Policy:
    max_password_age_days: int = 90
    unused_after_days: int = 90
    failed_logins_warn: int = 10
    accounts: list[Approved] = field(default_factory=list)
    exceptions: list[AcceptedRisk] = field(default_factory=list)

    def approved(self, account: Account) -> Approved | None:
        return next((a for a in self.accounts if a.matches(account)), None)


def load_policy(path: Path = POLICY_FILE) -> Policy:
    raw = tomllib.loads(path.read_text())
    return Policy(
        max_password_age_days=raw.get("max_password_age_days", 90),
        unused_after_days=raw.get("unused_after_days", 90),
        failed_logins_warn=raw.get("failed_logins_warn", 10),
        accounts=[Approved(**a) for a in raw.get("account", [])],
        exceptions=[AcceptedRisk(**e) for e in raw.get("exception", [])],
    )


# ---------------------------------------------------------------- rules


def _days(since: datetime, now: datetime) -> int:
    return (now - since).days


def evaluate(instances: list[Instance], policy: Policy, now: datetime | None = None) -> list[Finding]:
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []
    for inst in instances:
        findings += _instance_findings(inst, policy, now)
        if inst.error or not inst.auth_enabled:
            continue                                # no account means anything if anyone can connect
        for acct in inst.accounts:
            findings += _account_findings(acct, inst, policy, now)
    return _apply_exceptions(findings, policy, now.date())


def _instance_findings(inst: Instance, policy: Policy, now: datetime) -> list[Finding]:
    out = []

    def add(status, rule, detail, fix="", account="-"):
        out.append(Finding(status, rule, inst.engine, inst.name, account, detail, fix))

    if inst.error:
        add(Status.UNKNOWN, "unreachable", f"could not review: {inst.error}")
        return out
    if not inst.auth_enabled:
        add(Status.CRIT, "auth-disabled", inst.auth_detail or "authentication is off: anyone who can connect is an administrator",
            "turn on access control and create the approved accounts")
        return out                                  # with auth off, nothing below means anything
    for gap, fix in inst.audit_gaps:
        add(Status.WARN, "login-audit-off", gap, fix)
    names = {a.name for a in inst.accounts}
    for approved in policy.accounts:
        if inst.engine in approved.engines and (not approved.instances or inst.name in approved.instances):
            if not any(approved.matches(Account(inst.engine, inst.name, n)) for n in names):
                add(Status.WARN, "missing-account",
                    f"approved {approved.type} account ({approved.purpose}) does not exist: "
                    f"whatever does its job is using another account", "create it", account=approved.name)
    return out


def _account_findings(acct: Account, inst: Instance, policy: Policy, now: datetime) -> list[Finding]:
    out = []

    def add(status, rule, detail, fix=""):
        out.append(Finding(status, rule, acct.engine, acct.instance, acct.name, detail, fix))

    if not acct.active:
        return out
    approved = policy.approved(acct)
    if acct.no_password:
        add(Status.CRIT, "no-password", "can log in without a password", "set a password or lock the account")
    if acct.default_password:
        add(Status.CRIT, "default-password", "still has the vendor's default password", "change the password or lock the account")

    if approved is None and not acct.builtin:
        if acct.admin:
            add(Status.CRIT, "unapproved-account", "not on the approved list, and has admin rights",
                "lock it, or add it to the policy with an owner and a purpose")
        else:
            add(Status.WARN, "unapproved-account", "not on the approved list",
                "lock it, or add it to the policy with an owner and a purpose")
    elif approved is not None:
        if acct.admin and approved.type != "admin":
            add(Status.CRIT, "excess-privilege", f"approved as {approved.type}, but has admin rights",
                "revoke the admin roles and privileges")
        elif acct.ddl and approved.type in ("app", "service"):
            add(Status.WARN, "excess-privilege", f"approved as {approved.type}, but can create, alter or drop objects",
                "grant read and write on the data only")

    if acct.admin and acct.any_host:
        add(Status.WARN, "admin-any-host", "admin account that can log in from any address",
            "limit it to the hosts DBAs connect from")

    max_age = (approved.max_password_age_days if approved and approved.max_password_age_days
               else policy.max_password_age_days)
    if acct.password_changed and not acct.builtin and _days(acct.password_changed, now) > max_age:
        add(Status.WARN, "password-age", f"password last changed {_days(acct.password_changed, now)} days ago "
            f"(limit {max_age})", "rotate it")

    if not acct.builtin:
        if acct.last_login:
            if _days(acct.last_login, now) > policy.unused_after_days:
                add(Status.WARN, "unused-account", f"last login {_days(acct.last_login, now)} days ago",
                    "lock it if nobody needs it")
        elif inst.login_source:
            since = inst.audit_since or acct.created
            if since and _days(since, now) > policy.unused_after_days:
                add(Status.WARN, "unused-account", f"no login in the {_days(since, now)} days on record",
                    "lock it if nobody needs it")

    if acct.failed_logins and acct.failed_logins >= policy.failed_logins_warn:
        add(Status.WARN, "failed-logins", f"{acct.failed_logins} failed logins on record",
            "find out who is trying: a wrong password in a job, or someone guessing")
    return out


def _apply_exceptions(findings: list[Finding], policy: Policy, today: date) -> list[Finding]:
    for f in findings:
        for risk in policy.exceptions:
            if risk.covers(f):
                if risk.expires >= today:
                    f.accepted = f"{risk.reason} (accepted by {risk.approved_by} until {risk.expires})"
                else:
                    f.detail += f"; the accepted risk expired on {risk.expires}"
    return findings


def verdict(findings: list[Finding]) -> Status:
    from ..model import worst
    return worst(f.status for f in findings if not f.accepted)
