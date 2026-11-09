"""Orven jobs plugin: the panel's Claude jobs, run here as Mystical sessions.

The Orven panel keeps "Claude jobs": a prompt, a folder (cwd), a model, a permission mode, an
interval; a cron creates a pending run, an agent claims it and reports back (output, finish),
and finished reports feed the panel's newspaper. The Orven Mac app was the only agent. The
contract (api/src/claude-jobs): POST /api/claude-jobs/agent/heartbeat {name, version};
POST /agent/claim → {run: {id}, job: {id, name, prompt, cwd, model, permissionMode, sessionId?,
fixes?}} or null; POST /runs/:id/output {chunk}; POST /runs/:id/poll → {canceled}; POST
/runs/:id/finish {status ok|failed, exitCode, sessionId, costUsd, summary, error}. Auth is the
**device gate token** (GateAgentGuard: x-gate-token), which the Orven plugin holds for this
machine — so this plugin needs that one enrolled, and refreshes the token through it on a 401.

Why one Mystical session per job, resumed every run: a job is the same prompt in the same
folder every N hours; the panel already carries a `sessionId` to resume so the model keeps
context across runs (what it reported last time, what was fixed). A store session per job id
gives the same continuity the Mystical way: the transcript is one long thread per job, the
runner resumes it, and the limit-fallback machinery applies. Runs are few (35 a month), so
sessions, never one-shots. `fixes` (what the fixer resolved since the last run) is prepended
the way the Mac app did it, so the job does not re-report them.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable


def http(url: str, body: dict | None = None, headers: dict | None = None, method: str | None = None, timeout: int = 30) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text, code = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        text, code = e.read().decode("utf-8", "replace"), e.code
    try:
        return code, (json.loads(text) if text else {})
    except ValueError:
        return code, text


def natural_end(job) -> tuple[bool, str]:
    if getattr(job, "refusal", None):
        return False, f"the run was refused: {job.refusal}"
    if getattr(job, "timed_out", False):
        return False, "the hang watchdog killed the run"
    if getattr(job, "interrupted", False):
        return False, "the run was stopped"
    if getattr(job, "status", "") != "done":
        return False, getattr(job, "error_msg", None) or f"the run ended with status {getattr(job, 'status', '?')}"
    if getattr(job, "error_msg", None):
        return False, str(job.error_msg)
    return True, ""


def prompt_with_fixes(prompt: str, fixes) -> str:
    """The Mac app's framing: resolved fixes first, so the job does not re-report them."""
    if not fixes:
        return prompt
    block = "\n".join(f"- {f}" if isinstance(f, str) else f"- {f.get('body') or f.get('text') or json.dumps(f)}" for f in fixes)
    return ("FIXES APPLIED SINCE YOUR LAST RUN (reported by the fixer) — treat these as RESOLVED: do not re-report them, "
            f"and if any recur note that they were supposedly fixed:\n\n{block}\n\n----------\n\n{prompt}")


class Current:
    def __init__(self, run_id: str, job: dict):
        self.run_id, self.job = run_id, job
        self.name = str(job.get("name") or run_id[:8])
        self.started = time.time()
        self.session_id: str | None = None
        self.sent_events = 0


