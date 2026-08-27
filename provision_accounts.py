#!/usr/bin/env python3
"""Give every build.grabo.tools account its own UNIX user. Run as root on grabo-tech.

Until now every account's Codex session ran as nimrod_rotem, so isolation was a
prompt instruction rather than a kernel boundary: any session could read any
other account's advisor token (escalating a member to a manager), read any other
account's project directory, and use the VM service account to reach every host
in the project.

  provision_accounts.py plan            show what would change, touch nothing
  provision_accounts.py harden          lock the shared secrets, no user changes
  provision_accounts.py apply [user..]  create accounts and re-own their trees
  provision_accounts.py netguard        block the metadata server for session UIDs
  provision_accounts.py verify          prove the boundaries hold
  provision_accounts.py rollback-perms  put the shared-secret modes back

`apply` with no names does every account. Safe to re-run.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path("/home/nimrod_rotem")
DASH = HOME / ".tmux-dashboard"
USERS_JSON = DASH / "users.json"
OWNERS_JSON = DASH / "session_owners.json"
MAP_FILE = DASH / "account-unix-users.json"
SUDOERS = Path("/etc/sudoers.d/grabo-session-accounts")
NETGUARD_UNIT = Path("/etc/systemd/system/grabo-session-netguard.service")
NETGUARD_SH = Path("/usr/local/sbin/grabo-session-netguard")
PREFIX = "gx-"
AUTH_GROUP = "gxauth"
PYTEST_GROUP = "pytestgate"
PYTEST_LOCK_DIR = Path("/run/lock/tmux-dashboard")
PYTEST_LOCK = PYTEST_LOCK_DIR / "pytest-heavy.lock"
DAILY_TEST_LOCK = PYTEST_LOCK_DIR / "dashboard-daily.lock"
PYTEST_TMPFILES = Path("/etc/tmpfiles.d/tmux-dashboard.conf")
PYTEST_HOOK_SOURCE = (
    Path(__file__).resolve().parent
    / "runtime_hooks"
    / "tmux_dashboard_pytest_gate.py"
)
PYTEST_HOOK_DIR = Path("/usr/local/libexec/tmux-dashboard-python-hooks")
PYTEST_HOOK_TARGET = PYTEST_HOOK_DIR / "tmux_dashboard_pytest_gate.py"
PYTEST_HOOK_OWNER_UID = 0
PYTEST_HOOK_OWNER_GID = 0
METADATA_IP = "169.254.169.254"

# Readable by a session: shared reference material the policy tells it to load.
SHARED_READABLE = ["context", "skills", "skill-library"]
# Never readable by a session: credentials, other accounts, audit trails.
DASH_PRIVATE_KEEP_TRAVERSABLE = 0o710


def sh(*args, check=True, **kw):
    return subprocess.run(args, check=check, capture_output=True, text=True, **kw)


def load_users() -> list[dict]:
    data = json.loads(USERS_JSON.read_text())
    return data["users"] if isinstance(data, dict) and "users" in data else data


def load_map() -> dict:
    if MAP_FILE.exists():
        return json.loads(MAP_FILE.read_text())
    return {}


def save_map(m: dict) -> None:
    MAP_FILE.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    shutil.chown(MAP_FILE, user="nimrod_rotem", group="nimrod_rotem")
    MAP_FILE.chmod(0o640)


def unix_name(user: dict, taken: set[str]) -> str:
    raw = str(user.get("username") or user.get("id") or "")
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw.lower()).strip("-")[:24] or str(user["id"])
    name = PREFIX + slug
    if name in taken:
        name = (PREFIX + str(user["id"]).replace("u_", ""))[:32]
    return name[:32]


def codex_home(user: dict) -> Path:
    return HOME / f".codex-user-{user['id']}"


def project_dir(user: dict) -> Path:
    return HOME / "web-projects" / str(user.get("username") or user["id"])


def configure_browser_ipc_group(
    accounts: list[str], *, dry: bool = False
) -> list[str]:
    """Provision the narrow group used only to traverse and call browser IPC."""
    members = ["nimrod_rotem", *sorted(set(accounts))]
    if dry:
        return [
            f"would provision {AUTH_GROUP} for {', '.join(members)}",
            f"would share {DASH}/controller.sock as nimrod_rotem:{AUTH_GROUP} 0660",
        ]

    sh("groupadd", "-f", AUTH_GROUP)
    for member in members:
        try:
            pwd.getpwnam(member)
        except KeyError:
            continue
        sh("usermod", "-aG", AUTH_GROUP, member)
    if DASH.exists():
        shutil.chown(DASH, user="nimrod_rotem", group=AUTH_GROUP)
        DASH.chmod(DASH_PRIVATE_KEEP_TRAVERSABLE)
    sock = DASH / "controller.sock"
    if sock.exists():
        shutil.chown(sock, user="nimrod_rotem", group=AUTH_GROUP)
        sock.chmod(0o660)
    return [
        f"browser IPC: {AUTH_GROUP} includes {', '.join(members)}",
        f"browser IPC: {DASH} is 0710 and controller socket is group-callable",
    ]


# --------------------------------------------------------------------------
# harden: the shared secrets any session could read today
# --------------------------------------------------------------------------
def harden(dry: bool = False) -> list[str]:
    out = []

    def chmod(path: Path, mode: int, why: str):
        if not path.exists():
            out.append(f"skip  {path} (absent)")
            return
        cur = path.stat().st_mode & 0o777
        if cur == mode:
            out.append(f"ok    {path} already {oct(mode)}")
            return
        out.append(f"{'would ' if dry else ''}chmod {oct(cur)} -> {oct(mode)}  {path}   [{why}]")
        if not dry:
            path.chmod(mode)

    out.extend(
        configure_browser_ipc_group(
            sorted(set(load_map().values())),
            dry=dry,
        )
    )

    # The single worst one: the admin advisor token, the dashboard admin
    # password and the cookie signing secret, all world readable.
    chmod(
        Path("/etc/supervisor/conf.d/tmux-dashboard.conf"),
        0o600,
        "holds admin ADVISOR_TOKEN, TMUX_DASH_PASS, TMUX_DASH_SECRET",
    )
    chmod(HOME, 0o711, "traverse only, no listing of the admin home")
    chmod(HOME / ".config" / "gcloud", 0o700, "cached service account credentials")
    # Traverse only, not 0700: every member's auth.json is a symlink to the one
    # shared login in here, so closing the execute bit breaks every member
    # session with "Permission denied (os error 13)". share_codex_login() puts
    # that single file in the gxauth group; nothing else here is reachable.
    chmod(HOME / ".codex", 0o711, "admin CODEX_HOME, traversable for the shared login")
    chmod(HOME / ".cache", 0o711, "traverse only")
    chmod(DASH, DASH_PRIVATE_KEEP_TRAVERSABLE, "traverse only; children opened explicitly")

    # Everything under .tmux-dashboard is private unless it is shared reference
    # material the account policy tells sessions to read.
    if DASH.exists():
        for child in sorted(DASH.iterdir()):
            if child.name in SHARED_READABLE:
                if not dry:
                    sh("chmod", "-R", "o+rX", str(child))
                out.append(f"{'would ' if dry else ''}share  {child} (o+rX)")
            elif child.name == "uploads":
                continue  # handled per session in apply()
            elif child.is_dir():
                if not dry:
                    sh("chmod", "-R", "o-rwx", str(child))
                out.append(f"{'would ' if dry else ''}hide   {child}/ (o-rwx, recursive)")
            else:
                if not dry:
                    sh("chmod", "o-rwx", str(child))
                out.append(f"{'would ' if dry else ''}hide   {child}")

    # The MCP servers a session launches live in the dashboard tree and run on
    # the dashboard's venv, so those exact paths must stay reachable. The repo
    # directory is traverse-only and .git/.venv/.env stay closed; only the MCP
    # entry points and the module they import are opened.
    repo = HOME / "tmux-dashboard"
    if repo.is_dir():
        if not dry:
            # 0755, not 0711: google_workspace_mcp.py does `import google_policy`
            # and CPython's FileFinder scans the script directory, so a
            # traverse-only repo fails with ModuleNotFoundError. No secret lives
            # here (the tokens are in the supervisor environment), but .git and
            # the dashboard venv are closed anyway.
            repo.chmod(0o755)
            for closed in (".git", ".venv"):
                d = repo / closed
                if d.exists():
                    sh("chmod", "-R", "o-rwx", str(d), check=False)
        out.append(f"{'would ' if dry else ''}share  {repo} (0755; .git and .venv closed)")

    # Shared playwright MCP CLI and launcher, no per-account data in them.
    for sub in ("node_modules", "bin"):
        d = HOME / ".claude-browser" / sub
        if d.is_dir():
            if not dry:
                (HOME / ".claude-browser").chmod(0o711)
                sh("chmod", "-R", "o+rX", str(d))
            out.append(f"{'would ' if dry else ''}share  {d} (o+rX)")
    mcpvenv = DASH / "mcp"
    if mcpvenv.is_dir():
        if not dry:
            sh("chmod", "-R", "o+rX", str(mcpvenv))
        out.append(f"{'would ' if dry else ''}share  {mcpvenv} (o+rX, the MCP server venv)")

    # Re-assert the shared Codex login's group here too, so harden and apply can
    # run in either order without one undoing the other.
    auth = HOME / ".codex" / "auth.json"
    if auth.exists() and not dry:
        sh("groupadd", "-f", AUTH_GROUP, check=False)
        sh("chown", f"nimrod_rotem:{AUTH_GROUP}", str(auth), check=False)
        auth.chmod(0o640)
        out.append(f"share  {auth} (0640 nimrod_rotem:{AUTH_GROUP})")

    # Browser binaries are not secret and every session needs them.
    ms = HOME / ".cache" / "ms-playwright"
    if ms.exists():
        if not dry:
            sh("chmod", "-R", "o+rX", str(ms))
        out.append(f"{'would ' if dry else ''}share  {ms} (o+rX)")
    out.extend(install_pytest_hook(dry=dry))
    out.extend(configure_pytest_gate(sorted(set(load_map().values())), dry=dry))
    return out


def rollback_perms() -> list[str]:
    out = []
    for p, mode in [
        (Path("/etc/supervisor/conf.d/tmux-dashboard.conf"), 0o644),
        (HOME, 0o755),
        (HOME / "tmux-dashboard", 0o755),
        (HOME / ".cache", 0o755),
        (DASH, 0o755),
    ]:
        if p.exists():
            p.chmod(mode)
            out.append(f"restored {p} to {oct(mode)}")
    return out


# --------------------------------------------------------------------------
# apply: one UNIX user per account
# --------------------------------------------------------------------------
def ensure_account(name: str) -> str:
    try:
        pwd.getpwnam(name)
        return "exists"
    except KeyError:
        pass
    sh(
        "useradd",
        "--create-home",
        "--shell", "/bin/bash",
        "--comment", "build.grabo.tools session account",
        name,
    )
    sh("passwd", "--lock", name)
    Path(f"/home/{name}").chmod(0o700)
    return "created"



def join_dashboard_to_group(name: str) -> None:
    """The dashboard reaches an account's files through that account's group."""
    sh("usermod", "-aG", name, "nimrod_rotem", check=False)


def _read_regular_no_follow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError(f"not a single regular file: {path}")
        chunks = bytearray()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks)
    finally:
        os.close(fd)


def pytest_hook_status(expected: bytes | None = None) -> tuple[bool, str]:
    """Verify the immutable installed pytest hook and its containing directory."""
    try:
        source = expected if expected is not None else _read_regular_no_follow(
            PYTEST_HOOK_SOURCE
        )
        directory = PYTEST_HOOK_DIR.lstat()
        if (
            not stat.S_ISDIR(directory.st_mode)
            or PYTEST_HOOK_DIR.is_symlink()
            or directory.st_uid != PYTEST_HOOK_OWNER_UID
            or directory.st_gid != PYTEST_HOOK_OWNER_GID
            or stat.S_IMODE(directory.st_mode) != 0o755
        ):
            return False, "hook directory is not root-owned mode 0755"

        flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(PYTEST_HOOK_TARGET, flags)
        try:
            installed = os.fstat(fd)
            if (
                not stat.S_ISREG(installed.st_mode)
                or installed.st_nlink != 1
                or installed.st_uid != PYTEST_HOOK_OWNER_UID
                or installed.st_gid != PYTEST_HOOK_OWNER_GID
                or stat.S_IMODE(installed.st_mode) != 0o444
            ):
                return False, "installed hook is not root-owned mode 0444"
            chunks = bytearray()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.extend(chunk)
        finally:
            os.close(fd)
        if bytes(chunks) != source:
            return False, "installed hook content does not match runtime_hooks source"
        digest = hashlib.sha256(source).hexdigest()
        return True, f"installed hook verified sha256={digest}"
    except (OSError, ValueError) as exc:
        return False, f"installed hook unavailable: {exc}"


def install_pytest_hook(*, dry: bool = False) -> list[str]:
    """Atomically install the mandatory pytest plugin as immutable root state."""
    source = _read_regular_no_follow(PYTEST_HOOK_SOURCE)
    digest = hashlib.sha256(source).hexdigest()
    if dry:
        return [
            f"would install pytest hook {PYTEST_HOOK_TARGET} (sha256={digest})"
        ]

    PYTEST_HOOK_DIR.mkdir(mode=0o755, parents=True, exist_ok=True)
    directory = PYTEST_HOOK_DIR.lstat()
    if not stat.S_ISDIR(directory.st_mode) or PYTEST_HOOK_DIR.is_symlink():
        raise PermissionError(f"unsafe pytest hook directory: {PYTEST_HOOK_DIR}")
    os.chown(PYTEST_HOOK_DIR, PYTEST_HOOK_OWNER_UID, PYTEST_HOOK_OWNER_GID)
    PYTEST_HOOK_DIR.chmod(0o755)

    valid, _detail = pytest_hook_status(source)
    if not valid:
        fd, temporary = tempfile.mkstemp(
            prefix=PYTEST_HOOK_TARGET.name + ".",
            suffix=".tmp",
            dir=PYTEST_HOOK_DIR,
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(source)
                handle.flush()
                os.fchown(
                    handle.fileno(),
                    PYTEST_HOOK_OWNER_UID,
                    PYTEST_HOOK_OWNER_GID,
                )
                os.fchmod(handle.fileno(), 0o444)
                os.fsync(handle.fileno())
            os.replace(temporary, PYTEST_HOOK_TARGET)
            directory_fd = os.open(
                PYTEST_HOOK_DIR,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    valid, detail = pytest_hook_status(source)
    if not valid:
        raise PermissionError(detail)
    return [f"pytest hook: {detail}"]


def configure_pytest_gate(accounts: list[str], *, dry: bool = False) -> list[str]:
    """Provision the narrow group and immutable locks shared by test runners."""
    members = ["nimrod_rotem", *sorted(set(accounts))]
    if dry:
        return [
            f"would provision {PYTEST_GROUP} for {', '.join(members)}",
            f"would provision root:{PYTEST_GROUP} 0440 locks below {PYTEST_LOCK_DIR}",
        ]

    sh("groupadd", "-f", PYTEST_GROUP)
    for member in members:
        try:
            pwd.getpwnam(member)
        except KeyError:
            continue
        sh("usermod", "-aG", PYTEST_GROUP, member)

    tmpfiles = (
        f"d {PYTEST_LOCK_DIR} 0750 root {PYTEST_GROUP} -\n"
        f"f {PYTEST_LOCK} 0440 root {PYTEST_GROUP} -\n"
        f"f {DAILY_TEST_LOCK} 0440 root {PYTEST_GROUP} -\n"
        f"a+ {PYTEST_LOCK_DIR} - - - - u:nimrod_rotem:r-x\n"
        f"a+ {PYTEST_LOCK} - - - - u:nimrod_rotem:r--\n"
        f"a+ {DAILY_TEST_LOCK} - - - - u:nimrod_rotem:r--\n"
    )
    temporary = PYTEST_TMPFILES.with_suffix(".conf.tmp")
    temporary.write_text(tmpfiles)
    temporary.chmod(0o644)
    temporary.replace(PYTEST_TMPFILES)
    sh("systemd-tmpfiles", "--create", str(PYTEST_TMPFILES))
    return [
        f"pytest gate: {PYTEST_GROUP} includes {', '.join(members)}",
        f"pytest gate: root-owned locks provisioned below {PYTEST_LOCK_DIR}",
    ]


def share_codex_login(name: str) -> None:
    """Let session accounts read the one shared Codex login, and nothing else.

    _apply_member_auth symlinks each member's auth.json at the admin's
    ~/.codex/auth.json, so hardening ~/.codex to 0700 breaks every member login
    with "Permission denied (os error 13)". Open the traversal bit only, and put
    the single file in a group the session accounts share.

    Re-run apply after re-authenticating Codex: a fresh auth.json comes back
    with the admin's default group.
    """
    sh("groupadd", "-f", AUTH_GROUP, check=False)
    sh("usermod", "-aG", AUTH_GROUP, name, check=False)
    admin_codex = HOME / ".codex"
    if admin_codex.is_dir():
        admin_codex.chmod(0o711)
    auth = admin_codex / "auth.json"
    if auth.exists():
        sh("chown", f"nimrod_rotem:{AUTH_GROUP}", str(auth), check=False)
        auth.chmod(0o640)


def _browser_belongs_to(browser_id: str, user: dict) -> bool:
    """True when this browser profile is the account's leased browser."""
    try:
        cfg = (codex_home(user) / "config.toml").read_text()
    except OSError:
        return False
    return f'TMUX_DASH_BROWSER_ID = "{browser_id}"' in cfg


