"""Renew an idle Claude login's access token, so its meter never reads blank.

A login is two tokens: an access token that lapses about 8 hours after it was
issued, and a refresh token the CLI swaps for a new one on its next run. An
account nothing has run on for 8 hours keeps a perfectly good login (`claude
auth status` says loggedIn, the refresh token lasts weeks) but its access token
is dead, so usage.get_usage cannot read its meter. The account rows then show no
meter and "Usage unknown", which reads as "signed out": the owner signed account
2 in again and again (2026-11-11) while it was never signed out at all.

So when the usage poll finds an expired access token beside a refresh token,
it calls kick(), and a `claude -p /cost` runs under that account's
CLAUDE_CONFIG_DIR. `/cost` is a local command: no model call, no quota, and it
still makes the CLI renew the token on start-up (measured: $0, 0 API ms, the
Keychain item's expiresAt moved 28 hours on). The CLI does the renewal itself,
under its own storage lock, which is the point: refreshing the token from here
would rotate the refresh token behind the back of any claude already running on
that login, and logging the account out for real is exactly what
bridge/credentials.py's lock exists to prevent.

--no-session-persistence keeps it out of ~/.claude/projects, and it runs in a
temp dir outside BASE_PATH, so no session list ever sees it. One renewal per
login at a time, at most one per RETRY seconds. A disabled account is left
alone. Stdlib only; the fork's own module.
"""

import os
import subprocess
import tempfile
import threading
import time

RETRY = 600.0          # seconds between two renewal attempts for one login
TIMEOUT = 90           # a renewal is a CLI start-up; past this something is wrong

_lock = threading.Lock()
_last: "dict[int, float]" = {}
_busy: "set[int]" = set()


def slot_for(creds_path: str) -> "int | None":
    """The account slot a credentials path belongs to, or None."""
    from bridge import accounts
    home = os.path.dirname(creds_path)
    if os.path.realpath(home) == os.path.realpath(accounts.CLAUDE_HOME):
        return accounts.DEFAULT_SLOT
    parent, name = os.path.split(home)
    if name.isdigit() and os.path.realpath(parent) == os.path.realpath(accounts.ROOT):
        return int(name)
    return None


def _renew(slot: int) -> None:
    from bridge import accounts, usage
    try:
        env = {**os.environ, **accounts.env_for(slot)}
        if slot == accounts.DEFAULT_SLOT:
            env.pop("CLAUDE_CONFIG_DIR", None)
        subprocess.run(["claude", "-p", "/cost", "--no-session-persistence", "--output-format", "json"],
                       cwd=tempfile.gettempdir(), env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=TIMEOUT)
        with usage._lock:     # read the meter on the next poll, not after the retry wait
            usage._entry(accounts.credentials_path(slot))["next_try"] = 0.0
    except Exception as e:  # noqa: BLE001 - a renewal that fails just leaves the meter blank
        print(f"[login_keepalive] slot {slot}: {type(e).__name__}: {e}")
    finally:
        with _lock:
            _busy.discard(slot)


def kick(creds_path: str) -> bool:
    """Renew this login's access token in the background if it is worth trying.
    Returns whether a renewal started."""
    try:
        from bridge import accounts
        slot = slot_for(creds_path)
        if slot is None:
            return False
        if any(a["slot"] == slot and a.get("disabled") for a in accounts.list_accounts()):
            return False
    except Exception:  # noqa: BLE001
        return False
    now = time.monotonic()
    with _lock:
        if slot in _busy or now - _last.get(slot, -RETRY) < RETRY:
            return False
        _busy.add(slot)
        _last[slot] = now
    threading.Thread(target=_renew, args=(slot,), daemon=True, name=f"login-keepalive-{slot}").start()
    return True