class Plugin:
    def __init__(self):
        self.ctx = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.current: Current | None = None
        self.claiming = False
        self.online = False
        self.last_error: str | None = None
        self.last_beat_at: float | None = None
        self.last_claim_at: float | None = None
        self.recent: list[dict] = []
        self.sessions: dict[str, str] = {}      # panel job id -> store session id
        self.http: Callable = http
        self.gate_token: Callable[[bool], str | None] | None = None   # (refresh) -> token; injectable
        self.start_run: Callable | None = None
        self.wait_job: Callable | None = None

    # --- lifecycle -------------------------------------------------------------------------
    def start(self, ctx) -> None:
        self.ctx = ctx
        self._load()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="orven-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.current:
            from bridge import plugins_ext
            raise plugins_ext.PluginError(f"job «{self.current.name}» is running — let it finish, then remove Orven jobs.")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def settings_changed(self, _s: dict) -> None:
        pass

    def _s(self) -> dict:
        return self.ctx.settings()

    def _url(self) -> str:
        return (self._s().get("JOBS_PANEL_URL") or "https://console.orven.am").rstrip("/")

    # --- the gate token, from the Orven plugin --------------------------------------------
    def _token(self, refresh: bool = False) -> str | None:
        if self.gate_token:
            return self.gate_token(refresh)
        from bridge import plugins_ext
        if refresh:
            try:
                plugins_ext.dispatch("POST", "/plugin/orven/unlock", {}, {}, None)
            except Exception:  # noqa: BLE001
                pass
        try:
            with open(os.path.join(plugins_ext.data_dir("orven"), "gate.json"), encoding="utf-8") as f:
                return json.load(f).get("token") or None
        except (OSError, ValueError):
            return None

    def _api(self, path: str, body: dict | None = None) -> tuple[int, object]:
        tok = self._token()
        if not tok:
            raise RuntimeError("no gate token — install and enrol the Orven plugin first")
        code, out = self.http(f"{self._url()}/api{path}", body if body is not None else {}, {"x-gate-token": tok, "Cookie": f"orven_gate={tok}"}, "POST")
        if code in (401, 403):
            tok = self._token(refresh=True)
            if tok:
                code, out = self.http(f"{self._url()}/api{path}", body if body is not None else {}, {"x-gate-token": tok, "Cookie": f"orven_gate={tok}"}, "POST")
        return code, out

    # --- the loop --------------------------------------------------------------------------
    def _loop(self) -> None:
        last_beat = 0.0
        while not self._stop.is_set():
            try:
                every = max(5, int(self._s().get("JOBS_POLL_SEC") or 20))
            except ValueError:
                every = 20
            paused = str(self._s().get("JOBS_PAUSED") or "0") not in ("0", "", "false", "no")
            try:
                if time.time() - last_beat >= 30:
                    self.beat()
                    last_beat = time.time()
                if not paused:
                    self.claim_once()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)[:200]
                self.online = False
            self._stop.wait(every)

    def beat(self) -> None:
        code, _ = self._api("/claude-jobs/agent/heartbeat", {"name": self._s().get("JOBS_AGENT_NAME") or "Mystical", "version": "mystical-plugin/0.1"})
        self.online = code < 400
        self.last_beat_at = time.time()
        if not self.online:
            self.last_error = f"heartbeat {code}"
        else:
            self.last_error = None

    def claim_once(self) -> str:
        """'busy' | 'empty' | 'claimed' | 'error'. One run at a time, the claim guarded before the call."""
        with self._lock:
            if self.current or self.claiming:
                return "busy"
            self.claiming = True
        try:
            code, data = self._api("/claude-jobs/agent/claim")
            self.online = code < 400
            if code >= 400:
                self.last_error = f"claim {code}"
                return "error"
            if not isinstance(data, dict) or not data.get("run") or not data.get("job"):
                return "empty"
            cur = Current(str(data["run"]["id"]), data["job"])
            with self._lock:
                self.current = cur
                self.claiming = False
            self.last_claim_at = time.time()
            threading.Thread(target=self._run, args=(cur,), name=f"orven-job-{cur.run_id[:8]}", daemon=True).start()
            return "claimed"
        finally:
            with self._lock:
                self.claiming = False

    # --- one run ---------------------------------------------------------------------------
    def _run(self, cur: Current) -> None:
        job = cur.job
        finished = False

        def finish(status: str, **kw) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            try:
                self._api(f"/claude-jobs/runs/{cur.run_id}/finish", {"status": status, **kw})
            except Exception as e:  # noqa: BLE001
                self.ctx.log(f"finish failed for run {cur.run_id}: {e}")

        try:
            from bridge import config
            from bridge.browser import within_base
            cwd = os.path.realpath(os.path.expanduser(str(job.get("cwd") or "")))
            if not cwd or not os.path.isdir(cwd):
                raise RuntimeError(f"folder does not exist on this machine: {job.get('cwd')}")
            if not within_base(cwd):
                raise RuntimeError(f"folder is outside BASE_PATH ({config.BASE_PATH}): {cwd}")
            try:
                max_min = int(self._s().get("JOBS_MAX_RUN_MIN") or 120)
            except ValueError:
                max_min = 120
            prompt = prompt_with_fixes(str(job.get("prompt") or ""), job.get("fixes"))
            self._output(cur, f"· Mystical session in {cwd}\n")
            job_obj = (self.start_run or self._start_session)(str(job.get("id") or cur.run_id), cur.name, prompt, cwd,
                                                              str(job.get("model") or "") or None, str(job.get("permissionMode") or "bypassPermissions"), max_min * 60)
            if job_obj is None:
                raise RuntimeError("could not start a session (busy)")
            cur.session_id = getattr(job_obj, "store_session_id", None)
            (self.wait_job or self._wait)(job_obj, cur, max_min * 60)
            ok, why = natural_end(job_obj)
            summary = (getattr(job_obj, "result", None) or "\n".join(getattr(job_obj, "texts", []) or []))[:59000]
            finish("ok" if ok else "failed", exitCode=0 if ok else -1, sessionId=getattr(job_obj, "session_id", None),
                   costUsd=getattr(job_obj, "cost", None), summary=summary or None, error=None if ok else why)
            self._remember(cur, "ok" if ok else "failed", why, getattr(job_obj, "cost", None))
        except Exception as e:  # noqa: BLE001
            finish("failed", error=str(e)[:3900])
            self._remember(cur, "failed", str(e)[:200], None)
            self.ctx.log(f"run {cur.run_id} failed: {e}")
        finally:
            with self._lock:
                self.current = None

    def _start_session(self, job_id: str, name: str, prompt: str, cwd: str, model: str | None, permission_mode: str, hang: int):
        """The job's own store session, created once and resumed on every run."""
        from bridge import config, profiles, runner, store
        from bridge.browser import rel
        sid = self.sessions.get(job_id)
        if sid and not store.get_session(sid):
            sid = None
        if not sid:
            session = store.create_session(config.DASH_CHAT_ID, rel(cwd), session_id=uuid.uuid4().hex, origin=f"orven-job:{name}",
                                           cwd=cwd, permission_mode=permission_mode, profile_id=profiles.claude_default(rel(cwd)))
            store.rename(session["id"], f"Orven job · {name}")
            sid = session["id"]
            self.sessions[job_id] = sid
            self._save()
        if model:
            store.set_run_settings(sid, model=model)
        return runner.start_streaming_job(config.DASH_CHAT_ID, prompt, [], project=cwd, model=model or None,
                                          permission_mode=permission_mode, session_id=sid, origin=f"orven-job:{name}", hang_timeout=hang)

    def _wait(self, job_obj, cur: Current, timeout: float) -> None:
        deadline = time.time() + timeout
        last_poll = 0.0
        exited = getattr(job_obj, "exited", None)
        while True:
            over = exited.is_set() if exited is not None else getattr(job_obj, "status", "running") != "running"
            self._stream(cur, job_obj)
            if over:
                return
            now = time.time()
            if now > deadline:
                self._output(cur, f"\n[run exceeded {int(timeout // 60)} min — stopped]\n")
                self._interrupt(job_obj)
                job_obj.timed_out = True
                return
            if now - last_poll >= 5:
                last_poll = now
                try:
                    code, j = self._api(f"/claude-jobs/runs/{cur.run_id}/poll")
                    if code < 400 and isinstance(j, dict) and j.get("canceled"):
                        self._output(cur, "\n[canceled by user]\n")
                        self._interrupt(job_obj)
                        job_obj.interrupted = True
                        return
                except Exception:  # noqa: BLE001
                    pass
            if self._stop.wait(1.5):
                self._interrupt(job_obj)
                job_obj.interrupted = True
                return

    @staticmethod
    def _interrupt(job_obj) -> None:
        for name in ("interrupt", "stop"):
            fn = getattr(job_obj, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass
                return

    def _stream(self, cur: Current, job_obj) -> None:
        events = getattr(job_obj, "events", None) or []
        if len(events) <= cur.sent_events:
            return
        chunk = []
        for ev in events[cur.sent_events:]:
            t = ev.get("type")
            if t == "text" and ev.get("text"):
                chunk.append(str(ev["text"]) + "\n")
            elif t in ("tool", "tool_use") and ev.get("name"):
                chunk.append(f"· tool: {ev['name']}\n")
        cur.sent_events = len(events)
        if chunk:
            self._output(cur, "".join(chunk))

    def _output(self, cur: Current, text: str) -> None:
        try:
            self._api(f"/claude-jobs/runs/{cur.run_id}/output", {"chunk": text})
        except Exception:  # noqa: BLE001
            pass

    # --- state on disk ---------------------------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self.ctx.data_dir, "state.json")

    def _load(self) -> None:
        try:
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            self.sessions = dict(d.get("sessions") or {})
            self.recent = list(d.get("recent") or [])[:20]
        except (OSError, ValueError):
            self.sessions, self.recent = {}, []

    def _save(self) -> None:
        try:
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump({"sessions": self.sessions, "recent": self.recent[:20]}, f)
        except OSError:
            pass

    def _remember(self, cur: Current, status: str, why: str, cost) -> None:
        self.recent.insert(0, {"run": cur.run_id, "job": cur.job.get("id"), "name": cur.name, "status": status, "why": (why or "")[:200],
                               "cost": cost, "session": cur.session_id, "seconds": int(time.time() - cur.started), "at": time.time()})
        self.recent = self.recent[:20]
        self._save()

    # --- routes, telegram, status ----------------------------------------------------------
    def _view(self) -> dict:
        s = self._s()
        c = self.current
        return {"url": self._url(), "paused": str(s.get("JOBS_PAUSED") or "0") not in ("0", "", "false", "no"), "online": self.online,
                "hasToken": bool(self._token()), "lastBeatAt": self.last_beat_at, "lastClaimAt": self.last_claim_at, "error": self.last_error,
                "current": {"run": c.run_id, "name": c.name, "session": c.session_id, "seconds": int(time.time() - c.started)} if c else None,
                "recent": self.recent, "sessions": len(self.sessions)}

    def routes(self) -> dict:
        return {
            ("GET", "/state"): lambda req: (self._view(), 200),
            ("POST", "/claim"): lambda req: ({"did": self.claim_once(), **self._view()}, 200),
            ("POST", "/pause"): self._r_pause,
        }

    def _r_pause(self, req):
        from bridge import plugins_ext
        plugins_ext.save_settings("orven-jobs", {"JOBS_PAUSED": "1" if req["body"].get("paused") else ""})
        return self._view(), 200

    def telegram(self) -> dict:
        def jobs(chat_id: int, rest: str) -> str:
            v = self._view()
            if not v["hasToken"]:
                return "Orven jobs: no gate token — enrol this machine in the ORVEN tab first."
            cur = f"running «{v['current']['name']}» for {v['current']['seconds'] // 60} min" if v["current"] else "idle"
            last = v["recent"][0] if v["recent"] else None
            return (f"Orven jobs: {'online' if v['online'] else 'offline'} · {cur}" + (f"\nlast: {last['name']} {last['status']}" + (f" — {last['why']}" if last.get('why') else "") if last else "")
                    + (f"\nerror: {v['error']}" if v["error"] else "") + ("\n(paused)" if v["paused"] else ""))
        return {"/jobs": jobs}

    def status(self) -> dict:
        v = self._view()
        if not v["hasToken"]:
            return {"line": "needs the Orven plugin's gate token", "ok": False}
        if v["error"] and not v["online"]:
            return {"line": f"offline — {v['error']}", "ok": False}
        return {"line": ("online" if v["online"] else "starting") + (f" · running {v['current']['name']}" if v["current"] else " · idle") + (" · paused" if v["paused"] else ""), "ok": True}
