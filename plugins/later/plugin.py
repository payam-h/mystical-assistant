"""Later: jobs the bridge owns, which wake the session that started them when they end.

Why this exists. A turn is one `claude -p` child, and the runner ends it 15 s after its answer
once only background shells keep it alive (runner.BG_SHELL_GRACE). A build started with
`run_in_background` dies there. One started with nohup lives on, but nothing knows it exists,
so a turn that ended "waiting for the build" is never woken. That happened twice to the desktop
installer (1.2.1, 1.2.2), with the build done at 23:03 and nothing published for 16 hours.

What it does. `later__job_start` hands the command to the bridge, which runs it detached
(its own process group, so neither the turn's end nor a bridge restart takes it down). Its
output goes to a log FILE, not a pipe, and its exit code is written to a file by the wrapper,
so a bridge that restarted can still learn how it ended. When it ends, the session that
started it gets a new turn (on its own queue, so a turn you are in finishes first) with the
exit code, the end of the log and what the agent said it would do next. If a sub-agent
started it, the note names that agent so the session can resume it with SendMessage.

Why not wait_for or Automations. wait_for (bridge/timed_waits.py) waits on a time, a PR or a
port, holds one wait per session, ends at 24 h and is dropped by your next message.
Automations are owner-only, and they run in another chat. A job is a promise the bridge holds
until the job ends or you cancel it, and your messages don't cancel it.

Limits (settings): open items per session, jobs running at once, wake-ups per session per
hour, and wake-up tokens per session per day. Past a limit a wake-up is held and retried (the
hour rolls over, the day turns) or released with /later release. Only a session the owner
started can create jobs: no automation, Forge, Orven job, Pixie or Rivendell run. A command
that would stop or restart the bridge is refused, and a job never sees the bridge's tokens.

Quiet hours (LATER_QUIET): a wake-up still runs at night. Its Telegram pings wait for the end
of the window, as one message, unless the job was started urgent (runner._notify's fork seam
asks plugins_ext.holds_ping).

ponytail: liveness after a restart is polled every TICK_S (pid + its start time, so a reused
pid isn't the job), as stdlib has no way to wait on a process that isn't our child.

Stdlib only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid

TICK_S = 5
COALESCE_S = 3          # jobs that end within this of each other wake their chat once
KEEP_S = 7 * 86400
TAIL_LINES = 15
OPEN = ("running", "due", "held")
DEFAULT_TIMEOUT_MIN, MAX_TIMEOUT_MIN = 240, 1440
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# Origins of runs nobody is watching: these never get to schedule anything.
_UNATTENDED = ("automation", "orven-job", "forge", "pixie", "rivendell", "team:", "device-run:", "later")
# What would stop, restart or reload the bridge (or every python), from a job.
_BRIDGE = re.compile(
    r"\bmystical\s+(?:restart|stop|start|update|reload|app)\b|launchctl\b[^\n;|&]*mystical|"
    r"claude_telegram_bridge|systemctl\b[^\n;|&]*mystical|\bpkill\b[^\n;|&]*(?:mystical|bridge|python)|"
    r"\bkillall\b[^\n;|&]*(?:python|mystical)|cloud\.ainurhq\.mystical", re.I)
_SECRETS = ("MYSTICAL_DASH_TOKEN", "MYSTICAL_GATEWAY_TOKEN", "MYSTICAL_GATEWAY_URL", "DASH_TOKEN",
            "TELEGRAM_BOT_TOKEN", "MYSTICAL_DASH")

_S = {"type": "string"}
TOOLS = [
    {"name": "job_start", "description": (
        "Run a long command (a build, an installer, a test suite, an upload) owned by the bridge instead of "
        "run_in_background or nohup. It outlives this turn and a bridge restart, and its output goes to a log. "
        "When it ends you are woken in this same chat with the exit code and the end of the log. Start it, then "
        "end your turn with one line saying what you're waiting for. A sub-agent may call it too: then end the "
        "sub-agent, and the session is woken and told which agent to resume."),
     "inputSchema": {"type": "object", "required": ["command", "name", "then"], "properties": {
         "command": {"type": "string", "description": "Shell command, run with bash in cwd."},
         "name": {"type": "string", "description": "Short name, like: desktop 1.2.2 installer build."},
         "then": {"type": "string", "description": "What you will do with the result, in one sentence."},
         "cwd": {"type": "string", "description": "Folder to run in. Default: this chat's folder."},
         "timeout_min": {"type": "integer", "description": f"Stop it after this long. Default {DEFAULT_TIMEOUT_MIN}, at most {MAX_TIMEOUT_MIN}."},
         "wake": {"type": "string", "enum": ["exit", "fail", "never"], "description": "When to wake you: when it ends (default), only if it fails, or never."},
         "urgent": {"type": "boolean", "description": "Ping even in quiet hours. Only for something the user must see at once."},
         "agent_label": {"type": "string", "description": "From a sub-agent: what you are working on, so the session knows who to resume."}}}},
    {"name": "job_status", "description": "This chat's jobs: state, exit code, time, log. With id, that job and the end of its log.",
     "inputSchema": {"type": "object", "properties": {"id": _S, "lines": {"type": "integer"}}}},
    {"name": "job_cancel", "description": "Stop a job (its whole process group) and drop its wake-up.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": _S}}},
]


def _now() -> float:
    return time.time()


def _int(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _dur(s: float) -> str:
    s = int(max(0, s))
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def _clock(ts: "float | None") -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "?"


def _pid_start(pid: int) -> str:
    """When `pid` started, as ps says it, or "" when there is no such process."""
    try:
        p = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5)
        return " ".join((p.stdout or "").split())
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""


def _alive(it: dict) -> bool:
    """Is the job's process (the wrapper, its group leader) still the one we started?"""
    pid = it.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    got = _pid_start(int(pid))
    return bool(got) and (not it.get("pid_start") or got == it["pid_start"])


