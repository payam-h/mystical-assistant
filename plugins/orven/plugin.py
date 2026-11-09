"""Orven plugin: the console gate, from this machine (docs: ~/Desktop/pixie/docs/PLAN-MYSTICAL.md §4b).

What the Orven Mac app did and this replaces: it held an Ed25519 device key, and unlocking the
gate (POST /api/gate/challenge → sign the nonce → POST /api/gate/unlock) also allowed the
caller's current IP for a TTL. Its silent refresh is why a changed IP got unblocked by itself.
Here a worker thread does the same without a window: every ORVEN_IP_CHECK_SEC it reads the
public IP from two providers (they must agree, or the tick is skipped), and when the IP differs
from the one last allowed, or the allowance is about to lapse, it runs challenge → unlock from
the new IP and remembers the result. A refusal notifies the owner once, then waits for a change.

Why openssl and not a Python library: the bridge is stdlib-only, and hashlib has no Ed25519.
`openssl genpkey/pkey/pkeyutl` (OpenSSL 3, `-rawin`) does everything needed; the key stays in
the plugin's data dir, 0600. The raw 32-byte public key is the SPKI DER's last 32 bytes, sent
base64url without padding — the same JWK `x` shape the panel verifies (gate-crypto.ts).

Why an admin login lives in settings: enrolling a device and reading devices/allow-list are
JwtAuthGuard routes (a panel session), while challenge/unlock are public. The Mac app asked
for the password once and kept nothing; a headless plugin cannot ask, so the password is a
secret setting (0600) used only against ORVEN_PANEL_URL, and the orven_token it yields is
cached in memory until the panel says 401.

Stdlib only (subprocess to openssl).
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

IP_PROVIDERS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com")
_LOOKUP = object()   # tick(): "read the public IP yourself"
_IP = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def raw_pubkey_from_der(der: bytes) -> bytes:
    """An Ed25519 SubjectPublicKeyInfo is a 12-byte prefix and the 32-byte key."""
    if len(der) != 44:
        raise ValueError(f"unexpected SPKI length {len(der)}")
    return der[-32:]


class Keys:
    """The device key through openssl. `find_openssl()` picks a binary that can do Ed25519."""

    def __init__(self, path: str, openssl: str = ""):
        self.path = path
        self.openssl = openssl or self.find_openssl()

    @staticmethod
    def find_openssl() -> str:
        for cand in ("/opt/homebrew/opt/openssl@3/bin/openssl", "/opt/homebrew/bin/openssl", "/usr/local/opt/openssl@3/bin/openssl", "openssl"):
            try:
                out = subprocess.run([cand, "version"], capture_output=True, text=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if out.returncode == 0 and re.match(r"OpenSSL [3-9]", out.stdout):   # Ed25519 -rawin: 3.0+ (LibreSSL lacks it)
                return cand
        return "openssl"

    def ensure(self) -> str:
        """The public key (base64url raw), generating the key pair on first use."""
        if not os.path.isfile(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            r = subprocess.run([self.openssl, "genpkey", "-algorithm", "ed25519", "-out", self.path], capture_output=True, text=True, timeout=20)
            if r.returncode:
                raise RuntimeError(f"openssl genpkey failed: {r.stderr.strip()[:200]}")
            os.chmod(self.path, 0o600)
        return self.public()

    def public(self) -> str:
        r = subprocess.run([self.openssl, "pkey", "-in", self.path, "-pubout", "-outform", "DER"], capture_output=True, timeout=20)
        if r.returncode:
            raise RuntimeError(f"openssl pkey failed: {r.stderr.decode(errors='replace').strip()[:200]}")
        return _b64url(raw_pubkey_from_der(r.stdout))

    def sign(self, message: str) -> str:
        """Ed25519 over the UTF-8 message, standard base64 (the panel accepts both alphabets).
        pkeyutl -rawin wants a file it can size, not a pipe, so the nonce goes through a 0600 temp file."""
        import tempfile
        fd, msg_path = tempfile.mkstemp(prefix="orven-nonce-", dir=os.path.dirname(self.path))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(message.encode())
            r = subprocess.run([self.openssl, "pkeyutl", "-sign", "-inkey", self.path, "-rawin", "-in", msg_path], capture_output=True, timeout=20)
        finally:
            try:
                os.unlink(msg_path)
            except OSError:
                pass
        if r.returncode or len(r.stdout) != 64:
            raise RuntimeError(f"openssl pkeyutl failed: {r.stderr.decode(errors='replace').strip()[:200]}")
        return base64.b64encode(r.stdout).decode()


def http(url: str, body: dict | None = None, headers: dict | None = None, method: str | None = None, timeout: int = 20) -> tuple[int, dict | str, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            hdrs = dict(r.headers.items())
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        hdrs = dict(e.headers.items()) if e.headers else {}
        code = e.code
    try:
        parsed: dict | str = json.loads(raw) if raw else {}
    except ValueError:
        parsed = raw
    return code, parsed, hdrs


def public_ip(fetch: Callable[[str], str] | None = None) -> str | None:
    """The public IPv4, from two providers that agree; None when they don't (or can't)."""
    def default_fetch(url: str) -> str:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "mystical-orven"}), timeout=8) as r:
            return r.read().decode().strip()
    f = fetch or default_fetch
    seen: list[str] = []
    for url in IP_PROVIDERS:
        try:
            ip = f(url)
        except Exception:  # noqa: BLE001 - one provider down is why there are three
            continue
        if _IP.match(ip):
            seen.append(ip)
        if len(seen) >= 2:
            break
    if len(seen) >= 2 and seen[0] == seen[1]:
        return seen[0]
    return None


class Gate:
    """The panel's gate, for one device."""

    def __init__(self, base: str, keys: Keys, state_path: str, log: Callable[[str], None]):
        self.base = base.rstrip("/")
        self.keys = keys
        self.state_path = state_path
        self.log = log
        self.state = self._load()
        self._jwt: str | None = None

    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.state_path)

    # --- admin session (JwtAuthGuard routes) -------------------------------------------------
    def login(self, email: str, password: str) -> str:
        code, body, hdrs = http(f"{self.base}/api/auth/login", {"email": email, "password": password})
        if code != 200 and code != 201:
            raise RuntimeError("wrong email or password" if code == 401 else f"login failed ({code})")
        cookies = hdrs.get("Set-Cookie", "")
        m = re.search(r"orven_token=([^;]+)", cookies)
        if not m:
            raise RuntimeError("panel returned no session cookie")
        self._jwt = m.group(1)
        return self._jwt

    def admin(self, path: str, body: dict | None = None, method: str | None = None, creds: tuple[str, str] | None = None):
        """An admin call; logs in with `creds` when there is no session or it was refused once."""
        for attempt in (0, 1):
            if not self._jwt:
                if not creds or not creds[0]:
                    raise RuntimeError("admin email/password not set")
                self.login(*creds)
            # Behind the gate (nginx auth_request) an admin call needs the gate cookie too; once this device
            # has unlocked we have one. Before the first enrolment only an allowed IP gets through.
            cookie = f"orven_token={self._jwt}" + (f"; orven_gate={self.state['token']}" if self.state.get("token") else "")
            code, out, _ = http(f"{self.base}/api{path}", body, {"Cookie": cookie, **({"x-gate-token": self.state["token"]} if self.state.get("token") else {})}, method)
            if code in (401, 403) and attempt == 0:
                self._jwt = None
                continue
            if code >= 400:
                raise RuntimeError(f"{path}: {out.get('message') if isinstance(out, dict) else out} ({code})")
            return out
        raise RuntimeError("admin session refused")

    # --- device flow (public routes) ----------------------------------------------------------
    def enroll(self, name: str, creds: tuple[str, str]) -> dict:
        pub = self.keys.ensure()
        out = self.admin("/gate/devices", {"name": name, "platform": "other", "publicKey": pub}, creds=creds)
        self.state.update({"deviceId": out["deviceId"], "deviceName": name, "publicKey": pub, "enrolledAt": time.time()})
        self._save()
        return out

    def unlock(self) -> dict:
        dev = self.state.get("deviceId")
        if not dev:
            raise RuntimeError("not enrolled")
        code, ch, _ = http(f"{self.base}/api/gate/challenge", {"deviceId": dev})
        if code != 200 or not isinstance(ch, dict) or not ch.get("nonce"):
            raise RuntimeError(f"challenge refused ({code}): {ch if isinstance(ch, str) else ch.get('message')}")
        sig = self.keys.sign(ch["nonce"])
        code, out, _ = http(f"{self.base}/api/gate/unlock", {"deviceId": dev, "nonce": ch["nonce"], "signature": sig})
        if code != 200 or not isinstance(out, dict) or not out.get("token"):
            raise RuntimeError(f"unlock refused ({code}): {out if isinstance(out, str) else out.get('message')}")
        now = time.time()
        self.state.update({"lastUnlockAt": now, "ip": out.get("ip"), "ttlSec": out.get("ttlSec"),
                           "ipExpiresAt": out.get("ipExpiresAt"), "token": out["token"]})
        self._save()
        return out