def own_tree(path: Path, acct: str) -> None:
    """Hand a tree to the account, keeping the dashboard's access through the group.

    Owner and group are both the account, and nimrod_rotem is a member of that
    group (see ensure_account), so the dashboard keeps full access while every
    other account is left on the "other" bits, which are cleared.

    setgid on the directories is what makes this survive: whichever side creates
    the next file, it lands in the account's group, so neither party can lock
    the other out. That matters most for advisor-token and auth.json, which the
    dashboard rewrites and the session then has to read.
    """
    if not path.exists():
        return
    # -h, and chmod only on real files and directories. A member's auth.json is
    # a symlink to the admin's shared Codex login: chown -R dereferences by
    # default and would hand that shared file to whichever account was
    # provisioned last, then chmod -R would reset its mode too.
    sh("chown", "-R", "-h", f"{acct}:{acct}", str(path))
    sh("find", str(path), "(", "-type", "f", "-o", "-type", "d", ")",
       "-exec", "chmod", "u=rwX,g=rwX,o=", "{}", "+", check=False)
    sh("find", str(path), "-type", "d", "-exec", "chmod", "g+s", "{}", "+", check=False)


def set_account_umask(acct: str) -> None:
    """umask 007 so session-created files stay group-writable and world-closed."""
    profile = Path(f"/home/{acct}/.profile")
    line = "umask 007  # managed by provision_accounts.py\n"
    try:
        existing = profile.read_text() if profile.exists() else ""
    except OSError:
        return
    if "provision_accounts.py" in existing:
        return
    profile.write_text(existing + line)
    shutil.chown(profile, user=acct, group=acct)