def tail(path: str, n: int = TAIL_LINES, max_bytes: int = 64 * 1024) -> list:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [_ANSI.sub("", ln).rstrip() for ln in data.replace("\r", "\n").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    return [ln[:300] for ln in lines[-n:]]


def in_quiet(spec: str, now: "float | None" = None) -> bool:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", spec or "")
    if not m:
        return False
    a, b = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
    t = time.localtime(now or _now())
    cur = t.tm_hour * 60 + t.tm_min
    return (a <= cur < b) if a < b else (cur >= a or cur < b) if a > b else False


def _day_start(now: float) -> float:
    t = time.localtime(now)
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))


def refuses(command: str) -> "str | None":
    """Why a command may not run as a job, or None."""
    if _BRIDGE.search(command or ""):
        return "That command would stop or restart the Mystical bridge, which this whole chat runs in. Jobs can't do that; ask the user to press ⟳ themselves."
    me = {str(os.getpid()), str(os.getpgrp())}
    if re.search(r"\bkill\b", command or "") and me & set(re.findall(r"\b\d+\b", command)):
        return "That command signals the bridge's own process."
    return None


class Plugin:
    def __init__(self):
        self.ctx = None
        self.dir = ""
        self._lock = threading.RLock()
        self._items: dict = {}
        self._waking: dict = {}          # session id -> {"job": wake turn id, "urgent": bool, "at": ts}
        self._held: list = []            # pings quiet hours kept: [{"chat", "text", "at"}]
        self._procs: dict = {}           # id -> Popen, for jobs this process started and hasn't reaped
        self._spawned: set = set()       # ids this process started (else: adopted after a restart)
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        # The bridge, injectable for the tests.
        self.enqueue = self._enqueue
        self.session = self._session
        self.notify = self._notify
        self.tokens_of = self._tokens_of
        self.live_claude_sid = self._live_claude_sid
        self.send_held = self._send_held

    # ---------------------------------------------------------------- lifecycle
    def start(self, ctx) -> None:
        self.ctx = ctx
        self.dir = ctx.data_dir
        os.makedirs(os.path.join(self.dir, "jobs"), exist_ok=True)
        self._load()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="later", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """The jobs keep running: outliving the bridge is what they are for. The next start adopts them."""
        self._stop.set()
        self._save()

    def _loop(self) -> None:
        while not self._stop.wait(TICK_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - one bad item mustn't end the others
                self._log(f"tick: {e!r}")

    def _log(self, msg: str) -> None:
        if self.ctx:
            self.ctx.log(msg)
        else:
            print(f"[plugin:later] {msg}", file=sys.stderr)

    def _set(self, key: str, default: int) -> int:
        return _int((self.ctx.settings() if self.ctx else {}).get(key), default)

    def _quiet_spec(self) -> str:
        s = self.ctx.settings() if self.ctx else {}
        return str(s.get("LATER_QUIET", "22:00-09:00") if s.get("LATER_QUIET") is not None else "22:00-09:00")

    # ---------------------------------------------------------------- persistence
    def _path(self) -> str:
        return os.path.join(self.dir, "items.json")

    def _load(self) -> None:
        for p in (self._path(), self._path() + ".bak"):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                with self._lock:
                    self._items = {it["id"]: it for it in data.get("items", [])}
                    self._held = list(data.get("held", []))
                return
            except FileNotFoundError:
                continue
            except (OSError, ValueError, KeyError, TypeError) as e:
                self._log(f"{p} unreadable ({e!r}), trying the backup")

    def _save(self) -> None:
        if not self.dir:
            return
        with self._lock:
            data = {"items": sorted(self._items.values(), key=lambda i: i["created"]), "held": self._held}
        tmp = self._path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1, ensure_ascii=False)
            if os.path.exists(self._path()):
                try:
                    with open(self._path(), "rb") as src, open(self._path() + ".bak", "wb") as dst:
                        dst.write(src.read())
                except OSError:
                    pass
            os.replace(tmp, self._path())
        except OSError as e:
            self._log(f"save: {e!r}")

    # ---------------------------------------------------------------- the bridge (real ones)
    def _session(self, sid: str) -> "dict | None":
        from bridge import store
        return store.get_session(sid)

    def _live_claude_sid(self, sid: str) -> str:
        from bridge import runner
        job = runner.live_job(sid)
        return (getattr(job, "session_id", None) or getattr(job, "resume_id", None) or "") if job else ""

    def _enqueue(self, s: dict, text: str, job_id: str) -> None:
        from bridge import queue_manager
        queue_manager.enqueue(s["id"], text=text, prompt=text, images=[], model=None, effort=None,
                              permission_mode=None, width=None, sel=[], surface="later",
                              chat_id=s["chat_id"], project=s.get("project") or "", run_job_id=job_id)

    def _notify(self, s: dict, kind: str, text: str) -> None:
        from bridge import runner
        runner._notify(s["chat_id"], f"⏰ {text} — {runner._session_label(s['id'])}",
                       runner._session_kb(s["chat_id"], s["id"], "Open"), kind=kind, sid=s["id"], body=text[:300])

    def _tokens_of(self, turn_ids: list) -> int:
        """Fresh tokens (in + out + cache writes) the given turns spent. Cache reads are left out:
        they are the cheap part, and a resumed 150k chat reads its whole window every call."""
        if not turn_ids:
            return 0
        from contextlib import closing
        from bridge import store
        with closing(store._connect()) as c:  # noqa: SLF001 - a read the store has no helper for
            q = ",".join("?" * len(turn_ids))
            rows = c.execute(f"SELECT tok_in, tok_out, tok_cache_w FROM turns WHERE id IN ({q})", turn_ids).fetchall()
        return sum(int(r[0] or 0) + int(r[1] or 0) + int(r[2] or 0) for r in rows)

    def _send_held(self, chat: int, text: str) -> None:
        from bridge import config, telegram
        if config.TOKEN:
            telegram.send(chat, text, markdown=False)

    # ---------------------------------------------------------------- who may
    def _refusal(self, s: dict) -> "str | None":
        from bridge import config
        if not s:
            return "No chat is bound to this call."
        origin = str(s.get("origin") or "")
        if origin.startswith(_UNATTENDED) or s.get("job"):
            return "Jobs are only for chats the user started. This run is unattended, so it can't schedule anything."
        if not config.is_owner(s.get("chat_id")):
            return "Only the owner's chats can start jobs."
        if origin == "helper" and s.get("parent_session"):
            return self._refusal(self.session(s["parent_session"]) or {})
        return None

    # ---------------------------------------------------------------- tools
    def tools(self) -> list:
        return TOOLS

    def call_tool(self, s: dict, name: str, a: dict) -> str:
        if name == "job_start":
            return self.job_start(s, a)
        if name == "job_status":
            return self.job_status(s, a)
        if name == "job_cancel":
            return self.job_cancel(s, str(a.get("id") or ""))
        return f"Unknown tool: {name}"

    def job_start(self, s: dict, a: dict) -> str:
        why = self._refusal(s)
        if why:
            return why
        command = str(a.get("command") or "").strip()
        name = " ".join(str(a.get("name") or "").split())[:80]
        then = " ".join(str(a.get("then") or "").split())[:500]
        if not command or not name or not then:
            return "command, name and then are all required: then says what you'll do with the result."
        why = refuses(command)
        if why:
            return why
        cwd = os.path.abspath(os.path.expanduser(str(a.get("cwd") or s.get("work_cwd") or s.get("cwd") or "")))
        if not os.path.isdir(cwd):
            return f"No folder at {cwd}."
        wake = a.get("wake") if a.get("wake") in ("exit", "fail", "never") else "exit"
        tmin = min(max(_int(a.get("timeout_min"), DEFAULT_TIMEOUT_MIN), 1), MAX_TIMEOUT_MIN)
        with self._lock:
            mine = [i for i in self._items.values() if i["session_id"] == s["id"] and i["state"] in OPEN]
            running = [i for i in self._items.values() if i["state"] == "running"]
            cap, run_cap = self._set("LATER_MAX_PER_SESSION", 10), self._set("LATER_MAX_RUNNING", 6)
            if len(mine) >= cap:
                return (f"This chat already has {len(mine)} open jobs (the limit is {cap}): "
                        + ", ".join(f"{i['id']} {i['name']}" for i in mine) + ". Cancel one with job_cancel first.")
            if len(running) >= run_cap:
                return f"{len(running)} jobs are running across all chats (the limit is {run_cap}). Wait for one to end."
            jid = "j" + uuid.uuid4().hex[:8]
            it = {"id": jid, "name": name, "command": command, "then": then, "cwd": cwd,
                  "session_id": s["id"], "chat_id": s["chat_id"], "created": _now(), "wake": wake,
                  "urgent": a.get("urgent") is True, "timeout_s": tmin * 60, "state": "starting",
                  "agent_label": " ".join(str(a.get("agent_label") or "").split())[:120],
                  "claude_sid": self.live_claude_sid(s["id"]) or s.get("claude_session_id") or ""}
            self._items[jid] = it
        try:
            self._spawn(it)
        except OSError as e:
            with self._lock:
                self._items.pop(jid, None)
            self._save()
            return f"It couldn't start: {e}"
        self._save()
        try:
            self.notify(s, "waiting", f"Job started: {name} ({jid}). The chat is woken when it ends.")
        except Exception as e:  # noqa: BLE001 - a ping never fails a start
            self._log(f"notify: {e!r}")
        when = {"exit": "when it ends", "fail": "only if it fails", "never": "never (check it with job_status)"}[wake]
        return (f"Started job {jid} ({name}), pid {it['pid']}, in {cwd}.\nLog: {it['log']}\n"
                f"You'll be woken in this chat {when}, with the exit code and the end of the log. It stops itself "
                f"after {tmin} min. End your turn now with one line saying what you're waiting for; don't poll it.")

    def _spawn(self, it: dict) -> None:
        d = os.path.join(self.dir, "jobs", it["id"])
        os.makedirs(d, exist_ok=True)
        cmd, rc, log = (os.path.join(d, n) for n in ("cmd.sh", "rc", "out.log"))
        with open(cmd, "w", encoding="utf-8") as f:
            f.write(it["command"] + "\n")
        # The wrapper writes the exit code to a file, so a bridge that restarted (and is no
        # longer this process's parent) still learns how it ended.
        wrapper = 'bash "$1"; rc=$?; printf "%s\\n" "$rc" > "$2.tmp" && mv "$2.tmp" "$2"'
        env = {k: v for k, v in os.environ.items() if k not in _SECRETS}
        env["MYSTICAL_LATER_JOB"] = it["id"]
        with open(log, "ab") as out:
            p = subprocess.Popen(["/bin/bash", "-c", wrapper, "later-job", cmd, rc], cwd=it["cwd"],
                                 stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                 start_new_session=True, env=env, close_fds=True)
        with self._lock:
            it.update(pid=p.pid, pid_start=_pid_start(p.pid), started=_now(), state="running", log=log, rc_path=rc)
            self._procs[it["id"]] = p
            self._spawned.add(it["id"])
        threading.Thread(target=self._reap, args=(it["id"], p), name=f"later-{it['id']}", daemon=True).start()

    def _reap(self, jid: str, p) -> None:
        p.wait()
        with self._lock:
            self._procs.pop(jid, None)
        try:
            self.check(jid)
        except Exception as e:  # noqa: BLE001
            self._log(f"reap {jid}: {e!r}")

    def _mine(self, s: dict, jid: str) -> "dict | None":
        it = self._items.get(jid.strip())
        return it if it and it["session_id"] == s["id"] else None

    def job_status(self, s: dict, a: dict) -> str:
        jid = str(a.get("id") or "").strip()
        with self._lock:
            if jid:
                it = self._mine(s, jid)
                if not it:
                    return f"No job {jid} in this chat."
                lines = tail(it.get("log", ""), min(max(_int(a.get("lines"), 20), 1), 200))
                return self._line(it) + f"\nLog: {it.get('log')}\n" + ("\n".join(lines) or "(no output yet)")
            rows = [i for i in self._items.values() if i["session_id"] == s["id"]]
        if not rows:
            return "No jobs in this chat."
        return "\n".join(self._line(i) for i in sorted(rows, key=lambda i: i["created"]))

    def _line(self, it: dict) -> str:
        st = it["state"]
        if st == "running":
            what = f"running {_dur(_now() - it.get('started', it['created']))}"
        else:
            what = f"{st} · {self._how(it)}"
        return f"{it['id']} · {it['name']} · {what}"

    def _how(self, it: dict) -> str:
        o = it.get("outcome")
        took = _dur((it.get("ended") or _now()) - it.get("started", it["created"]))
        if o == "exit":
            return f"exit {it.get('rc')} after {took}"
        if o == "timed_out":
            return f"stopped at its {it['timeout_s'] // 60} min limit"
        if o == "cancelled":
            return "cancelled"
        if o == "lost":
            return f"ended without an exit code after about {took} (killed, or the Mac restarted)"
        return "starting"

    def job_cancel(self, s: "dict | None", jid: str, by: str = "the agent") -> str:
        with self._lock:
            it = self._mine(s, jid) if s is not None else self._items.get(jid.strip())
            if not it:
                return f"No job {jid} here."
            if it["state"] not in OPEN:
                return f"{it['id']} is already {it['state']}."
            was_running = it["state"] == "running"
            it.update(state="cancelled", outcome=it.get("outcome") or "cancelled", ended=it.get("ended") or _now(),
                      note=f"cancelled by {by}")
        if was_running:
            self._kill(it)
        self._save()
        return f"Cancelled {it['id']} ({it['name']})" + (": its process group was stopped." if was_running else ".")

    def _kill(self, it: dict) -> None:
        if not _alive(it):
            return
        try:
            os.killpg(int(it["pid"]), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

        def hard():
            end = _now() + 10
            while _now() < end and _alive(it):
                time.sleep(0.5)
            if _alive(it):
                try:
                    os.killpg(int(it["pid"]), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        threading.Thread(target=hard, daemon=True).start()

    # ---------------------------------------------------------------- watching
    def check(self, jid: str) -> None:
        """Has a running job ended? Reads the rc file first: that is the truth even after a restart."""
        with self._lock:
            it = self._items.get(jid)
            if not it or it["state"] != "running":
                return
        rc_path = it.get("rc_path") or os.path.join(self.dir, "jobs", jid, "rc")
        ended = None
        if os.path.exists(rc_path):
            ended = ("exit", self._rc(rc_path), os.path.getmtime(rc_path))
        elif jid in self._procs or _alive(it):
            if _now() - it.get("started", it["created"]) > it["timeout_s"]:
                self._kill(it)
                ended = ("timed_out", None, _now())
        else:
            time.sleep(0.2)               # the wrapper writes rc just before it exits
            if os.path.exists(rc_path):
                ended = ("exit", self._rc(rc_path), os.path.getmtime(rc_path))
            else:
                ended = ("lost", None, _now())
        if not ended:
            return
        outcome, rc, at = ended
        with self._lock:
            if it["state"] != "running":
                return
            quiet = it["wake"] == "never" or (it["wake"] == "fail" and outcome == "exit" and rc == 0)
            it.update(outcome=outcome, rc=rc, ended=at, state="silent" if quiet else "due",
                      noticed=_now(), bridge_restarted=jid not in self._spawned)
        self._save()

    @staticmethod
    def _rc(path: str) -> "int | None":
        try:
            with open(path, encoding="utf-8") as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _agent_for(self, it: dict) -> "tuple[str, str]":
        """(agent id, description) of the sub-agent whose transcript holds this job's id, if any."""
        csid = it.get("claude_sid")
        if not csid:
            return "", ""
        try:
            from bridge import transcript_jsonl
            main = transcript_jsonl.find_transcript(csid)
        except Exception:  # noqa: BLE001
            main = None
        if not main:
            return "", ""
        d = os.path.join(os.path.dirname(main), csid, "subagents")
        for path in sorted(glob.glob(os.path.join(d, "agent-*.jsonl")), key=os.path.getmtime, reverse=True):
            if os.path.getmtime(path) < it["created"] - 60:
                break
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    if it["id"] not in f.read():
                        continue
            except OSError:
                continue
            aid = os.path.basename(path)[len("agent-"):-len(".jsonl")]
            desc = ""
            try:
                with open(path[:-len(".jsonl")] + ".meta.json", encoding="utf-8") as f:
                    desc = str(json.load(f).get("description") or "")
            except (OSError, ValueError):
                pass
            return aid, desc
        return "", ""

    def message(self, it: dict) -> str:
        noticed = it.get("noticed") or _now()
        late = f", noticed {_clock(noticed)} after a bridge restart" if it.get("bridge_restarted") and noticed - it["ended"] > 30 else ""
        head = f"⏰ Later · job {it['id']} \"{it['name']}\" {'finished' if it.get('outcome') == 'exit' else 'ended'}: {self._how(it)} (ended {_clock(it['ended'])}{late})."
        lines = tail(it.get("log", ""))
        body = "Last lines of its log:\n" + "\n".join("  " + ln for ln in lines) if lines else "It printed nothing."
        aid, desc = self._agent_for(it)
        who = ""
        if aid or it.get("agent_label"):
            label = desc or it.get("agent_label") or "a sub-agent"
            who = (f"\nIt was started by the sub-agent \"{label}\"" + (f" (id {aid}). Resume that agent with SendMessage "
                   f"to {aid}, passing it this result, or do the follow-up yourself." if aid else ". Do its follow-up."))
        return (f"{head}\n{body}\nFull log: {it.get('log')}\nYou said you'd: {it['then']}{who}\n"
                f"(Woken by the bridge, not the user. Carry on.)")

    # ---------------------------------------------------------------- waking
    def _wake_ids(self, sid: str, since: float) -> list:
        return sorted({i["wake_job"] for i in self._items.values()
                       if i["session_id"] == sid and i.get("wake_job") and i.get("delivered", 0) >= since})

    def deliver_due(self) -> None:
        now = _now()
        with self._lock:
            due = [i for i in self._items.values() if i["state"] in ("due", "held")]
        by_session: dict = {}
        for it in due:
            if it["state"] == "held" and it.get("held_until", 0) > now and not it.get("released"):
                continue
            by_session.setdefault(it["session_id"], []).append(it)
        for sid, items in by_session.items():
            seen = [i.get("noticed", 0) for i in items if i["state"] == "due"]
            # Wait until the chat's jobs have stopped ending for COALESCE_S (at most 30 s), so they wake it once.
            if seen and now - max(seen) < COALESCE_S and now - min(seen) < 30:
                continue
            s = self.session(sid)
            if not s or s.get("archived"):
                with self._lock:
                    for it in items:
                        it.update(state="dropped", note="its chat is gone")
                self._save()
                continue
            released = any(i.get("released") for i in items)
            hour_cap, tok_cap = self._set("LATER_WAKES_PER_HOUR", 6), self._set("LATER_DAILY_TOKENS", 2_000_000)
            woke = self._wake_ids(sid, now - 3600)
            spent = self.tokens_of(self._wake_ids(sid, _day_start(now)))
            held = None
            if not released and len(woke) >= hour_cap:
                first = min(i["delivered"] for i in self._items.values() if i.get("wake_job") in woke)
                held = (first + 3600, f"held: this chat had {len(woke)} wake-ups in the last hour (limit {hour_cap})")
            elif not released and spent >= tok_cap:
                held = (_day_start(now) + 86400 + 60, f"held: wake-ups here used {spent:,} tokens today (limit {tok_cap:,})")
            if held:
                fresh = [i for i in items if i["state"] != "held"]
                with self._lock:
                    for it in items:
                        it.update(state="held", held_until=held[0], note=held[1] + f"; goes {time.strftime('%a %H:%M', time.localtime(held[0]))} or /later release {it['id']}")
                self._save()
                if fresh:
                    self.notify(s, "parked", f"Wake-up {held[1]} — " + ", ".join(i["name"] for i in fresh) + ". /later release to send it now.")
                continue
            wake_job = uuid.uuid4().hex
            text = "\n\n".join(self.message(i) for i in items)
            with self._lock:
                for it in items:
                    it.update(state="delivered", delivered=now, wake_job=wake_job, released=False)
                self._waking[sid] = {"job": wake_job, "urgent": any(i.get("urgent") for i in items), "at": now}
            self._save()
            try:
                self.enqueue(s, text, wake_job)
            except Exception as e:  # noqa: BLE001
                self._log(f"enqueue {sid}: {e!r}")
                with self._lock:
                    for it in items:
                        it.update(state="due", wake_job=None)
                self._save()

    def release(self, jid: str) -> str:
        with self._lock:
            it = self._items.get(jid.strip())
            if not it or it["state"] != "held":
                return f"{jid} isn't held."
            it.update(released=True, held_until=0)
        self._save()
        self.deliver_due()
        return f"Released {jid}."

    # ---------------------------------------------------------------- quiet hours
    def holds_ping(self, sid: str, kind: str, text: str) -> bool:
        """runner._notify asks before a Telegram ping: a wake-up's ping in quiet hours waits for morning."""
        w = self._waking.get(sid or "")
        if not w or w.get("urgent") or not in_quiet(self._quiet_spec()):
            return False
        try:
            from bridge import notices
            if notices.tier(kind) == "needs":
                return False
        except Exception:  # noqa: BLE001
            pass
        s = self.session(sid) or {}
        with self._lock:
            self._held.append({"chat": s.get("chat_id"), "text": text[:500], "at": _now()})
        self._save()
        return True

    def _flush_held(self) -> None:
        if not self._held or in_quiet(self._quiet_spec()):
            return
        with self._lock:
            held, self._held = self._held, []
        self._save()
        for chat in {h["chat"] for h in held if h.get("chat")}:
            rows = [h for h in held if h["chat"] == chat]
            text = "🌙 While it was quiet (Later wake-ups):\n" + "\n".join(f"{_clock(h['at'])} {h['text']}" for h in rows[-20:])
            try:
                self.send_held(chat, text)
            except Exception as e:  # noqa: BLE001
                self._log(f"held pings: {e!r}")

    def _settle_waking(self) -> None:
        """A wake-up turn's end ping has gone (or it never ran): stop holding for that chat."""
        from bridge import runner, state
        now = _now()
        for sid, w in list(self._waking.items()):
            job = runner.get_job(w["job"])
            done = job is not None and job.exited.is_set()
            stale = job is None and now - w["at"] > 6 * 3600
            if (done and not state.is_running(sid) and now - w.get("done_at", now) > 30) or stale:
                self._waking.pop(sid, None)
            elif done and "done_at" not in w:
                w["done_at"] = now

    # ---------------------------------------------------------------- tick
    def tick(self) -> None:
        with self._lock:
            running = [i["id"] for i in self._items.values() if i["state"] == "running"]
        for jid in running:
            self.check(jid)
        self.deliver_due()
        try:
            self._settle_waking()
        except Exception as e:  # noqa: BLE001
            self._log(f"settle: {e!r}")
        self._flush_held()
        self._prune()

    def _prune(self) -> None:
        cut = _now() - KEEP_S
        with self._lock:
            old = [k for k, i in self._items.items() if i["state"] not in OPEN and i.get("ended", i["created"]) < cut]
            for k in old:
                self._items.pop(k)
        if old:
            self._save()

    # ---------------------------------------------------------------- surfaces
    def telegram(self) -> dict:
        return {"/later": self._tg}

    def _tg(self, chat_id: int, rest: str) -> str:
        verb, _, arg = (rest or "").strip().partition(" ")
        if verb == "cancel" and arg:
            return self.job_cancel(None, arg, by="you")
        if verb == "release" and arg:
            return self.release(arg)
        return self.listing()

    def listing(self) -> str:
        with self._lock:
            rows = sorted(self._items.values(), key=lambda i: i["created"])
        live = [i for i in rows if i["state"] in OPEN]
        done = [i for i in rows if i["state"] not in OPEN][-5:]
        if not rows:
            return "⏰ Nothing pending."
        out = ["⏰ Pending" if live else "⏰ Nothing pending."]
        for i in live:
            s = self.session(i["session_id"]) or {}
            note = f" · {i['note']}" if i.get("note") and i["state"] == "held" else ""
            out.append(f"• {self._line(i)} · in {s.get('title') or i['session_id'][:8]}{note}")
        if done:
            out.append("Recent")
            out += [f"· {self._line(i)}" for i in done]
        out.append("/later cancel <id> · /later release <id>")
        return "\n".join(out)

    def routes(self) -> dict:
        return {("GET", "/plugin/later/state"): lambda req: ({"items": sorted(self._items.values(), key=lambda i: i["created"]),
                                                              "held_pings": len(self._held)}, 200),
                ("POST", "/plugin/later/cancel"): lambda req: ({"text": self.job_cancel(None, str((req.get("body") or {}).get("id") or ""), by="you")}, 200),
                ("POST", "/plugin/later/release"): lambda req: ({"text": self.release(str((req.get("body") or {}).get("id") or ""))}, 200)}

    def status(self) -> dict:
        with self._lock:
            n_run = sum(1 for i in self._items.values() if i["state"] == "running")
            n_wait = sum(1 for i in self._items.values() if i["state"] in ("due", "held"))
        return {"line": f"{n_run} running · {n_wait} waiting to wake" if n_run or n_wait else "idle", "ok": True}
