#!/usr/bin/env python3
"""Per-user Google Workspace read and write tools for Codex.

The dashboard starts one stdio MCP process per Codex session and binds it to
that member's private OAuth-token directory or company Workspace delegation
subject. The MCP dependency is imported only by ``main()`` so the REST/token
behavior remains easy to test in the dashboard's regular Python environment.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

_GOOGLE_API_BASES = {
    "drive": "https://www.googleapis.com",
    "gmail": "https://gmail.googleapis.com",
    "calendar": "https://www.googleapis.com",
}
_USER_AGENT = "Grabo-Google-Workspace-MCP/1.0"
_MAX_TEXT_BYTES = 100_000
_GOOGLE_EXPORT_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
    "application/vnd.google-apps.script": (
        "application/vnd.google-apps.script+json"
    ),
}
_DWD_SCOPES = {
    "drive": "https://www.googleapis.com/auth/drive",
    "gmail": "https://www.googleapis.com/auth/gmail.modify",
}
_DWD_ACCOUNT_MARKER = Path("/__grabo_google_workspace_dwd__")
_DWD_TOKEN_CACHE: dict[str, dict[str, Any]] = {}


def _credentials_dir() -> Path:
    raw = os.environ.get("GOOGLE_MCP_CREDENTIALS_DIR", "").strip()
    if not raw:
        raise RuntimeError("Google credentials directory is not configured.")
    return Path(raw).expanduser()


def _client_file() -> Path:
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_FILE", "").strip()
    if not raw:
        raise RuntimeError("Google OAuth client is not configured.")
    return Path(raw).expanduser()


def _dwd_service_account_file() -> Path | None:
    raw = os.environ.get(
        "GOOGLE_WORKSPACE_DWD_SERVICE_ACCOUNT_FILE",
        "",
    ).strip() or str(Path.home() / ".gworkspace-admin" / "sa-key.json")
    path = Path(raw).expanduser()
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked Workspace delegation key.")
    return path


def _dwd_subject() -> str:
    configured = os.environ.get(
        "GOOGLE_WORKSPACE_DWD_SUBJECT",
        "",
    ).strip().lower()
    if configured:
        return configured
    try:
        credentials_dir = _credentials_dir()
        user_id = credentials_dir.name
        users_file = credentials_dir.parent.parent / "users.json"
        value = json.loads(users_file.read_text())
        users = value.get("users") if isinstance(value, dict) else []
        user = next(
            item
            for item in users
            if isinstance(item, dict) and item.get("id") == user_id
        )
        email = str(user.get("google_email") or "").strip().lower()
    except (OSError, ValueError, TypeError, StopIteration, json.JSONDecodeError):
        return ""
    allowed_domains = {
        item.strip().lower().lstrip("@")
        for item in os.environ.get(
            "GOOGLE_WORKSPACE_DWD_DOMAINS",
            "grabo.com,nemopowertools.com",
        ).split(",")
        if item.strip()
    }
    if "@" not in email or email.split("@", 1)[1] not in allowed_domains:
        return ""
    return email


def _dwd_available(service: str) -> bool:
    key_file = _dwd_service_account_file()
    return bool(
        service in _DWD_SCOPES
        and _dwd_subject()
        and key_file is not None
        and key_file.is_file()
    )


def _dwd_access_token(service: str) -> str:
    """Mint and cache a short-lived token for this member's Workspace account."""
    if not _dwd_available(service):
        raise RuntimeError(
            f"Delegated {service.title()} access is not configured."
        )
    key_file = _dwd_service_account_file()
    subject = _dwd_subject()
    assert key_file is not None
    cache_key = f"{service}:{subject}:{key_file}"
    cached = _DWD_TOKEN_CACHE.get(cache_key) or {}
    if (
        cached.get("token")
        and time.time() < float(cached.get("expires_at") or 0) - 120
    ):
        return str(cached["token"])

    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account

        credentials = service_account.Credentials.from_service_account_file(
            str(key_file),
            scopes=[_DWD_SCOPES[service]],
            subject=subject,
        )
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:
        raise RuntimeError(
            f"Could not authorize delegated {service.title()} access."
        ) from exc
    token = str(credentials.token or "")
    if not token:
        raise RuntimeError(
            f"Google returned no delegated {service.title()} token."
        )
    expiry = credentials.expiry
    expires_at = expiry.timestamp() if expiry is not None else time.time() + 3600
    _DWD_TOKEN_CACHE[cache_key] = {
        "token": token,
        "expires_at": expires_at,
    }
    return token