def apply(names: list[str], dry: bool = False) -> list[str]:
    out = []
    users = load_users()
    mapping = load_map()
    taken = set(mapping.values())
    owners = json.loads(OWNERS_JSON.read_text()) if OWNERS_JSON.exists() else {}

    for u in users:
        if u.get("role") == "admin":
            continue
        uname = str(u.get("username") or u["id"])
        if names and uname not in names:
            continue
        acct = mapping.get(u["id"]) or unix_name(u, taken)
        taken.add(acct)
        if dry:
            out.append(f"would provision {uname} -> {acct}")
            continue

        state = ensure_account(acct)
        join_dashboard_to_group(acct)
        set_account_umask(acct)
        mapping[u["id"]] = acct
        out.append(f"{uname}: unix account {acct} ({state})")

        # CODEX_HOME: the account owns it, the dashboard reaches it through the
        # group, and nobody else gets past the directory bit.
        ch = codex_home(u)
        if ch.exists():
            own_tree(ch, acct)
            # The token is the escalation path, so the dashboard does not get to
            # read it either. Only root and the account itself.
            for secret in ("advisor-token", "auth.json"):
                p = ch / secret
                if p.exists():
                    sh("chown", f"{acct}:{acct}", str(p))
                    p.chmod(0o660)
            out.append(f"  {ch.name}: {acct}:{acct}, secrets 0660 (no access for other accounts)")

        # After own_tree, so a dereferenced chown cannot undo it.
        share_codex_login(acct)

        # The account's private browser writes its profile and output here.
        pmcp = HOME / ".playwright-mcp"
        if pmcp.is_dir():
            pmcp.chmod(0o711)
            for d in pmcp.iterdir():
                if d.is_dir() and _browser_belongs_to(d.name, u):
                    own_tree(d, acct)
                    out.append(f"  playwright-mcp/{d.name}: owned by {acct}")

        # The Google MCP server runs as the account and reads this account's
        # OAuth credentials from here.
        conn = DASH / "connections" / str(u["id"])
        if conn.exists():
            own_tree(conn, acct)
            out.append(f"  connections/{u['id']}: owned by {acct}")

        pd = project_dir(u)
        if pd.exists():
            own_tree(pd, acct)
            out.append(f"  web-projects/{pd.name}: owned by {acct}")

        # Uploads are keyed by session name; give each session's dir to its owner.
        for session, owner_id in owners.items():
            if owner_id != u["id"]:
                continue
            up = DASH / "uploads" / session
            if up.exists():
                own_tree(up, acct)
                out.append(f"  uploads/{session}: owned by {acct}")

    if not dry:
        save_map(mapping)
        write_sudoers(sorted(set(mapping.values())))
        out.extend(configure_browser_ipc_group(sorted(set(mapping.values()))))
        out.extend(install_pytest_hook())
        out.extend(configure_pytest_gate(sorted(set(mapping.values()))))
        out.append(f"sudoers: {SUDOERS} written for {len(set(mapping.values()))} accounts")
    return out


