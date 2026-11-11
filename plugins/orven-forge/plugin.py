"""Forge plugin: this machine as a Forge executor, through Mystical sessions.

Forge (payam-h/forge) hands typed tasks to an agent: POST /api/agent/claim returns a task and a
job (renderedPrompt, model, permissionMode, allowedTools, inputs[{filename,url}], outputs[{name,
path}], slot); the agent streams text to /tasks/:id/output, polls /tasks/:id/poll for a cancel,
uploads each declared output to /tasks/:id/artifact (multipart: name, file) and ends with
/tasks/:id/finish ({status ok|failed, exitCode, sessionId, costUsd, summary, error}). Headers on
every call: x-agent-token, x-agent-id, x-agent-slots. The Orven Mac app did this with a bare
`claude -p`; here each task is a Mystical **session** (runner.start_streaming_job in the slot's
workspace, origin `forge:<template>`), so the transcript, the cost and the limit-fallback machinery
("3 ways on") apply to Forge runs, and they appear in the dashboard like any other session.

Two ways to run a task. Forge's volume is small tasks — comment moderation, 1,800 a day at 15 s
each (2026-11-09) — and a Mystical session per task would flood the store and the sidebar. So a
task runs as a **one-shot** (runner.run_blocking with tools, no context pack, no MCP: the Forge
prompt reaches claude as rendered) unless it is heavy: an Opus model, a tool allow-list, or a
template named in FORGE_SESSION_TEMPLATES (translations, biographies). Heavy tasks are **sessions**
(start_streaming_job, origin `forge:<template>`): transcript, cost, cancel from Forge, and the
limit-fallback machinery apply, and they appear in the dashboard like any other session.

The two rules the Mac app learned the hard way, kept:
  - one workspace per SLOT, wiped before each task, never shared: `<BASE_PATH>/.forge/<slot>` —
    under BASE_PATH so the session may run there (browser.within_base), hidden so the project
    browser skips it;
  - a run is `ok` only when it ended naturally: the job exited with status done, a result, no
    interruption, no watchdog timeout, no error; anything else is `failed` and its artifacts are
    NOT uploaded (so "has artifacts" keeps meaning "ran to completion").

Capacity is reserved before the claim call, not after (the original double-claim bug). Stdlib only.

How many slots. FORGE_SLOTS blank (the default) means "whatever the Forge panel says for this
device": the agent offers MAX_SLOTS in x-agent-slots, and Forge's claim never hands out more than
the device's own number there, so the panel is the one place to change it. The heartbeat answers
with that number when the Forge server is new enough (`slots`), and the tab shows it. A number in
FORGE_SLOTS still caps this Mac below what the panel says.

Out of the way. A heavy task's session is archived the moment it is made, so the slot workspaces
(anvil, ember, …) never turn up as projects in the sidebar; the tab's "session" link still opens
it. A one-shot runs with --no-session-persistence: nothing reads its transcript, and they used to
pile up at ~1,800 a day under ~/.claude/projects, which every session-list poll walked.

Which Claude login a task runs on. FORGE_PROFILE names a Mystical profile (the FORGE tab's ACCOUNT
picker writes it); blank means the bridge's default profile for the workspace. Both ways of running
follow it: a session is bound to the profile, a one-shot gets the profile's account slot (it used to
get none, i.e. always the machine's own login, whatever the profiles said). A picked profile that
cannot run (removed, not Claude, its login disabled) stops claiming rather than falling back to
another login: a task Forge hands out should not fail, or bill someone else, because of a local
setting. Every finished task is tallied per account in <data>/usage.json (runs, failures, cost,
seconds, by day), which is what the tab's USAGE section shows; Forge itself has no idea which login
paid for a task.
"""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

SLOT_NAMES = ("anvil", "ember", "bellows", "tongs", "forge", "mandrel", "swage", "chisel",
              "crucible", "hammer", "quench", "rasp", "scale", "flux", "hearth", "billet")
MAX_SLOTS = 16    # what Forge's own claim accepts in x-agent-slots; the panel's per-device number decides below it