SHARED_CREDENTIALS_DIR_NAME = "_shared"


def _shared_credentials_dir() -> Path | None:
    """Where the company-wide grant lives.

    Defaults to a `_shared` sibling of this member's own credentials directory,
    so an existing config.toml needs no rewrite to pick it up.
    """
    raw = os.environ.get("GOOGLE_MCP_SHARED_CREDENTIALS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        return _credentials_dir().parent / SHARED_CREDENTIALS_DIR_NAME
    except RuntimeError:
        return None


def _checked_token_file(directory: Path, service: str) -> Path:
    path = directory / f"{service}.json"
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked Google credential file.")
    return path


def _account_paths(service: str) -> list[tuple[str, Path]]:
    """Every grant this member may use, as (label, token file).

    The company account comes first, then the member's own if they connected
    one. Both are searched: someone who signed in with their personal Google
    account still needs to find company documents.
    """
    if service not in _GOOGLE_API_BASES:
        raise ValueError("Unsupported Google service.")
    found: list[tuple[str, Path]] = []
    own = _checked_token_file(_credentials_dir(), service)
    shared_dir = _shared_credentials_dir()
    # Company first. This is a shared work environment and the company archive
    # is what people are looking for; when a member also has a personal account
    # its files would otherwise fill the top of every result list.
    if shared_dir is not None:
        shared = _checked_token_file(shared_dir, service)
        if shared.exists() and shared != own:
            found.append(("company", shared))
    if _dwd_available(service):
        found.append(("mine", _DWD_ACCOUNT_MARKER))
    elif own.exists():
        found.append(("mine", own))
    return found


# Set while a call is pinned to one account; see _use_account.
_ACTIVE_ACCOUNT: dict[str, Path] = {}


@contextlib.contextmanager
def _use_account(service: str, path: Path):
    previous = _ACTIVE_ACCOUNT.get(service)
    _ACTIVE_ACCOUNT[service] = path
    try:
        yield
    finally:
        if previous is None:
            _ACTIVE_ACCOUNT.pop(service, None)
        else:
            _ACTIVE_ACCOUNT[service] = previous


def _token_path(service: str) -> Path:
    """The token file the current call should use."""
    pinned = _ACTIVE_ACCOUNT.get(service)
    if pinned is not None:
        return pinned
    accounts = _account_paths(service)
    if accounts:
        return accounts[0][1]
    return _checked_token_file(_credentials_dir(), service)


def _merged(service: str, call, key: str, *args, **kwargs) -> dict[str, Any]:
    """Run `call` against every account and merge its `key` list.

    One account failing (an expired personal grant, say) must not hide the
    other's results, so failures are collected and reported alongside them.
    """
    accounts = _account_paths(service)
    if not accounts:
        raise RuntimeError(
            f"{service.title()} is not connected. Ask an admin to connect the "
            "company account in dashboard Settings, or connect your own."
        )
    items: list[Any] = []
    extra: dict[str, Any] = {}
    problems: list[str] = []
    seen: set[str] = set()
    for label, path in accounts:
        try:
            with _use_account(service, path):
                result = call(*args, **kwargs)
        except Exception as exc:                       # one account, not the tool
            problems.append(f"{label}: {exc}")
            continue
        for item in result.get(key, []) or []:
            ident = str(item.get("id") or "") if isinstance(item, dict) else ""
            if ident and ident in seen:
                continue
            if ident:
                seen.add(ident)
            if isinstance(item, dict):
                item["account"] = label
            items.append(item)
        for other_key, value in result.items():
            if other_key != key and value:
                extra.setdefault(other_key, value)
    if not items and problems and len(problems) == len(accounts):
        raise RuntimeError("; ".join(problems))
    out = {key: items, "accounts_searched": [label for label, _ in accounts]}
    out.update(extra)
    if problems:
        out["warnings"] = problems
    return out


def _first_account_that_works(service: str, call, *args, **kwargs) -> dict[str, Any]:
    """Read one item: try each account until one returns it."""
    accounts = _account_paths(service)
    if not accounts:
        raise RuntimeError(f"{service.title()} is not connected.")
    problems = []
    for label, path in accounts:
        try:
            with _use_account(service, path):
                result = call(*args, **kwargs)
            result["account"] = label
            return result
        except Exception as exc:
            problems.append(f"{label}: {exc}")
    raise RuntimeError("; ".join(problems))


def _oauth_client() -> tuple[str, str]:
    try:
        value = json.loads(_client_file().read_text())
        value = value.get("web") or value.get("installed") or value
        client_id = str(value.get("client_id") or "")
        client_secret = str(value.get("client_secret") or "")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not load the Google OAuth client.") from exc
    if not client_id or not client_secret:
        raise RuntimeError("Google OAuth client credentials are incomplete.")
    return client_id, client_secret


def _atomic_write_token(path: Path, token: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(token, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _refresh_access_token(
    service: str,
    token: dict[str, Any],
    path: Path,
) -> str:
    refresh_token = str(token.get("refresh_token") or "")
    if not refresh_token:
        raise RuntimeError(
            f"{service.title()} needs to be reconnected in dashboard Settings."
        )
    client_id, client_secret = _oauth_client()
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            refreshed = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Google rejected the {service.title()} token refresh "
            f"(HTTP {exc.code}); reconnect it in dashboard Settings."
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not refresh {service.title()}; retry shortly."
        ) from exc

    access_token = str(refreshed.get("access_token") or "")
    if not access_token:
        raise RuntimeError(
            f"Google returned no {service.title()} access token."
        )
    token["access_token"] = access_token
    token["expires_in"] = int(refreshed.get("expires_in") or 3600)
    token["_obtained_at"] = time.time()
    _atomic_write_token(path, token)
    return access_token


def _access_token(service: str) -> str:
    path = _token_path(service)
    if path == _DWD_ACCOUNT_MARKER:
        return _dwd_access_token(service)
    if not path.exists():
        raise RuntimeError(
            f"{service.title()} is not connected. Sign in with Google or "
            "connect it in dashboard Settings."
        )
    try:
        token = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"The saved {service.title()} connection is invalid; reconnect it."
        ) from exc
    if not isinstance(token, dict):
        raise RuntimeError(
            f"The saved {service.title()} connection is invalid; reconnect it."
        )
    access_token = str(token.get("access_token") or "")
    obtained_at = float(token.get("_obtained_at") or 0)
    expires_in = max(0, int(token.get("expires_in") or 3600))
    if access_token and time.time() < obtained_at + expires_in - 120:
        return access_token
    return _refresh_access_token(service, token, path)


def _api_request(
    service: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    body: bytes | None = None,
    content_type: str = "",
    raw: bool = False,
) -> Any:
    """Call one allowlisted Google REST origin with the user's bearer token."""
    if service not in _GOOGLE_API_BASES or not path.startswith("/"):
        raise ValueError("Unsupported Google API endpoint.")
    if json_body is not None and body is not None:
        raise ValueError("Use either json_body or body, not both.")
    url = _GOOGLE_API_BASES[service] + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    if json_body is not None:
        body = json.dumps(json_body).encode()
        content_type = "application/json"
    headers = {
        "Authorization": "Bearer " + _access_token(service),
        "User-Agent": _USER_AGENT,
    }
    if body is not None:
        headers["Content-Type"] = content_type or "application/octet-stream"
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            if raw:
                return payload
            return json.loads(payload) if payload else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(500).decode("utf-8", "replace")
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(
            f"Google {service.title()} API failed (HTTP {exc.code}){suffix}"
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Could not call Google {service.title()}; retry shortly."
        ) from exc


def _api_get(
    service: str,
    path: str,
    params: dict[str, Any] | None = None,
    *,
    raw: bool = False,
) -> Any:
    return _api_request(service, path, params, raw=raw)


def _own_account_call(service: str, call, *args, **kwargs) -> dict[str, Any]:
    """Run a mutation only as this dashboard user's own Google identity."""
    own = _checked_token_file(_credentials_dir(), service)
    if _dwd_available(service):
        credential = _DWD_ACCOUNT_MARKER
    elif own.exists():
        credential = own
    else:
        raise RuntimeError(
            f"Your own {service.title()} account is not connected. "
            "Sign out and continue with Google again."
        )
    with _use_account(service, credential):
        result = call(*args, **kwargs)
    if isinstance(result, dict):
        result["account"] = "mine"
    return result


def _drive_query_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _drive_search_one(
    query: str = "",
    page_size: int = 20,
    page_token: str = "",
) -> dict[str, Any]:
    """Search all non-trashed Drive files, including shared-drive content."""
    clean_query = str(query or "").strip()
    drive_query = "trashed = false"
    if clean_query:
        drive_query = (
            "fullText contains '"
            + _drive_query_literal(clean_query)
            + "' and trashed = false"
        )
    params = {
        "q": drive_query,
        "pageSize": min(max(int(page_size), 1), 100),
        "orderBy": "modifiedTime desc",
        "spaces": "drive",
        "corpora": "user",
        "includeItemsFromAllDrives": "true",
        "supportsAllDrives": "true",
        "fields": (
            "nextPageToken,files(id,name,mimeType,modifiedTime,size,"
            "webViewLink,driveId,owners(displayName,emailAddress))"
        ),
    }
    if page_token:
        params["pageToken"] = str(page_token)
    data = _api_get("drive", "/drive/v3/files", params)
    return {
        "files": data.get("files", []),
        "nextPageToken": data.get("nextPageToken", ""),
    }


def _drive_read_one(file_id: str) -> dict[str, Any]:
    """Read or export one Drive file, capped at 100 KB of returned text."""
    safe_id = urllib.parse.quote(str(file_id or "").strip(), safe="")
    if not safe_id:
        raise ValueError("file_id is required.")
    metadata = _api_get(
        "drive",
        f"/drive/v3/files/{safe_id}",
        {
            "fields": (
                "id,name,mimeType,modifiedTime,size,webViewLink,"
                "driveId,owners(displayName,emailAddress)"
            ),
            "supportsAllDrives": "true",
        },
    )
    mime_type = str(metadata.get("mimeType") or "")
    if mime_type.startswith("application/vnd.google-apps."):
        export_type = _GOOGLE_EXPORT_TYPES.get(mime_type)
        if not export_type:
            return {
                "file": metadata,
                "content": "",
                "note": (
                    "This Google-native file type has no safe text export. "
                    "Use its webViewLink."
                ),
                "truncated": False,
            }
        content_path = f"/drive/v3/files/{safe_id}/export"
        content_params = {"mimeType": export_type}
    else:
        content_path = f"/drive/v3/files/{safe_id}"
        content_params = {
            "alt": "media",
            "supportsAllDrives": "true",
        }
    payload = _api_get(
        "drive",
        content_path,
        content_params,
        raw=True,
    )
    limited = payload[:_MAX_TEXT_BYTES]
    return {
        "file": metadata,
        "content": limited.decode("utf-8", "replace"),
        "truncated": len(payload) > len(limited),
    }


_MAX_WRITE_BYTES = 10_000_000


def _drive_create_text_one(
    name: str,
    content: str,
    mime_type: str = "text/plain",
    folder_id: str = "",
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name or len(clean_name) > 255:
        raise ValueError("name is required and must be at most 255 characters.")
    clean_type = str(mime_type or "text/plain").strip()
    if "\r" in clean_type or "\n" in clean_type:
        raise ValueError("mime_type is invalid.")
    payload = str(content or "").encode()
    if len(payload) > _MAX_WRITE_BYTES:
        raise ValueError("content exceeds the 10 MB write limit.")
    metadata: dict[str, Any] = {"name": clean_name}
    clean_folder = str(folder_id or "").strip()
    if clean_folder:
        metadata["parents"] = [clean_folder]
    boundary = "grabo_" + uuid.uuid4().hex
    delimiter = ("--" + boundary + "\r\n").encode()
    close_delimiter = ("--" + boundary + "--\r\n").encode()
    multipart = b"".join((
        delimiter,
        b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
        json.dumps(metadata).encode(),
        b"\r\n",
        delimiter,
        ("Content-Type: " + clean_type + "\r\n\r\n").encode(),
        payload,
        b"\r\n",
        close_delimiter,
    ))
    return _api_request(
        "drive",
        "/upload/drive/v3/files",
        {
            "uploadType": "multipart",
            "supportsAllDrives": "true",
            "fields": "id,name,mimeType,modifiedTime,webViewLink,driveId",
        },
        method="POST",
        body=multipart,
        content_type=f"multipart/related; boundary={boundary}",
    )


def _drive_update_text_one(
    file_id: str,
    content: str,
    mime_type: str = "text/plain",
) -> dict[str, Any]:
    safe_id = urllib.parse.quote(str(file_id or "").strip(), safe="")
    if not safe_id:
        raise ValueError("file_id is required.")
    clean_type = str(mime_type or "text/plain").strip()
    if "\r" in clean_type or "\n" in clean_type:
        raise ValueError("mime_type is invalid.")
    payload = str(content or "").encode()
    if len(payload) > _MAX_WRITE_BYTES:
        raise ValueError("content exceeds the 10 MB write limit.")
    return _api_request(
        "drive",
        f"/upload/drive/v3/files/{safe_id}",
        {
            "uploadType": "media",
            "supportsAllDrives": "true",
            "fields": "id,name,mimeType,modifiedTime,webViewLink,driveId",
        },
        method="PATCH",
        body=payload,
        content_type=clean_type,
    )


def _drive_move_to_trash_one(file_id: str) -> dict[str, Any]:
    safe_id = urllib.parse.quote(str(file_id or "").strip(), safe="")
    if not safe_id:
        raise ValueError("file_id is required.")
    return _api_request(
        "drive",
        f"/drive/v3/files/{safe_id}",
        {
            "supportsAllDrives": "true",
            "fields": "id,name,trashed,modifiedTime",
        },
        method="PATCH",
        json_body={"trashed": True},
    )


def _gmail_search_one(query: str = "", page_size: int = 10) -> dict[str, Any]:
    """Search connected Gmail and return message metadata."""
    listing = _api_get(
        "gmail",
        "/gmail/v1/users/me/messages",
        {
            "q": str(query or ""),
            "maxResults": min(max(int(page_size), 1), 25),
        },
    )
    messages = []
    for item in listing.get("messages", []):
        message_id = urllib.parse.quote(str(item.get("id") or ""), safe="")
        if not message_id:
            continue
        value = _api_get(
            "gmail",
            f"/gmail/v1/users/me/messages/{message_id}",
            {
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Subject", "Date"],
            },
        )
        headers = {
            header.get("name", ""): header.get("value", "")
            for header in value.get("payload", {}).get("headers", [])
        }
        messages.append({
            "id": item.get("id"),
            "threadId": item.get("threadId"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "snippet": value.get("snippet", ""),
        })
    return {"messages": messages}


def _decode_websafe_text(value: str) -> str:
    encoded = str(value or "")
    try:
        decoded = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        )
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Gmail returned an invalid message body.") from exc
    return decoded.decode("utf-8", "replace")


def _plain_gmail_body(part: dict[str, Any]) -> str:
    if (
        part.get("mimeType") == "text/plain"
        and part.get("body", {}).get("data")
    ):
        return _decode_websafe_text(part["body"]["data"])
    for child in part.get("parts", []) or []:
        body = _plain_gmail_body(child)
        if body:
            return body
    return ""


def _gmail_read_one(message_id: str) -> dict[str, Any]:
    """Read one connected Gmail message."""
    safe_id = urllib.parse.quote(str(message_id or "").strip(), safe="")
    if not safe_id:
        raise ValueError("message_id is required.")
    value = _api_get(
        "gmail",
        f"/gmail/v1/users/me/messages/{safe_id}",
        {"format": "full"},
    )
    headers = {
        header.get("name", ""): header.get("value", "")
        for header in value.get("payload", {}).get("headers", [])
    }
    body = _plain_gmail_body(value.get("payload", {}))
    if not body:
        body = str(value.get("snippet") or "")
    return {
        "id": value.get("id"),
        "threadId": value.get("threadId"),
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": body[:50_000],
        "truncated": len(body) > 50_000,
    }


def _gmail_raw_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> str:
    clean_to = str(to or "").strip()
    if not clean_to:
        raise ValueError("to is required.")
    message = EmailMessage()
    message["To"] = clean_to
    message["Subject"] = str(subject or "")
    if str(cc or "").strip():
        message["Cc"] = str(cc).strip()
    if str(bcc or "").strip():
        message["Bcc"] = str(bcc).strip()
    message.set_content(str(body or ""))
    return base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")


def _gmail_send_one(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> dict[str, Any]:
    return _api_request(
        "gmail",
        "/gmail/v1/users/me/messages/send",
        method="POST",
        json_body={"raw": _gmail_raw_message(to, subject, body, cc, bcc)},
    )


def _gmail_create_draft_one(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> dict[str, Any]:
    return _api_request(
        "gmail",
        "/gmail/v1/users/me/drafts",
        method="POST",
        json_body={
            "message": {
                "raw": _gmail_raw_message(to, subject, body, cc, bcc),
            },
        },
    )


def _gmail_modify_labels_one(
    message_id: str,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> dict[str, Any]:
    safe_id = urllib.parse.quote(str(message_id or "").strip(), safe="")
    if not safe_id:
        raise ValueError("message_id is required.")
    return _api_request(
        "gmail",
        f"/gmail/v1/users/me/messages/{safe_id}/modify",
        method="POST",
        json_body={
            "addLabelIds": [str(value) for value in (add_label_ids or [])],
            "removeLabelIds": [str(value) for value in (remove_label_ids or [])],
        },
    )


def _gmail_move_to_trash_one(message_id: str) -> dict[str, Any]:
    safe_id = urllib.parse.quote(str(message_id or "").strip(), safe="")
    if not safe_id:
        raise ValueError("message_id is required.")
    return _api_request(
        "gmail",
        f"/gmail/v1/users/me/messages/{safe_id}/trash",
        method="POST",
        json_body={},
    )


def _calendar_list_events_one(
    page_size: int = 20,
    query: str = "",
) -> dict[str, Any]:
    """List upcoming events from the connected primary Google Calendar."""
    params = {
        "maxResults": min(max(int(page_size), 1), 50),
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": datetime.now(timezone.utc).isoformat(),
    }
    if query:
        params["q"] = str(query)
    value = _api_get(
        "calendar",
        "/calendar/v3/calendars/primary/events",
        params,
    )
    return {"events": value.get("items", [])}



# --- public tools: every account the member is entitled to -------------------

def drive_search(
    query: str = "",
    page_size: int = 20,
    page_token: str = "",
) -> dict[str, Any]:
    """Search company Google Drive (and your own, if you connected it).

    Covers shared drives and everything shared with the account. Each result
    carries `account`: "company" for the shared GRABO Drive, "mine" for your own.
    """
    return _merged("drive", _drive_search_one, "files", query, page_size, page_token)


def drive_read(file_id: str) -> dict[str, Any]:
    """Read or export one Drive file (100 KB of text max), from either account."""
    return _first_account_that_works("drive", _drive_read_one, file_id)


def drive_create_text(
    name: str,
    content: str,
    mime_type: str = "text/plain",
    folder_id: str = "",
) -> dict[str, Any]:
    """Create a text file in your own Drive, optionally inside one folder."""
    return _own_account_call(
        "drive",
        _drive_create_text_one,
        name,
        content,
        mime_type,
        folder_id,
    )


def drive_update_text(
    file_id: str,
    content: str,
    mime_type: str = "text/plain",
) -> dict[str, Any]:
    """Replace one file's content in your own Drive (10 MB maximum)."""
    return _own_account_call(
        "drive",
        _drive_update_text_one,
        file_id,
        content,
        mime_type,
    )


def drive_move_to_trash(file_id: str) -> dict[str, Any]:
    """Move one file in your own Drive to trash; this is recoverable."""
    return _own_account_call("drive", _drive_move_to_trash_one, file_id)


def gmail_search(query: str = "", page_size: int = 10) -> dict[str, Any]:
    """Search the company mailbox (and your own, if you connected it).

    Accepts normal Gmail query syntax, e.g. `from:someone@x.com has:attachment`.
    Each message carries `account`: "company" or "mine".
    """
    return _merged("gmail", _gmail_search_one, "messages", query, page_size)


def gmail_read(message_id: str) -> dict[str, Any]:
    """Read one message by id, from whichever account holds it."""
    return _first_account_that_works("gmail", _gmail_read_one, message_id)


def gmail_send(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> dict[str, Any]:
    """Send an email from your own connected Gmail account."""
    return _own_account_call(
        "gmail",
        _gmail_send_one,
        to,
        subject,
        body,
        cc,
        bcc,
    )


def gmail_create_draft(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
) -> dict[str, Any]:
    """Create a draft in your own connected Gmail account."""
    return _own_account_call(
        "gmail",
        _gmail_create_draft_one,
        to,
        subject,
        body,
        cc,
        bcc,
    )


def gmail_modify_labels(
    message_id: str,
    add_label_ids: list[str] | None = None,
    remove_label_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Add or remove labels on one message in your own Gmail account."""
    return _own_account_call(
        "gmail",
        _gmail_modify_labels_one,
        message_id,
        add_label_ids,
        remove_label_ids,
    )


def gmail_move_to_trash(message_id: str) -> dict[str, Any]:
    """Move one message in your own Gmail account to trash; this is recoverable."""
    return _own_account_call("gmail", _gmail_move_to_trash_one, message_id)


def calendar_list_events(page_size: int = 20, query: str = "") -> dict[str, Any]:
    """Upcoming events from the company calendar (and your own, if connected)."""
    return _merged("calendar", _calendar_list_events_one, "events", page_size, query)


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("google-workspace")
    for tool in (
        drive_search,
        drive_read,
        drive_create_text,
        drive_update_text,
        drive_move_to_trash,
        gmail_search,
        gmail_read,
        gmail_send,
        gmail_create_draft,
        gmail_modify_labels,
        gmail_move_to_trash,
        calendar_list_events,
    ):
        server.tool()(tool)
    server.run()


if __name__ == "__main__":
    main()