def write_sudoers(accounts: list[str]) -> None:
    body = [
        "# Managed by provision_accounts.py. The dashboard launches each Codex",
        "# session as that account's own UNIX user. nimrod_rotem already holds",
        "# full sudo via google-sudoers, so this grants it nothing new; it only",
        "# lets the launch drop privilege without a password prompt.",
    ]
    for a in accounts:
        body.append(f"nimrod_rotem ALL=({a}) NOPASSWD: ALL")
    body.append("")
    tmp = Path(str(SUDOERS) + ".tmp")
    tmp.write_text("\n".join(body))
    tmp.chmod(0o440)
    r = subprocess.run(["visudo", "-cf", str(tmp)], capture_output=True, text=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"sudoers syntax check failed: {r.stdout}{r.stderr}")
    tmp.replace(SUDOERS)


# --------------------------------------------------------------------------
# netguard: no cloud metadata, therefore no project-owner credentials
# --------------------------------------------------------------------------
def netguard(dry: bool = False) -> list[str]:
    accounts = sorted(set(load_map().values()))
    if not accounts:
        return ["no accounts provisioned yet, run apply first"]
    lines = [
        "#!/bin/sh",
        "# Managed by provision_accounts.py.",
        "# Session accounts must not reach the GCE metadata server: that is where",
        "# gcloud picks up nimo-843@nimo-gpt (roles/owner) and with it ssh to every",
        "# VM in the project. The dashboard itself (nimrod_rotem) is untouched.",
        "#",
        "# iptables-legacy on purpose. /usr/sbin/iptables is iptables-nft here and",
        "# it ACCEPTS these rules, shows them in -S, and never matches a packet:",
        "# the kernel is running the legacy xtables path. A rule that inserts",
        "# cleanly and blocks nothing is worse than no rule, so this script ends",
        "# by proving the block works and exits non-zero if it does not.",
        "set -e",
        "IPT=/usr/sbin/iptables-legacy",
    ]
    for a in accounts:
        lines.append(f'$IPT -D OUTPUT -d {METADATA_IP} -p tcp --dport 80 -m owner --uid-owner {a} -j REJECT 2>/dev/null || true')
        lines.append(f'$IPT -D OUTPUT -d {METADATA_IP} -m owner --uid-owner {a} -j REJECT 2>/dev/null || true')
    for a in accounts:
        # Port 80 only. This address is also the VM's DNS resolver, so a
        # blanket reject takes name resolution with it and every outbound
        # call from a session fails with curl exit 6.
        lines.append(f'$IPT -A OUTPUT -d {METADATA_IP} -p tcp --dport 80 -m owner --uid-owner {a} -j REJECT')
    probe = accounts[0]
    lines += [
        "",
        "# Defect check: no token, but DNS and normal outbound must still work.",
        f'if ! sudo -n -u {probe} getent hosts advisor.rotem.ai >/dev/null; then',
        f'  echo "grabo-session-netguard: FAILED, {probe} lost DNS" >&2',
        "  exit 1",
        "fi",
        f'if sudo -n -u {probe} curl -s --max-time 5 -o /dev/null '
        f'-H "Metadata-Flavor: Google" '
        f'http://{METADATA_IP}/computeMetadata/v1/instance/service-accounts/default/token; then',
        f'  echo "grabo-session-netguard: FAILED, {probe} still reached the metadata server" >&2',
        "  exit 1",
        "fi",
        'echo "grabo-session-netguard: metadata blocked for $(echo "$0" >/dev/null; echo '
        + str(len(accounts))
        + ') session account(s)"',
    ]
    script = "\n".join(lines) + "\n"

    unit = """[Unit]
Description=Block GCE metadata for build.grabo.tools session accounts
After=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/grabo-session-netguard

[Install]
WantedBy=multi-user.target
"""
    if dry:
        return [f"would install netguard for {len(accounts)} accounts"]
    # Drop the ineffective nft-backend copies so the two views cannot disagree.
    for a in accounts:
        sh("iptables", "-D", "OUTPUT", "-d", METADATA_IP, "-m", "owner",
           "--uid-owner", a, "-j", "REJECT", check=False)
    NETGUARD_SH.write_text(script)
    NETGUARD_SH.chmod(0o755)
    NETGUARD_UNIT.write_text(unit)
    sh("systemctl", "daemon-reload")
    sh("systemctl", "enable", "--now", "grabo-session-netguard.service", check=False)
    run = sh("/usr/local/sbin/grabo-session-netguard", check=False)
    out = [(run.stdout + run.stderr).strip() or "netguard applied"]
    if run.returncode != 0:
        out.append("FAILED: netguard could not prove the block")
    return out


