"""The Orven plugin (plugins/orven): key handling through openssl, the IP watcher's decisions,
and its routes through the plugin door."""

import base64
import importlib.util
import json
import os
import subprocess
import tempfile
import time

import pytest

from bridge import plugins_ext

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(HERE, "plugins", "orven")


def _mod():
    spec = importlib.util.spec_from_file_location("orven_plugin_under_test", os.path.join(PLUGIN_DIR, "plugin.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _openssl_ok(binary: str) -> bool:
    try:
        out = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.split()[1][0] in "3456789"


def test_raw_pubkey_is_the_last_32_bytes_of_spki():
    m = _mod()
    der = bytes(range(12)) + bytes(range(32))
    assert m.raw_pubkey_from_der(der) == bytes(range(32))
    with pytest.raises(ValueError):
        m.raw_pubkey_from_der(b"short")


def test_key_round_trip_through_openssl():
    m = _mod()
    binary = m.Keys.find_openssl()
    if not _openssl_ok(binary):
        pytest.skip("no OpenSSL 3+ on this machine")
    keys = m.Keys(os.path.join(tempfile.mkdtemp(), "device.pem"), binary)
    pub = keys.ensure()
    assert len(base64.urlsafe_b64decode(pub + "==")) == 32 and "=" not in pub
    assert oct(os.stat(keys.path).st_mode & 0o777) == "0o600"
    sig = keys.sign("nonce-abc")
    raw = base64.b64decode(sig)
    assert len(raw) == 64
    # the panel's check (gate-crypto.ts verifyEd25519) is openssl's: verify with the public key
    pubpem = subprocess.run([binary, "pkey", "-in", keys.path, "-pubout"], capture_output=True, check=True).stdout
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "pub.pem"), "wb") as f:
        f.write(pubpem)
    with open(os.path.join(d, "sig"), "wb") as f:
        f.write(raw)
    with open(os.path.join(d, "msg"), "wb") as f:
        f.write(b"nonce-abc")
    v = subprocess.run([binary, "pkeyutl", "-verify", "-pubin", "-inkey", os.path.join(d, "pub.pem"), "-rawin", "-in", os.path.join(d, "msg"), "-sigfile", os.path.join(d, "sig")],
                       capture_output=True, text=False)
    assert v.returncode == 0 and b"Verified" in v.stdout


def test_public_ip_needs_two_providers_to_agree():
    m = _mod()
    answers = {"https://api.ipify.org": "1.2.3.4", "https://ifconfig.me/ip": "1.2.3.4"}
    assert m.public_ip(lambda u: answers[u]) == "1.2.3.4"
    answers["https://ifconfig.me/ip"] = "5.6.7.8"
    assert m.public_ip(lambda u: answers[u]) is None                       # disagreement → skip the tick
    assert m.public_ip(lambda u: (_ for _ in ()).throw(OSError("down"))) is None
    assert m.public_ip(lambda u: "not an ip") is None


class FakeCtx:
    def __init__(self, data_dir, settings=None):
        self.data_dir = data_dir
        self._s = settings or {}
        self.logs, self.notes = [], []
    def settings(self):
        return self._s
    def log(self, msg):
        self.logs.append(msg)
    def notify(self, text):
        self.notes.append(text)


def _plugin_with_fake_gate(m, unlock_result=None, fail=None):
    p = m.Plugin()
    p.ctx = FakeCtx(tempfile.mkdtemp(), {"ORVEN_PANEL_URL": "https://gate.test"})
    p._reconfigure()
    p.gate.state = {"deviceId": "dev1", "ip": "1.1.1.1", "lastUnlockAt": time.time() - 100,
                    "ipExpiresAt": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 7200))}
    calls = []
    def unlock():
        calls.append(1)
        if fail:
            raise RuntimeError(fail)
        p.gate.state.update({"ip": p.last["ip"], "lastUnlockAt": time.time(), "ipExpiresAt": "2099-01-01T00:00:00+00:00"})
        return unlock_result or {"ip": p.last["ip"], "ipExpiresAt": "2099-01-01T00:00:00+00:00"}
    p.gate.unlock = unlock
    return p, calls


def test_watcher_unlocks_only_on_a_new_ip_or_a_lapsing_allowance():
    m = _mod()
    p, calls = _plugin_with_fake_gate(m)
    assert p.tick(ip="1.1.1.1") == "same" and calls == []
    assert p.tick(ip="2.2.2.2") == "unlocked" and len(calls) == 1          # the IP moved
    assert p.tick(ip="2.2.2.2") == "same" and len(calls) == 1
    p.gate.state["ipExpiresAt"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 60))
    assert p.tick(ip="2.2.2.2") == "unlocked" and len(calls) == 2          # about to lapse
    assert p.tick(ip=None) == "skip" and len(calls) == 2                   # providers disagreed


def test_a_refusal_notifies_once():
    m = _mod()
    p, calls = _plugin_with_fake_gate(m, fail="unlock refused (401)")
    assert p.tick(ip="9.9.9.9") == "failed"
    assert p.tick(ip="9.9.9.9") == "failed"
    assert len(p.ctx.notes) == 1 and "9.9.9.9" in p.ctx.notes[0]
    assert p.status()["ok"] is False


def test_not_enrolled_is_quiet():
    m = _mod()
    p = m.Plugin()
    p.ctx = FakeCtx(tempfile.mkdtemp(), {})
    p._reconfigure()
    assert p.tick(ip="1.1.1.1") == "not-enrolled"
    assert p.status() == {"line": "not enrolled", "ok": True}
    assert "not enrolled" in p.telegram()["/orven"](555, "")


def test_routes_through_the_plugin_door(monkeypatch):
    """The real manifest and entry, loaded by the loader; /state answers, /enroll refuses without a login."""
    monkeypatch.setattr(plugins_ext, "_ext_dir", lambda: tempfile.mkdtemp())
    dirs = {}
    monkeypatch.setattr(plugins_ext, "data_dir", lambda pid: dirs.setdefault(pid, tempfile.mkdtemp()))
    monkeypatch.setenv("MYSTICAL_PLUGINS_BUNDLED", "1")   # this test wants the shipped manifest
    plugins_ext._registry = None
    plugins_ext._live.clear()
    plugins_ext._broken.clear()
    try:
        assert plugins_ext.manifest("orven")["pane"]["title"] == "ORVEN"
        assert plugins_ext.start("orven"), plugins_ext.broken("orven")
        body, code = plugins_ext.dispatch("GET", "/plugin/orven/state", {}, None, 555)
        assert code == 200 and body["enrolled"] is False and body["panel"] == "https://console.orven.am"
        body, code = plugins_ext.dispatch("POST", "/plugin/orven/enroll", {}, {}, 555)
        assert code == 400 and "email" in body["error"]
        assert "orven" in plugins_ext.manifest("orven")["telegram"]
        assert plugins_ext.asset("/plugin/orven/ui/pane.js")[1].startswith("text/javascript")
    finally:
        plugins_ext.stop_all()
        plugins_ext._registry = None