def http(url: str, body: dict | None = None, headers: dict | None = None, method: str | None = None, timeout: int = 30, raw: bytes | None = None) -> tuple[int, object]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    if raw is None and data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        code = e.code
    try:
        return code, (json.loads(text) if text else {})
    except ValueError:
        return code, text


def multipart(fields: dict, filename: str, content: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body with text fields and one file part named `file`."""
    boundary = "----mystical" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
    out += content + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def natural_end(job) -> tuple[bool, str]:
    """Did the session end on its own terms? (ok, reason-if-not). Mirrors the Mac app's contract."""
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
    if not getattr(job, "result", None) and not getattr(job, "texts", None):
        return False, "the run ended without a result"
    return True, ""


class OneShot:
    """What run_blocking returned, shaped like a Job for natural_end() and the finish payload."""

    def __init__(self, text: str, session_id, cost, is_error: bool):
        self.result = text
        self.texts = [text] if text else []
        self.session_id = session_id
        self.cost = cost
        self.store_session_id = None
        self.events: list = []
        self.timed_out = text.startswith("⏱️")
        self.interrupted = False
        self.refusal = None
        self.error_msg = text if (is_error and not self.timed_out) else None
        self.status = "error" if is_error else "done"


def heavy(job: dict, session_templates: str) -> bool:
    """A session (not a one-shot) for Opus, for a tool allow-list, or for a template named in the setting."""
    model = str(job.get("model") or "").lower()
    if "opus" in model or str(job.get("allowedTools") or "").strip():
        return True
    names = {t.strip() for t in str(session_templates or "").split(",") if t.strip()}
    return str(job.get("templateKey") or "") in names


class Run:
    def __init__(self, task_id: str, job: dict, slot: int, workdir: str):
        self.task_id, self.job, self.slot, self.workdir = task_id, job, slot, workdir
        self.label = str(job.get("templateKey") or task_id[:8])
        self.started = time.time()
        self.session_id: str | None = None
        self.sent_events = 0
        self.state = "starting"
        self.mode = "session"
        self.account = ""                     # the login it ran on, as the usage tally names it


class Plugin:
    def __init__(self):
        self.ctx = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.runs: dict[int, Run] = {}
        self.reserved = 0
        self.online = False
        self.last_error: str | None = None
        self.last_claim_at: float | None = None
        self.last_beat_at: float | None = None
        self.recent: list[dict] = []          # last 20 finished tasks
        self.device_slots: int | None = None  # the Forge panel's number for this device, from the heartbeat
        self.usage: dict = {}                 # account name -> {day -> {runs, failed, cost, seconds}}
        # injectable for tests
        self.http: Callable = http
        self.start_run: Callable | None = None   # (prompt, workdir, model, permission_mode, allowed_tools, origin, hang) -> job
        self.wait_job: Callable | None = None    # (job, run, timeout) -> None
        self.run_once: Callable | None = None    # (prompt, workdir, model, permission_mode, hang) -> OneShot

    # --- lifecycle ---------------------------------------------------------------------------
    def start(self, ctx) -> None:
        self.ctx = ctx
        self._tidy_sessions()
        self._load_recent()
        self._load_usage()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="forge-agent", daemon=True)
        self._thread.start()

    def _tidy_sessions(self) -> None:
        """Archive Forge sessions made before they were archived at birth (once; cheap after)."""
        try:
            from bridge import store
            from contextlib import closing
            with closing(store._connect()) as c:
                ids = [r[0] for r in c.execute("SELECT id FROM sessions WHERE archived=0 AND origin LIKE 'forge:%'")]
            for sid in ids:
                store.archive(sid)
        except Exception as e:  # noqa: BLE001
            self.ctx.log(f"tidy sessions: {e}")

    def stop(self) -> None:
        if self.runs:
            from bridge import plugins_ext
            raise plugins_ext.PluginError(f"{len(self.runs)} Forge task(s) still running — let them finish, then remove Forge.")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def settings_changed(self, _s: dict) -> None:
        pass   # settings are read on every tick

    # --- settings & headers -------------------------------------------------------------------
    def _s(self) -> dict:
        return self.ctx.settings()

    def _url(self) -> str:
        return (self._s().get("FORGE_URL") or "https://forge.imagelens.xyz").rstrip("/")

    def _slots(self) -> int:
        """Tasks this Mac takes at once: the Forge panel's number for it, under FORGE_SLOTS if set."""
        try:
            local = max(1, min(MAX_SLOTS, int(self._s().get("FORGE_SLOTS") or MAX_SLOTS)))
        except ValueError:
            local = MAX_SLOTS
        return min(local, self.device_slots) if self.device_slots else local

    def _offer(self) -> int:
        """What x-agent-slots says: this Mac's own ceiling, never the panel's number fed back to it."""
        try:
            return max(1, min(MAX_SLOTS, int(self._s().get("FORGE_SLOTS") or MAX_SLOTS)))
        except ValueError:
            return MAX_SLOTS

    def _agent_id(self) -> str:
        p = os.path.join(self.ctx.data_dir, "agent-id")
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            aid = "mystical-" + uuid.uuid4().hex[:12]
            with open(p, "w", encoding="utf-8") as f:
                f.write(aid)
            return aid

    def _headers(self) -> dict:
        return {"x-agent-token": self._s().get("FORGE_AGENT_TOKEN", ""), "x-agent-id": self._agent_id(), "x-agent-slots": str(self._offer())}

    def _api(self, path: str, body: dict | None = None, raw: bytes | None = None, ctype: str | None = None):
        tok = self._s().get("FORGE_AGENT_TOKEN", "")
        if not tok:
            raise RuntimeError("no agent token")
        headers = self._headers()
        if ctype:
            headers["Content-Type"] = ctype
        return self.http(f"{self._url()}/api{path}", body if raw is None else None, headers, "POST", raw=raw)

    # --- the account ---------------------------------------------------------------------------
    def _account(self) -> dict:
        """The login tasks run on now: {picked, profile, name, slot, problem, options}. `profile` is
        the profile id a session is bound to (None = unbound), `slot` the account a one-shot gets
        (None = the machine's own login), `problem` why the picked profile cannot run."""
        out = {"picked": str((self._s() if self.ctx else {}).get("FORGE_PROFILE") or ""), "profile": None, "name": "this Mac's login",
               "slot": None, "problem": None, "options": []}
        try:
            from bridge import profiles
            from bridge.browser import rel
            claude = [p for p in profiles.all_profiles() if (p.get("agent") or profiles.CLAUDE) == profiles.CLAUDE]

            def usable(p: dict) -> bool:
                a = str(p.get("account") or "")
                return not a or (a.isdigit() and profiles.claude_slot_usable(int(a)))

            out["options"] = [{"id": p["id"], "name": p.get("name") or p["id"], "usable": usable(p)} for p in claude]
            if out["picked"]:
                p = next((x for x in claude if x["id"] == out["picked"]), None)
                if p is None:
                    out["problem"] = "account: the picked profile is gone or is not a Claude profile"
                    return out
                if not usable(p):
                    out["problem"] = f"account: {p.get('name') or p['id']} cannot run (its login is disabled or missing)"
            else:
                p = profiles.get(profiles.claude_default(rel(self._workspace(0))))
            if p:
                a = str(p.get("account") or "")
                out.update(profile=p["id"], name=p.get("name") or p["id"], slot=int(a) if a.isdigit() else None)
        except Exception as e:  # noqa: BLE001 - the tab must still draw
            if out["picked"]:           # a pick we cannot check is a pick we cannot honour
                out["problem"] = f"account: could not read the profiles ({e})"
            if self.ctx:
                self.ctx.log(f"account lookup failed: {e}")
        return out

    # --- the loop ----------------------------------------------------------------------------
    def _loop(self) -> None:
        beat_every = 30
        last_beat = 0.0
        while not self._stop.is_set():
            try:
                every = max(3, int(self._s().get("FORGE_POLL_SEC") or 10))
            except ValueError:
                every = 10
            paused = str(self._s().get("FORGE_PAUSED") or "0") not in ("0", "", "false", "no")
            if self._s().get("FORGE_AGENT_TOKEN"):
                try:
                    if time.time() - last_beat >= beat_every:
                        self.beat()
                        last_beat = time.time()
                    if not paused:
                        self.claim_once()
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)[:200]
                    self.online = False
            self._stop.wait(every)

    def beat(self) -> None:
        code, data = self._api("/agent/heartbeat", {"name": self._s().get("FORGE_AGENT_NAME") or "Mystical", "version": "mystical-plugin/0.2"})
        self.online = code < 400
        n = data.get("slots") if isinstance(data, dict) else None
        if isinstance(n, int) and n > 0:
            self.device_slots = min(MAX_SLOTS, n)
        self.last_beat_at = time.time()
        if not self.online:
            self.last_error = f"heartbeat {code}"

    def claim_once(self) -> str:
        """One claim attempt. Returns 'full', 'empty', 'claimed' or 'error'."""
        problem = self._account()["problem"]
        if problem:                     # never claim what the picked login cannot run
            self.last_error = problem
            return "error"
        if (self.last_error or "").startswith("account:"):
            self.last_error = None
        with self._lock:
            if len(self.runs) + self.reserved >= self._slots():
                return "full"
            self.reserved += 1          # taken BEFORE the await-like call: the double-claim bug
        holding = True
        try:
            code, data = self._api("/agent/claim")
            self.online = code < 400
            if code >= 400:
                self.last_error = f"claim {code}"
                return "error"
            if not isinstance(data, dict) or not data.get("task") or not data.get("job"):
                return "empty"
            task_id = str(data["task"]["id"])
            job = data["job"]
            with self._lock:
                slot = int(job.get("slot") or 0)
                while slot in self.runs:
                    slot += 1
                run = Run(task_id, job, slot, self._workspace(slot))
                self.runs[slot] = run      # the slot is held before the reservation is released
                self.reserved -= 1
                holding = False
            self.last_claim_at = time.time()
            threading.Thread(target=self._run_task, args=(run,), name=f"forge-{task_id[:8]}", daemon=True).start()
            return "claimed"
        finally:
            if holding:
                with self._lock:
                    self.reserved -= 1

    # --- one task ----------------------------------------------------------------------------
    def _workspace(self, slot: int) -> str:
        from bridge import config
        name = SLOT_NAMES[slot] if slot < len(SLOT_NAMES) else f"slot{slot}"
        return os.path.join(config.BASE_PATH, ".forge", name)

    def _run_task(self, run: Run) -> None:
        job = run.job
        work = run.workdir
        out_parts: list[str] = []
        finished = False

        def finish(status: str, **kw) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            try:
                self._api(f"/agent/tasks/{run.task_id}/finish", {"status": status, **kw})
            except Exception as e:  # noqa: BLE001
                self.ctx.log(f"finish failed for {run.task_id}: {e}")

        try:
            # 1) workspace + inputs
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(os.path.join(work, "input"), exist_ok=True)
            os.makedirs(os.path.join(work, "output"), exist_ok=True)
            for inp in job.get("inputs") or []:
                url, fname = str(inp.get("url") or ""), os.path.basename(str(inp.get("filename") or "input"))
                headers = self._headers() if url.startswith(self._url()) else {}
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=120) as r:
                    with open(os.path.join(work, "input", fname), "wb") as f:
                        shutil.copyfileobj(r, f)
            run.mode = "session" if heavy(job, self._s().get("FORGE_SESSION_TEMPLATES", "")) else "one-shot"
            run.account = self._account()["name"]
            self._output(run, f"· workspace: {os.path.basename(work)} (slot {run.slot}) · Mystical {run.mode} · {run.account}\n")

            # 2) the run
            try:
                max_min = int(self._s().get("FORGE_MAX_RUN_MIN") or 160)
            except ValueError:
                max_min = 160
            prompt = str(job.get("renderedPrompt") or "")
            model = str(job.get("model") or "") or None
            pm = str(job.get("permissionMode") or "bypassPermissions")
            run.state = "running"
            if run.mode == "one-shot":
                job_obj = (self.run_once or self._run_once)(prompt, work, model, pm, max_min * 60)
            else:
                job_obj = (self.start_run or self._start_session)(prompt, work, model, pm, str(job.get("allowedTools") or ""), f"forge:{run.label}", max_min * 60)
                if job_obj is None:
                    raise RuntimeError("could not start a session (busy)")
                run.session_id = getattr(job_obj, "store_session_id", None)
                (self.wait_job or self._wait)(job_obj, run, max_min * 60)

            # 3) verdict, artifacts, finish
            ok, why = natural_end(job_obj)
            produced = []
            for out in job.get("outputs") or []:
                rel = str(out.get("path") or "")
                full = os.path.join(work, rel)
                if not rel or not os.path.isfile(full) or os.path.getsize(full) == 0:
                    continue
                produced.append(rel)
                if ok:
                    with open(full, "rb") as f:
                        body, ctype = multipart({"name": str(out.get("name") or rel)}, os.path.basename(rel), f.read())
                    self._api(f"/agent/tasks/{run.task_id}/artifact", raw=body, ctype=ctype)
            if not ok and produced:
                self._output(run, f"\n[not uploaded — run did not complete: {', '.join(produced)}]\n")
            summary = (getattr(job_obj, "result", None) or "\n".join(getattr(job_obj, "texts", []) or []))[:59000]
            finish("ok" if ok else "failed", exitCode=0 if ok else -1, sessionId=getattr(job_obj, "session_id", None),
                   costUsd=getattr(job_obj, "cost", None), summary=summary or None, error=None if ok else why)
            self._remember(run, "ok" if ok else "failed", why, getattr(job_obj, "cost", None))
        except Exception as e:  # noqa: BLE001
            finish("failed", error=str(e)[:500])
            self._remember(run, "failed", str(e)[:200], None)
            self.ctx.log(f"task {run.task_id} failed: {e}")
        finally:
            with self._lock:
                self.runs.pop(run.slot, None)
            shutil.rmtree(work, ignore_errors=True)

    def _run_once(self, prompt: str, workdir: str, model: str | None, permission_mode: str, hang: int) -> OneShot:
        """One claude -p in the workspace: tools on, no context pack, no MCP (skip_pack with a permission mode)."""
        from bridge import config, native, runner
        # Tagged like the bridge's own one-shots: the session scanner (bridge/native.py) would otherwise list
        # every run — comment-moderation alone is ~1,800 a day — as a session in the sidebar.
        text, sid, cost, is_error = runner.run_blocking(config.DASH_CHAT_ID, f"{native.INTERNAL_ONESHOT_TAG}\n{prompt}", cwd=workdir, timeout=hang, model=model,
                                                        skip_pack=True, permission_mode=permission_mode, account_slot=self._account()["slot"],
                                                        extra_args=["--no-session-persistence"])
        return OneShot(text or "", sid, cost, bool(is_error))

    def _start_session(self, prompt: str, workdir: str, model: str | None, permission_mode: str, allowed_tools: str, origin: str, hang: int):
        from bridge import config, runner, store
        from bridge.browser import rel
        session = store.create_session(config.DASH_CHAT_ID, rel(workdir), session_id=uuid.uuid4().hex, origin=origin,
                                       cwd=workdir, permission_mode=permission_mode, profile_id=self._account()["profile"])
        if model:
            store.set_run_settings(session["id"], model=model)
        store.rename(session["id"], f"Forge · {origin.split(':', 1)[-1]}")
        store.archive(session["id"])          # a slot workspace is not a project: keep it out of the sidebar
        extra = ["--allowedTools", allowed_tools] if allowed_tools else None
        return runner.start_streaming_job(config.DASH_CHAT_ID, prompt, [], project=workdir, model=model or None,
                                          permission_mode=permission_mode, session_id=session["id"], origin=origin,
                                          extra_args=extra, hang_timeout=hang)

    def _wait(self, job_obj, run: Run, timeout: float) -> None:
        """Stream new text to Forge every ~1.5 s, honour a cancel from Forge, stop at the cap."""
        deadline = time.time() + timeout
        last_poll = 0.0
        exited = getattr(job_obj, "exited", None)
        while True:
            over = exited.is_set() if exited is not None else getattr(job_obj, "status", "running") != "running"
            self._stream(run, job_obj)
            if over:
                return
            now = time.time()
            if now > deadline:
                self._output(run, f"\n[run exceeded {int(timeout // 60)} min — stopped]\n")
                self._interrupt(job_obj)
                job_obj.timed_out = True
                return
            if now - last_poll >= 5:
                last_poll = now
                try:
                    code, j = self._api(f"/agent/tasks/{run.task_id}/poll")
                    if code < 400 and isinstance(j, dict) and j.get("canceled"):
                        self._output(run, f"\n[stopped — Forge marked this task {j.get('status') or 'canceled'}]\n")
                        self._interrupt(job_obj)
                        job_obj.interrupted = True
                        return
                except Exception:  # noqa: BLE001 - a poll that fails is not a cancel
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

    def _stream(self, run: Run, job_obj) -> None:
        events = getattr(job_obj, "events", None) or []
        if len(events) <= run.sent_events:
            return
        chunk = []
        for ev in events[run.sent_events:]:
            t = ev.get("type")
            if t == "text" and ev.get("text"):
                chunk.append(str(ev["text"]) + "\n")
            elif t in ("tool", "tool_use") and ev.get("name"):
                chunk.append(f"· tool: {ev['name']}\n")
        run.sent_events = len(events)
        if chunk:
            self._output(run, "".join(chunk))

    def _output(self, run: Run, text: str) -> None:
        try:
            self._api(f"/agent/tasks/{run.task_id}/output", {"chunk": text})
        except Exception:  # noqa: BLE001
            pass

    # --- memory of recent runs -----------------------------------------------------------------
    def _recent_path(self) -> str:
        return os.path.join(self.ctx.data_dir, "recent.json")

    def _load_recent(self) -> None:
        try:
            with open(self._recent_path(), encoding="utf-8") as f:
                self.recent = json.load(f)[:20]
        except (OSError, ValueError):
            self.recent = []

    def _remember(self, run: Run, status: str, why: str, cost) -> None:
        self.recent.insert(0, {"task": run.task_id, "label": run.label, "slot": run.slot, "mode": run.mode, "status": status, "why": why[:200] if why else "",
                               "cost": cost, "session": run.session_id, "seconds": int(time.time() - run.started), "at": time.time(),
                               "account": run.account})
        self.recent = self.recent[:20]
        try:
            with open(self._recent_path(), "w", encoding="utf-8") as f:
                json.dump(self.recent, f)
        except OSError:
            pass
        self._tally(run, status, cost)

    # --- usage per account ---------------------------------------------------------------------
    def _usage_path(self) -> str:
        return os.path.join(self.ctx.data_dir, "usage.json")

    def _load_usage(self) -> None:
        try:
            with open(self._usage_path(), encoding="utf-8") as f:
                data = json.load(f)
            self.usage = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.usage = {}

    def _tally(self, run: Run, status: str, cost) -> None:
        """Count one finished task against the login it ran on, under today's date."""
        with self._lock:
            day = self.usage.setdefault(run.account or "unknown", {}).setdefault(time.strftime("%Y-%m-%d"), {"runs": 0, "failed": 0, "cost": 0.0, "seconds": 0})
            day["runs"] += 1
            day["failed"] += 0 if status == "ok" else 1
            day["cost"] = round(day["cost"] + float(cost or 0), 6)
            day["seconds"] += int(time.time() - run.started)
            try:
                tmp = self._usage_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.usage, f)
                os.replace(tmp, self._usage_path())
            except OSError:
                pass

    def _usage_view(self) -> list[dict]:
        """Per account: today, the last 7 days, and everything since the tally began."""
        today = time.strftime("%Y-%m-%d")
        week = {time.strftime("%Y-%m-%d", time.localtime(time.time() - d * 86400)) for d in range(7)}

        def total(days: dict, keep) -> dict:
            out = {"runs": 0, "failed": 0, "cost": 0.0, "seconds": 0}
            for d, v in days.items():
                if keep(d):
                    for k in out:
                        out[k] += v.get(k) or 0
            out["cost"] = round(out["cost"], 4)
            return out

        rows = [{"name": name, "today": total(days, lambda d: d == today), "week": total(days, lambda d: d in week),
                 "all": total(days, lambda d: True), "since": min(days) if days else None}
                for name, days in self.usage.items()]
        return sorted(rows, key=lambda r: -r["all"]["runs"])

    # --- routes, telegram, status --------------------------------------------------------------
    def _view(self) -> dict:
        s = self._s()
        return {
            "url": self._url(), "configured": bool(s.get("FORGE_AGENT_TOKEN")), "paused": str(s.get("FORGE_PAUSED") or "0") not in ("0", "", "false", "no"),
            "online": self.online, "slots": self._slots(), "agentId": self._agent_id() if self.ctx else None,
            "running": [{"slot": r.slot, "task": r.task_id, "label": r.label, "state": r.state, "mode": r.mode, "session": r.session_id, "seconds": int(time.time() - r.started)} for r in sorted(self.runs.values(), key=lambda r: r.slot)],
            "lastClaimAt": self.last_claim_at, "lastBeatAt": self.last_beat_at, "error": self.last_error, "recent": self.recent,
            "account": {k: v for k, v in self._account().items() if k != "slot"}, "usage": self._usage_view(),
        }

    def routes(self) -> dict:
        return {
            ("GET", "/state"): lambda req: (self._view(), 200),
            ("POST", "/claim"): lambda req: ({"did": self.claim_once(), **self._view()}, 200),
            ("POST", "/pause"): self._r_pause,
            ("POST", "/account"): self._r_account,
        }

    def _r_account(self, req):
        """Pick the profile Forge tasks run on; "" goes back to the bridge's default."""
        from bridge import plugins_ext
        pid = str(req["body"].get("profile") or "")
        if pid and pid not in {o["id"] for o in self._account()["options"]}:
            return {"error": "no such Claude profile"}, 400
        plugins_ext.save_settings("orven-forge", {"FORGE_PROFILE": pid})
        return self._view(), 200

    def _r_pause(self, req):
        from bridge import plugins_ext
        plugins_ext.save_settings("orven-forge", {"FORGE_PAUSED": "1" if req["body"].get("paused") else ""})
        return self._view(), 200

    def telegram(self) -> dict:
        def forge(chat_id: int, rest: str) -> str:
            v = self._view()
            if not v["configured"]:
                return "Forge: no agent token yet — SETTINGS ▸ INTEGRATIONS ▸ Forge."
            run = ", ".join(f"{r['label']} ({r['seconds'] // 60} min)" for r in v["running"]) or "idle"
            last = v["recent"][0] if v["recent"] else None
            return (f"Forge: {'online' if v['online'] else 'offline'} · {len(v['running'])}/{v['slots']} slots · {run}"
                    + (f"\nlast: {last['label']} {last['status']}" + (f" — {last['why']}" if last.get('why') else "") if last else "")
                    + (f"\nerror: {v['error']}" if v["error"] else "") + ("\n(paused)" if v["paused"] else ""))
        return {"/forge": forge}

    def status(self) -> dict:
        v = self._view()
        if not v["configured"]:
            return {"line": "no agent token", "ok": True}
        if v["error"] and not v["online"]:
            return {"line": f"offline — {v['error']}", "ok": False}
        return {"line": f"{'online' if v['online'] else 'starting'} · {len(v['running'])}/{v['slots']} slots" + (" · paused" if v["paused"] else ""), "ok": True}