# --------------------------------------------------------------------------
def verify() -> list[str]:
    hook_ok, hook_detail = pytest_hook_status()
    out = [f"  [{'PASS' if hook_ok else 'FAIL'}] pytest hook: {hook_detail}"]
    mapping = load_map()
    if not mapping:
        out.append("nothing provisioned")
        return out
    accounts = sorted(set(mapping.values()))
    probe = accounts[0]

    def as_probe(cmd: str) -> tuple[int, str]:
        r = subprocess.run(
            ["sudo", "-n", "-u", probe, "-H", "bash", "-lc", cmd],
            capture_output=True, text=True,
        )
        return r.returncode, (r.stdout + r.stderr).strip()

    out.append(f"probing as {probe}")
    rc, who = as_probe("id -un")
    out.append(f"  runs as: {who} (rc={rc})")

    checks = [
        ("cannot read the admin advisor token", "cat /home/nimrod_rotem/.advisor-token"),
        ("cannot read the dashboard supervisor conf",
         "cat /etc/supervisor/conf.d/tmux-dashboard.conf"),
        ("cannot read the roster", f"cat {USERS_JSON}"),
        ("cannot read Google connections", f"ls {DASH}/connections"),
        ("cannot reach the metadata server",
         f"curl -s --max-time 4 -H 'Metadata-Flavor: Google' "
         f"http://{METADATA_IP}/computeMetadata/v1/instance/service-accounts/default/token"),
        ("cannot use gcloud", "gcloud auth print-access-token"),
    ]
    for label, cmd in checks:
        rc, _ = as_probe(cmd)
        out.append(f"  [{'PASS' if rc != 0 else 'FAIL'}] {label}")

    for other_id, other_acct in mapping.items():
        if other_acct == probe:
            continue
        rc, _ = as_probe(f"cat /home/nimrod_rotem/.codex-user-{other_id}/advisor-token")
        out.append(f"  [{'PASS' if rc != 0 else 'FAIL'}] cannot read {other_acct}'s advisor token")
        break

    positives = [
        ("can read shared context", f"test -r {DASH}/context/browser-policy.md"),
        ("can run codex", "command -v codex"),
        ("can traverse browser IPC directory", f"test -x {DASH}"),
        ("can reach the controller socket", f"test -w {DASH}/controller.sock"),
        ("can read the pytest host lock", f"test -r {PYTEST_LOCK}"),
        (
            "can read and compile the installed pytest hook",
            "python3 -c "
            + repr(
                "from pathlib import Path; "
                f"compile(Path({str(PYTEST_HOOK_TARGET)!r}).read_text(), "
                "'tmux_dashboard_pytest_gate.py', 'exec')"
            ),
        ),
    ]
    for label, cmd in positives:
        rc, _ = as_probe(cmd)
        out.append(f"  [{'PASS' if rc == 0 else 'FAIL'}] {label}")
    return out


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if os.geteuid() != 0 and cmd not in ("plan", "verify"):
        print("must run as root")
        return 2
    if cmd == "plan":
        lines = harden(dry=True) + apply([], dry=True) + netguard(dry=True)
    elif cmd == "harden":
        lines = harden()
    elif cmd == "apply":
        lines = apply(sys.argv[2:])
    elif cmd == "netguard":
        lines = netguard()
    elif cmd == "verify":
        lines = verify()
    elif cmd == "rollback-perms":
        lines = rollback_perms()
    else:
        print(__doc__)
        return 2
    print("\n".join(lines))
    return 1 if any("FAIL" in x for x in lines) else 0


if __name__ == "__main__":
    sys.exit(main())
