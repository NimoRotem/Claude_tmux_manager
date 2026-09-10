# Incident Response: Outlaw/Shellbot Compromise

**Context:** A cryptominer (Outlaw/Shellbot) was found and killed on this server. The binary was at `/tmp/.X23-unix/.rsync/c/kthreadadd64`, installed ~March 25 2026, running as orphaned UID 1006. The staging directory has been deleted and no persistence was found, but the entry vector is unknown. Your job is to investigate what happened and harden the system.

---

## Part 1: Forensics — Understand What the Attacker Did

### 1. Identify UID 1006

- Check `/etc/passwd-`, `/etc/shadow-`, and any backup passwd files for historical record of UID 1006.
- Search `/var/vibe-coder/users/` for directories owned by UID 1006.
- Run `find / -uid 1006 -ls 2>/dev/null` to find any remaining files they own.
- Check `/var/log/auth.log*` for any `useradd` or `adduser` calls that created this UID.

### 2. Trace the Entry Vector

- Grep `/var/log/auth.log*` and `/var/log/syslog*` around **March 24–26** for:
  - Successful SSH logins from unusual IPs
  - `useradd`/`adduser` calls
  - `su`/`sudo` usage by unusual accounts
- Check vibe-coder user directories at `/var/vibe-coder/users/*/` for:
  - Shell histories and `.bash_history`
  - Downloaded scripts or tarballs (`dota3.tar.gz`, etc.)
  - `curl`/`wget` commands in history
- Check if any vibe-coder sandbox process triggered the dropper.

### 3. Check for Lateral Movement

- Try to recover `scan.log` contents (it was in `/tmp/.X23-unix/` — check filesystem journal if possible).
- Inspect `~/.ssh/known_hosts` and `~/.ssh/authorized_keys` for **every** user on the system (root, nimrod_rotem, lapto, all vc-* users). Flag any keys you can't identify.
- Check `/var/log/auth.log*` for outbound SSH connections originating from this host.

### 4. Check for Privilege Escalation

- Run `last` and `lastb` to review login history.
- Check for modified setuid binaries: `find / -perm -4000 -newer /tmp -ls 2>/dev/null`
- Inspect `/etc/sudoers` and `/etc/sudoers.d/` for unexpected entries.
- Verify `/etc/ld.so.preload` is empty.
- Verify `/etc/ld.so.conf.d/` has no rogue entries.

### 5. Check for Additional Persistence

- Dump all user crontabs:
  ```bash
  for u in $(cut -d: -f1 /etc/passwd); do echo "==$u=="; crontab -l -u "$u" 2>/dev/null; done
  ```
- Check systemd user units in `~/.config/systemd/` for all users.
- Check `/etc/profile.d/`, `/etc/bash.bashrc`, and per-user `.bashrc`/`.profile` for injected commands.
- Check for deleted-but-running binaries: `ls -la /proc/*/exe 2>/dev/null | grep deleted`

---

## Part 2: Hardening — Prevent Reinfection

### 6. Lock Down SSH

In `/etc/ssh/sshd_config`, ensure:

```
PasswordAuthentication no
PermitRootLogin no
AllowUsers <list only the accounts that need SSH>
```

Restart sshd after changes. Install and enable `fail2ban` if not already present, with an SSH jail configured.

### 7. Clean Up Orphaned Accounts

- Remove UID 1006 if it still exists anywhere.
- Audit all accounts in `/etc/passwd` — disable or remove any that aren't needed.
- Lock unused accounts with `usermod -L <username>`.

### 8. Restrict /tmp Execution

If feasible, remount `/tmp` with `noexec,nosuid,nodev`:

```
# /etc/fstab entry:
tmpfs /tmp tmpfs defaults,noexec,nosuid,nodev 0 0
```

**Before doing this**, check whether vibe-coder or other services depend on executing binaries from `/tmp`. Document any conflicts.

### 9. Harden Vibe-Coder Sandboxes

Investigate `/var/vibe-coder/` and determine whether user sandboxes can:

- Execute arbitrary binaries
- Make outbound network connections
- Write to shared directories like `/tmp`

Document findings. If sandboxes aren't containerized or namespaced, flag this as a **critical gap**.

### 10. Rotate Secrets (manual — do from a separate machine)

Rotate all API keys listed in `CLAUDE.md` and any `.env` files, including OpenAI, Gemini, and Anthropic keys. Do this from a different machine, not this server.

### 11. Set Up Basic Monitoring

- Install `rkhunter` or `chkrootkit` and run a baseline scan.
- Add a cron job to alert on executables in suspicious locations:
  ```bash
  # Example: check every 15 minutes
  */15 * * * * find /tmp /var/tmp /dev/shm -name '.*' -type f -executable 2>/dev/null | logger -t dotfile-alert
  ```

---

## Expected Output

Produce a summary report containing:

1. **Entry vector** — confirmed or best theory with evidence
2. **Full list of attacker artifacts** found during forensics
3. **All hardening changes made** with before/after config
4. **Items needing manual human decision** (e.g., which accounts to keep, key rotation, vibe-coder architecture changes)
