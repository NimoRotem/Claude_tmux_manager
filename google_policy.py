#!/usr/bin/env python3
"""Who may reach what through the Google Workspace MCP, and the record of it.

Every dashboard account drives Codex on one shared host, so "which Google
account is this call made as" and "may this group see this document" cannot be
left to the prompt: an agent that is asked nicely enough will route around a
sentence in its instructions. The rules live here, the MCP calls them on the way
in and out of Google, and every decision is appended to an audit log.

Kept free of the MCP's transport code so the dashboard and the tests can import
it without a Google credential in sight.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from email.utils import getaddresses
from pathlib import Path
from typing import Any, Iterable

AUDIT_FILE_ENV = "GOOGLE_MCP_AUDIT_FILE"
AUDIT_FILE_NAME = "google-mcp-audit.jsonl"
_AUDIT_MAX_DETAIL = 400


class PolicyDenied(RuntimeError):
    """A rule refused this call. The message is shown to the agent verbatim."""


@dataclass(frozen=True)
class Actor:
    """The dashboard account a Google MCP process is running for."""

    user_id: str = ""
    username: str = ""
    group: str = ""
    role: str = "user"
    google_email: str = ""
    known: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.username or self.user_id or "unknown"


# --- what counts as sensitive -----------------------------------------------
#
# Matched against a Drive file's NAME only, never its content: a name is what
# search returns, so a rule on the name is a rule the member cannot step around
# by reading instead of listing. English and Chinese both appear in this
# company's file names, so both are listed.
#
# Keep these tight. A pattern that fires on ordinary engineering work (say a
# bare "invoice", which the contractor-invoice app is built around) costs a
# blocked day and teaches people to route around the tool, which is the failure
# this file exists to prevent.

_CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "finance": (
        r"payroll", r"pay\s*slip", r"payslip", r"salar(?:y|ies)", r"wage\b",
        r"compensation", r"comp\s+plan", r"\bbonus", r"commission",
        r"bank\s+statement", r"bank\s+account", r"\biban\b", r"\bswift\b",
        r"wire\s+transfer", r"\bp\s*&\s*l\b", r"profit\s+and\s+loss",
        r"balance\s+sheet", r"\bebitda\b", r"cash\s*flow", r"tax\s+return",
        r"\bw-?2\b", r"\b1099\b", r"credit\s+card", r"cardholder",
        r"工资", r"薪资", r"薪酬", r"奖金", r"提成", r"对账单", r"银行账户",
        r"利润表", r"资产负债表", r"现金流", r"报税", r"纳税申报",
    ),
    "hr": (
        r"offer\s+letter", r"employment\s+(?:agreement|contract)",
        r"performance\s+review", r"personnel\s+file", r"disciplinary",
        r"termination\s+letter", r"severance", r"background\s+check",
        r"health\s+insurance", r"headcount", r"org\s*chart",
        r"劳动合同", r"录用通知", r"绩效考核", r"社保", r"医保", r"人事档案",
    ),
    "identity": (
        r"passport", r"driver'?s?\s+licen[cs]e", r"driving\s+licen[cs]e",
        r"\bssn\b", r"social\s+security", r"\bhkid\b", r"national\s+id\b",
        r"birth\s+certificate", r"identity\s+document", r"id\s+scan",
        r"护照", r"身份证", r"出生证",
    ),
}

_COMPILED: dict[str, tuple[re.Pattern[str], ...]] = {
    category: tuple(re.compile(pattern, re.I) for pattern in patterns)
    for category, patterns in _CATEGORY_PATTERNS.items()
}

_CATEGORY_LABEL = {
    "finance": "company financial and payroll",
    "hr": "HR and personnel",
    "identity": "personal identity",
}

# Group -> the categories it may NOT open. Mirrors PERMISSION_GROUPS in app.py
# and the same person's advisor scopes: engineers and dev are denied company
# money and people data there, so they are denied the documents here too.
_GROUP_DENIED: dict[str, frozenset[str]] = {
    "managers": frozenset(),
    "accounting-all": frozenset({"identity"}),
    "accounting-cn": frozenset({"identity"}),
    "engineers": frozenset({"finance", "hr", "identity"}),
    "dev": frozenset({"finance", "hr", "identity"}),
    "limited-dev": frozenset({"finance", "hr", "identity"}),
}
# An account with no group, or one carrying a group this build does not know,
# gets the strictest set rather than a free pass.
_DENIED_FALLBACK = frozenset({"finance", "hr", "identity"})

# Groups whose work genuinely reaches outside the company. Everyone else may
# mail colleagues only.
_MAY_MAIL_EXTERNAL = frozenset({"managers", "accounting-all", "accounting-cn"})

# Free mailbox providers. Mail to one of these is a person's own inbox, which is
# the cheapest way company material walks out, so it is refused for every
# account except the admin regardless of group.
_PERSONAL_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "msn.com", "yahoo.com", "yahoo.co.uk", "ymail.com", "aol.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "pm.me", "gmx.com", "gmx.de", "yandex.com", "yandex.ru", "mail.ru",
    "qq.com", "foxmail.com", "163.com", "126.com", "sina.com", "sohu.com",
    "aliyun.com", "tutanota.com", "zoho.com", "hushmail.com",
})


def company_domains() -> frozenset[str]:
    """Domains that count as inside the company."""
    raw = ",".join((
        os.environ.get("GOOGLE_WORKSPACE_DWD_DOMAINS", "")
        or "grabo.com,nemopowertools.com",
        os.environ.get("GOOGLE_WORKSPACE_COMPANY_DOMAINS", ""),
    ))
    return frozenset(
        item.strip().lower().lstrip("@") for item in raw.split(",") if item.strip()
    )


def denied_categories(actor: Actor) -> frozenset[str]:
    if actor.is_admin:
        return frozenset()
    return _GROUP_DENIED.get(actor.group, _DENIED_FALLBACK)


def classify(text: str) -> frozenset[str]:
    """Every sensitive category this file or subject name matches."""
    value = str(text or "")
    if not value:
        return frozenset()
    return frozenset(
        category
        for category, patterns in _COMPILED.items()
        if any(pattern.search(value) for pattern in patterns)
    )


def drive_denial(actor: Actor, file_name: str) -> str:
    """Empty when this account may open the file, else the reason to show."""
    hits = classify(file_name) & denied_categories(actor)
    if not hits:
        return ""
    kinds = ", ".join(sorted(_CATEGORY_LABEL.get(hit, hit) for hit in hits))
    group = actor.group or "no group"
    return (
        f"Denied: this file looks like {kinds} material, which the "
        f"{group} permission group may not open. Ask an admin if you need it "
        "for a specific task; do not try another account or another route."
    )


def query_denial(actor: Actor, query: str) -> str:
    """Empty when this account may run this Drive search, else the reason.

    Drive search is full text, so a file called "Gregory Griffin" comes back for
    "payroll" and no rule on the file NAME can catch it. Refusing the search
    itself is what stops the sweep — someone fishing for salaries has to say
    so in the query.
    """
    hits = classify(query) & denied_categories(actor)
    if not hits:
        return ""
    kinds = ", ".join(sorted(_CATEGORY_LABEL.get(hit, hit) for hit in hits))
    group = actor.group or "no group"
    return (
        f"Denied: this searches company Drive for {kinds} material, which the "
        f"{group} permission group may not read. Search for the work you are "
        "doing instead, and ask an admin for a figure you actually need."
    )


def _addresses(*values: str) -> tuple[list[str], bool]:
    """Every recipient address, and whether anything failed to parse.

    `getaddresses` hardened against malformed header lists and now answers an
    empty list for input it will not parse, so passing it a header it dislikes
    would otherwise read as "no recipients" and wave the message through. Each
    value is parsed on its own, with a plain split as the fallback, and an
    unparseable non-empty value is reported so the caller can refuse.
    """
    out: list[str] = []
    unparsed = False
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        found = [
            address.strip().lower()
            for _, address in getaddresses([text])
            if address and "@" in address
        ]
        if not found:
            found = [
                token.strip().strip("<>").lower()
                for token in re.split(r"[,;]", text)
                if "@" in token
            ]
        if not found:
            unparsed = True
        out.extend(found)
    return out, unparsed


def recipient_denial(actor: Actor, to: str, cc: str = "", bcc: str = "") -> str:
    """Empty when this account may mail these people, else the reason."""
    if actor.is_admin:
        return ""
    addresses, unparsed = _addresses(to, cc, bcc)
    if unparsed:
        return (
            "Denied: the recipient list could not be read, so it cannot be "
            "checked against the mail rules. Send to one plain address at a "
            "time, e.g. name@grabo.com."
        )
    if not addresses:
        return ""
    inside = company_domains()
    personal = sorted({
        address for address in addresses
        if address.rpartition("@")[2] in _PERSONAL_MAIL_DOMAINS
    })
    if personal:
        return (
            "Denied: "
            + ", ".join(personal)
            + " is a personal mailbox. Company material is not sent to personal "
            "accounts from this host. Use the recipient's company address, or "
            "publish the work as a dashboard project link."
        )
    if actor.group in _MAY_MAIL_EXTERNAL:
        return ""
    outside = sorted({
        address for address in addresses
        if address.rpartition("@")[2] not in inside
    })
    if outside:
        group = actor.group or "no group"
        return (
            "Denied: "
            + ", ".join(outside)
            + f" is outside the company and the {group} permission group sends "
            "internal mail only. Ask a manager to send it, or hand the draft "
            "back naming the recipient."
        )
    return ""


# --- who is calling ----------------------------------------------------------

def load_actor(
    credentials_dir: Path | str | None,
    users_file: Path | str | None = None,
) -> Actor:
    """Resolve the dashboard account from its per-user credentials directory.

    The directory is named for the account id, which is how the dashboard binds
    one MCP process to one person. An id we cannot resolve is treated as an
    unknown member, never as the admin.
    """
    if not credentials_dir:
        return Actor()
    directory = Path(credentials_dir)
    user_id = directory.name
    path = (
        Path(users_file)
        if users_file
        else directory.parent.parent / "users.json"
    )
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return Actor(user_id=user_id)
    users = value.get("users") if isinstance(value, dict) else value
    for item in users or []:
        if not isinstance(item, dict) or item.get("id") != user_id:
            continue
        return Actor(
            user_id=user_id,
            username=str(item.get("username") or ""),
            group=str(item.get("group") or ""),
            role=str(item.get("role") or "user"),
            google_email=str(item.get("google_email") or "").strip().lower(),
            known=True,
        )
    return Actor(user_id=user_id)


# --- the record --------------------------------------------------------------

def audit_file(credentials_dir: Path | str | None = None) -> Path | None:
    configured = os.environ.get(AUDIT_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    if not credentials_dir:
        return None
    return Path(credentials_dir).parent.parent / AUDIT_FILE_NAME


def audit(
    actor: Actor,
    tool: str,
    decision: str,
    detail: str = "",
    reason: str = "",
    credentials_dir: Path | str | None = None,
    accounts: Iterable[str] = (),
) -> None:
    """Append one line to the audit log. Never raises: a failed write must not
    take down a Google call, and a swallowed write is visible as a gap."""
    path = audit_file(credentials_dir)
    if path is None:
        return
    entry: dict[str, Any] = {
        "ts": round(time.time(), 3),
        "user_id": actor.user_id,
        "username": actor.username,
        "group": actor.group,
        "role": actor.role,
        "tool": tool,
        "decision": decision,
        "detail": str(detail or "")[:_AUDIT_MAX_DETAIL],
    }
    if reason:
        entry["reason"] = str(reason)[:_AUDIT_MAX_DETAIL]
    accounts = [str(item) for item in accounts]
    if accounts:
        entry["accounts"] = accounts
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        pass


def read_audit(path: Path | str, limit: int = 200) -> list[dict[str, Any]]:
    """The most recent entries, newest first. Used by the admin API."""
    try:
        lines = Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in reversed(lines):
        if len(out) >= max(1, int(limit)):
            break
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out