class Plugin:
    """The worker, the routes, the pane's data, the Telegram command."""

    def __init__(self):
        self.ctx = None
        self.gate: Gate | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: dict = {"ip": None, "checkedAt": None, "error": None, "notified": False}

    # --- lifecycle ---------------------------------------------------------------------------
    def start(self, ctx) -> None:
        self.ctx = ctx
        self._reconfigure()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="orven-gate", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def settings_changed(self, _s: dict) -> None:
        self._reconfigure()

    def _reconfigure(self) -> None:
        s = self.ctx.settings()
        keys = Keys(os.path.join(self.ctx.data_dir, "device.pem"), s.get("ORVEN_OPENSSL", ""))
        self.gate = Gate(s.get("ORVEN_PANEL_URL") or "https://console.orven.am", keys,
                         os.path.join(self.ctx.data_dir, "gate.json"), self.ctx.log)

    def _creds(self) -> tuple[str, str]:
        s = self.ctx.settings()
        return (s.get("ORVEN_ADMIN_EMAIL", ""), s.get("ORVEN_ADMIN_PASSWORD", ""))

    # --- the watcher -------------------------------------------------------------------------
    def tick(self, now: float | None = None, ip: "str | None | object" = _LOOKUP) -> str:
        """One check. `ip` left out → read the public IP; None → the providers disagreed (skip).
        Returns what it did: 'skip', 'same', 'unlocked', 'failed', 'not-enrolled'."""
        now = now or time.time()
        if not self.gate or not self.gate.state.get("deviceId"):
            return "not-enrolled"
        if ip is _LOOKUP:
            ip = public_ip()
        self.last["checkedAt"] = now
        if not ip:
            return "skip"
        self.last["ip"] = ip
        st = self.gate.state
        exp = st.get("ipExpiresAt")
        exp_ts = _parse_ts(exp) if exp else 0
        lapsing = exp_ts and exp_ts - now < 600
        if st.get("ip") == ip and not lapsing and st.get("lastUnlockAt"):
            return "same"
        try:
            out = self.gate.unlock()
            self.last["error"] = None
            self.last["notified"] = False
            self.ctx.log(f"gate: allowed {out.get('ip') or ip} until {out.get('ipExpiresAt')}")
            return "unlocked"
        except Exception as e:  # noqa: BLE001 - a refusal is reported, not raised into the thread
            self.last["error"] = str(e)[:200]
            if not self.last["notified"]:
                self.last["notified"] = True
                self.ctx.notify(f"Orven gate: could not unlock from {ip} — {self.last['error']}")
            return "failed"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self.ctx.log(f"tick failed: {e}")
            try:
                every = max(30, int(self.ctx.settings().get("ORVEN_IP_CHECK_SEC") or 120))
            except ValueError:
                every = 120
            self._stop.wait(every)

    # --- routes ------------------------------------------------------------------------------
    def _view(self) -> dict:
        st = self.gate.state if self.gate else {}
        return {
            "panel": self.gate.base if self.gate else None,
            "enrolled": bool(st.get("deviceId")), "deviceId": st.get("deviceId"), "deviceName": st.get("deviceName"),
            "publicKey": st.get("publicKey") or self._pubkey_if_any(),
            "ip": self.last.get("ip"), "checkedAt": self.last.get("checkedAt"), "error": self.last.get("error"),
            "allowedIp": st.get("ip"), "allowedUntil": st.get("ipExpiresAt"), "lastUnlockAt": st.get("lastUnlockAt"),
            "adminConfigured": bool(self._creds()[0] and self._creds()[1]),
        }

    def _pubkey_if_any(self) -> "str | None":
        """The device public key once a key exists (so an owner can enrol it from the panel by hand)."""
        try:
            return self.gate.keys.public() if self.gate and os.path.isfile(self.gate.keys.path) else None
        except Exception:  # noqa: BLE001
            return None

    def routes(self) -> dict:
        return {
            ("GET", "/state"): lambda req: (self._view(), 200),
            ("POST", "/unlock"): self._r_unlock,
            ("POST", "/enroll"): self._r_enroll,
            ("GET", "/gate"): self._r_gate,
            ("POST", "/gate/settings"): self._r_gate_settings,
            ("POST", "/devices/revoke"): self._r_revoke,
            ("POST", "/forget"): self._r_forget,
            ("POST", "/keygen"): lambda req: ({"publicKey": self.gate.keys.ensure()}, 200),
            ("POST", "/adopt"): self._r_adopt,
        }

    def _r_unlock(self, req):
        what = self.tick(ip=public_ip())
        if what == "same":    # asked for it: do it anyway
            try:
                self.gate.unlock()
                what = "unlocked"
            except Exception as e:  # noqa: BLE001
                return {"error": str(e)[:200], **self._view()}, 502
        if what == "failed":
            return {"error": self.last.get("error"), **self._view()}, 502
        return {"did": what, **self._view()}, 200

    def _r_enroll(self, req):
        s = self.ctx.settings()
        name = (req["body"].get("name") or s.get("ORVEN_DEVICE_NAME") or "Mystical").strip()[:80]
        creds = (req["body"].get("email") or s.get("ORVEN_ADMIN_EMAIL", ""), req["body"].get("password") or s.get("ORVEN_ADMIN_PASSWORD", ""))
        if not creds[0] or not creds[1]:
            return {"error": "email and password are needed to enrol (set them in the card or send them once)"}, 400
        try:
            self.gate.enroll(name, creds)
            self.tick(ip=public_ip())
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}, 502
        return {"ok": True, **self._view()}, 200

    def _r_gate(self, req):
        try:
            settings = self.gate.admin("/gate/settings", creds=self._creds())
            devices = self.gate.admin("/gate/devices", creds=self._creds())
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}, 502
        return {"settings": settings, "devices": devices}, 200

    def _r_gate_settings(self, req):
        allowed = {"mode", "sessionTtlSec", "unlockIpTtlSec", "allowIps"}
        patch = {k: v for k, v in (req["body"] or {}).items() if k in allowed}
        if not patch:
            return {"error": "nothing to change"}, 400
        try:
            out = self.gate.admin("/gate/settings", patch, method="PATCH", creds=self._creds())
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}, 502
        return {"settings": out}, 200

    def _r_revoke(self, req):
        dev = str(req["body"].get("id") or "")
        if not dev:
            return {"error": "id"}, 400
        try:
            self.gate.admin(f"/gate/devices/{dev}/revoke", {}, creds=self._creds())
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)[:200]}, 502
        return {"ok": True}, 200

    def _r_adopt(self, req):
        """The device was enrolled by hand in the panel (its id pasted here): remember it and unlock."""
        dev = str(req["body"].get("deviceId") or "").strip()
        if not dev:
            return {"error": "deviceId"}, 400
        self.gate.state.update({"deviceId": dev, "deviceName": req["body"].get("name") or self.ctx.settings().get("ORVEN_DEVICE_NAME"), "publicKey": self.gate.keys.ensure(), "enrolledAt": time.time()})
        self.gate._save()
        return {"did": self.tick(), **self._view()}, 200

    def _r_forget(self, req):
        self.gate.state = {}
        self.gate._save()
        return {"ok": True, **self._view()}, 200

    # --- telegram & status -------------------------------------------------------------------
    def telegram(self) -> dict:
        def orven(chat_id: int, rest: str) -> str:
            if rest.strip() == "unlock":
                body, code = self._r_unlock({"body": {}})
                return f"gate: {body.get('did') or body.get('error')} · ip {body.get('ip')}"
            v = self._view()
            if not v["enrolled"]:
                return "Orven gate: this machine is not enrolled yet — open the ORVEN tab."
            return (f"Orven gate: ip {v['ip'] or '?'} · allowed {v['allowedIp'] or '—'} until {v['allowedUntil'] or '—'}"
                    + (f" · last error: {v['error']}" if v["error"] else "") + "\n/orven unlock — unlock now")
        return {"/orven": orven}

    def status(self) -> dict:
        v = self._view()
        if not v["enrolled"]:
            return {"line": "not enrolled", "ok": True}
        if v["error"]:
            return {"line": f"ip {v['ip']} · gate refused: {v['error']}", "ok": False}
        return {"line": f"ip {v['ip'] or '?'} · allowed until {_short(v['allowedUntil'])}", "ok": True}


def _parse_ts(s) -> float:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _short(s) -> str:
    if not s:
        return "—"
    return str(s).replace("T", " ")[:16]
