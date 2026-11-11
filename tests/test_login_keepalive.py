"""bridge/login_keepalive.py: an idle login's expired access token is renewed by the CLI itself."""

import os
import threading

from bridge import accounts, login_keepalive, usage


def test_slot_for_maps_credentials_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "ROOT", str(tmp_path / "accounts"))
    monkeypatch.setattr(accounts, "CLAUDE_HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "accounts" / "2")
    os.makedirs(tmp_path / "home")
    assert login_keepalive.slot_for(accounts.credentials_path(1)) == 1
    assert login_keepalive.slot_for(accounts.credentials_path(2)) == 2
    assert login_keepalive.slot_for(str(tmp_path / "elsewhere" / ".credentials.json")) is None


def test_kick_runs_the_cli_once_per_window_and_skips_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(accounts, "ROOT", str(tmp_path / "accounts"))
    monkeypatch.setattr(accounts, "CLAUDE_HOME", str(tmp_path / "home"))
    os.makedirs(tmp_path / "accounts" / "2")
    os.makedirs(tmp_path / "accounts" / "3")
    os.makedirs(tmp_path / "home")
    monkeypatch.setattr(accounts, "list_accounts", lambda: [{"slot": 2, "disabled": False}, {"slot": 3, "disabled": True}])
    monkeypatch.setattr(accounts, "env_for", lambda slot: {"CLAUDE_CONFIG_DIR": accounts.profile_dir(slot)})
    monkeypatch.setattr(login_keepalive, "_last", {})
    ran, done = [], threading.Event()

    def fake_run(cmd, **kw):
        ran.append((cmd, kw["env"].get("CLAUDE_CONFIG_DIR"), kw["cwd"]))
        done.set()
    monkeypatch.setattr(login_keepalive.subprocess, "run", fake_run)
    path = accounts.credentials_path(2)
    with usage._lock:
        usage._entry(path)["next_try"] = 1e18
    assert login_keepalive.kick(path) is True
    assert done.wait(5)
    for _ in range(100):
        if 2 not in login_keepalive._busy:
            break
        threading.Event().wait(0.01)
    cmd, cfg, cwd = ran[0]
    assert cmd[:3] == ["claude", "-p", "/cost"] and "--no-session-persistence" in cmd
    assert cfg == accounts.profile_dir(2) and not cwd.startswith(str(tmp_path))
    assert usage._entry(path)["next_try"] == 0.0                     # the meter is read again next poll
    assert login_keepalive.kick(path) is False                       # once per window
    assert login_keepalive.kick(accounts.credentials_path(3)) is False   # a disabled account is left alone
    assert len(ran) == 1


def test_get_usage_kicks_only_an_expired_login_that_can_renew(monkeypatch):
    kicked = []
    monkeypatch.setattr(login_keepalive, "kick", lambda p: kicked.append(p) or True)
    monkeypatch.setattr(usage, "_fetch", lambda t: None)
    monkeypatch.setattr(usage, "_token", lambda p: None)
    monkeypatch.setattr(usage, "has_token", lambda p: True)
    usage._cache.pop("/x/expired", None)
    usage.get_usage("/x/expired")
    monkeypatch.setattr(usage, "has_token", lambda p: False)
    usage._cache.pop("/x/gone", None)
    usage.get_usage("/x/gone")
    assert kicked == ["/x/expired"]
