"""What each dashboard account may reach through the Google Workspace MCP.

These guard a real incident: the shared "company" grant resolved to one person's
Workspace account (nimo@nemopowertools.com) and `_account_paths` handed it to
every member ahead of their own identity, so all fourteen members could read
that Drive and mailbox over the top of every Workspace permission.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import google_policy as policy
import google_workspace_mcp as mcp

USERS = {
    "users": [
        {"id": "admin", "username": "Nimo", "role": "admin",
         "google_email": "nimrod.rotem@gmail.com"},
        {"id": "u_eng", "username": "bill", "role": "user", "group": "engineers",
         "google_email": "bill@nemopowertools.com"},
        {"id": "u_mgr", "username": "guy", "role": "user", "group": "managers",
         "google_email": "guy@nemopowertools.com"},
        {"id": "u_acct", "username": "amy", "role": "user",
         "group": "accounting-all", "google_email": "amy@grabo.com"},
        {"id": "u_new", "username": "newbie", "role": "user", "group": "",
         "google_email": "newbie@grabo.com"},
    ]
}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    """A dashboard data directory with one connections dir per account."""
    root = tmp_path / ".tmux-dashboard"
    (root / "connections").mkdir(parents=True)
    (root / "users.json").write_text(json.dumps(USERS))
    for name in ("_shared", "admin", "u_eng", "u_mgr", "u_acct", "u_new"):
        (root / "connections" / name).mkdir()
    for service in ("drive", "gmail", "calendar"):
        (root / "connections" / "_shared" / f"{service}.json").write_text("{}")
    monkeypatch.setenv(policy.AUDIT_FILE_ENV, str(root / "audit.jsonl"))
    monkeypatch.setenv("GOOGLE_WORKSPACE_DWD_DOMAINS",
                       "grabo.com,nemopowertools.com")
    return root


def _as(root: Path, user_id: str, monkeypatch, *, dwd: bool = True):
    monkeypatch.setenv("GOOGLE_MCP_CREDENTIALS_DIR",
                       str(root / "connections" / user_id))
    return patch.object(mcp, "_dwd_available", lambda service: dwd)


def actor(root: Path, user_id: str) -> policy.Actor:
    return policy.load_actor(root / "connections" / user_id)


# --- which Google account a call is made as ---------------------------------

def test_member_never_receives_the_shared_company_grant(workspace, monkeypatch):
    with _as(workspace, "u_eng", monkeypatch):
        accounts = mcp._account_paths("drive")

    assert [label for label, _ in accounts] == ["mine"]
    assert all("_shared" not in str(path) for _, path in accounts)


def test_admin_still_receives_the_shared_company_grant(workspace, monkeypatch):
    with _as(workspace, "admin", monkeypatch, dwd=False):
        accounts = mcp._account_paths("gmail")

    assert [label for label, _ in accounts] == ["company"]


def test_member_without_delegation_gets_nothing_rather_than_a_fallback(
    workspace, monkeypatch
):
    """A personal OAuth grant must not become the route company data walks out."""
    personal = workspace / "connections" / "u_eng" / "drive.json"
    personal.write_text("{}")

    with _as(workspace, "u_eng", monkeypatch, dwd=False):
        accounts = mcp._account_paths("drive")
        with pytest.raises(RuntimeError, match="admin only"):
            mcp._merged("drive", lambda: {"files": []}, "files")

    assert accounts == []


def test_member_mutations_refuse_a_personal_grant(workspace, monkeypatch):
    (workspace / "connections" / "u_eng" / "gmail.json").write_text("{}")

    with _as(workspace, "u_eng", monkeypatch, dwd=False):
        with pytest.raises(RuntimeError, match="admin only"):
            mcp._own_account_call("gmail", lambda: {"ok": True})


def test_members_read_their_own_calendar_not_the_admins(workspace, monkeypatch):
    """Calendar has a delegation scope, so 'mine' resolves before any share."""
    assert "calendar" in mcp._DWD_SCOPES

    with _as(workspace, "u_mgr", monkeypatch):
        assert [label for label, _ in mcp._account_paths("calendar")] == ["mine"]


# --- which documents a group may open ---------------------------------------

@pytest.mark.parametrize("name", [
    "2026 Payroll summary.xlsx",
    "Salaries Q3.gsheet",
    "BOA bank statement June.pdf",
    "Nimo passport scan.pdf",
    "员工工资表 2026.xlsx",
    "offer letter - Tofik.docx",
])
def test_engineers_cannot_open_finance_hr_or_identity_files(name, workspace):
    assert policy.drive_denial(actor(workspace, "u_eng"), name)


@pytest.mark.parametrize("name", [
    "GRABO Pro firmware notes.md",
    "vacuum anchor test plan.docx",
    "invoice-app schema.sql",
    "sitemap audit 2026-08.csv",
])
def test_ordinary_engineering_files_stay_open(name, workspace):
    """A guard that fires on normal work teaches people to route around it."""
    assert policy.drive_denial(actor(workspace, "u_eng"), name) == ""


def test_accounting_may_open_payroll_but_not_identity_documents(workspace):
    who = actor(workspace, "u_acct")

    assert policy.drive_denial(who, "2026 Payroll summary.xlsx") == ""
    assert policy.drive_denial(who, "Nimo passport scan.pdf")


def test_managers_and_admin_open_everything(workspace):
    for user_id in ("u_mgr", "admin"):
        assert policy.drive_denial(actor(workspace, user_id),
                                   "2026 Payroll summary.xlsx") == ""


def test_an_unknown_group_gets_the_strictest_rules_not_a_free_pass(workspace):
    assert policy.drive_denial(actor(workspace, "u_new"), "Payroll 2026.xlsx")


@pytest.mark.parametrize("query", ["payroll", "salary 2026", "工资表", "passport"])
def test_engineers_cannot_sweep_drive_for_sensitive_material(
    query, workspace, monkeypatch
):
    """Full-text search returns innocently named files, so the query is checked."""
    with _as(workspace, "u_eng", monkeypatch), \
            patch.object(mcp, "_merged") as searched:
        with pytest.raises(policy.PolicyDenied):
            mcp.drive_search(query)

    searched.assert_not_called()


def test_managers_may_still_search_for_payroll(workspace, monkeypatch):
    with _as(workspace, "u_mgr", monkeypatch), \
            patch.object(mcp, "_merged", return_value={"files": []}):
        assert mcp.drive_search("payroll")["files"] == []


def test_ordinary_searches_are_untouched(workspace, monkeypatch):
    with _as(workspace, "u_eng", monkeypatch), \
            patch.object(mcp, "_merged", return_value={"files": []}):
        assert mcp.drive_search("vacuum anchor test plan")["files"] == []


def test_drive_search_hides_denied_files_and_says_how_many(workspace, monkeypatch):
    listing = {"files": [
        {"id": "1", "name": "firmware notes.md"},
        {"id": "2", "name": "2026 Payroll summary.xlsx"},
    ]}
    with _as(workspace, "u_eng", monkeypatch), \
            patch.object(mcp, "_merged", return_value=dict(listing)):
        result = mcp.drive_search("anything")

    assert [item["name"] for item in result["files"]] == ["firmware notes.md"]
    assert result["withheld"] == 1


def test_drive_read_refuses_before_any_content_is_fetched(workspace, monkeypatch):
    calls = []

    def fake_get(service, path, params=None, raw=False):
        calls.append(path)
        return {"id": "f1", "name": "2026 Payroll summary.xlsx",
                "mimeType": "text/plain"}

    with _as(workspace, "u_eng", monkeypatch), \
            patch.object(mcp, "_api_get", fake_get):
        with pytest.raises(policy.PolicyDenied):
            mcp._drive_read_one("f1")

    assert calls == ["/drive/v3/files/f1"], "content must not be requested"


# --- where mail may go -------------------------------------------------------

def test_no_member_may_mail_a_personal_mailbox(workspace, monkeypatch):
    for user_id in ("u_eng", "u_mgr", "u_acct"):
        with _as(workspace, user_id, monkeypatch):
            with pytest.raises(policy.PolicyDenied, match="personal mailbox"):
                mcp._gmail_raw_message("someone@gmail.com", "s", "b")


def test_a_personal_mailbox_hidden_in_bcc_is_still_refused(workspace, monkeypatch):
    with _as(workspace, "u_mgr", monkeypatch):
        with pytest.raises(policy.PolicyDenied, match="personal mailbox"):
            mcp._gmail_raw_message(
                "buyer@supplier.com", "s", "b", bcc="Guy <guy@qq.com>"
            )


def test_engineers_may_mail_colleagues_but_not_outside_the_company(
    workspace, monkeypatch
):
    with _as(workspace, "u_eng", monkeypatch):
        assert mcp._gmail_raw_message("guy@nemopowertools.com", "s", "b")
        with pytest.raises(policy.PolicyDenied, match="outside the company"):
            mcp._gmail_raw_message("buyer@supplier.com", "s", "b")


def test_managers_may_mail_business_addresses_outside_the_company(
    workspace, monkeypatch
):
    with _as(workspace, "u_mgr", monkeypatch):
        assert mcp._gmail_raw_message("buyer@supplier.com", "s", "b")


def test_a_draft_is_not_a_way_around_the_recipient_rules(workspace, monkeypatch):
    with _as(workspace, "u_eng", monkeypatch), \
            patch.object(mcp, "_own_account_call") as sent:
        with pytest.raises(policy.PolicyDenied):
            mcp.gmail_create_draft("someone@gmail.com", "s", "b")

    sent.assert_not_called()


def test_a_recipient_list_that_cannot_be_parsed_is_refused(workspace, monkeypatch):
    """Failing to read a header must not read as 'no recipients to check'."""
    with _as(workspace, "u_mgr", monkeypatch):
        with pytest.raises(policy.PolicyDenied, match="could not be read"):
            mcp._gmail_raw_message("Guy <guy(at)qq.com>", "s", "b")


# --- the record --------------------------------------------------------------

def test_every_call_is_recorded_with_the_account_and_the_outcome(
    workspace, monkeypatch
):
    with _as(workspace, "u_eng", monkeypatch):
        with patch.object(mcp, "_merged", return_value={"files": []}):
            mcp.drive_search("anchor test plan")
        with patch.object(mcp, "_own_account_call") as unused:
            with pytest.raises(policy.PolicyDenied):
                mcp.gmail_send("someone@gmail.com", "s", "b")
            unused.assert_not_called()

    entries = policy.read_audit(workspace / "audit.jsonl", limit=10)
    assert [entry["tool"] for entry in entries] == ["gmail_send", "drive_search"]
    denied, allowed = entries
    assert denied["decision"] == "denied" and denied["username"] == "bill"
    assert "someone@gmail.com" in denied["detail"]
    assert allowed["decision"] == "allowed" and allowed["group"] == "engineers"


def test_the_audit_log_never_records_a_message_body(workspace, monkeypatch):
    with _as(workspace, "u_mgr", monkeypatch), \
            patch.object(mcp, "_own_account_call", return_value={"id": "m1"}):
        mcp.gmail_send(
            "colleague@grabo.com", "Q3 numbers", "EBITDA was 1,234,567 dollars"
        )

    entry = policy.read_audit(workspace / "audit.jsonl", limit=1)[0]
    assert "1,234,567" not in json.dumps(entry)
    assert entry["detail"].startswith("to=colleague@grabo.com")
