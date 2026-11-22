"""plugins/later: bridge-owned jobs that wake the session that started them."""

import importlib.util
import json
import os
import tempfile
import time

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins", "later", "plugin.py")


def _mod():
    spec = importlib.util.spec_from_file_location("later_plugin_under_test", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Ctx:
    def __init__(self, d, settings=None):
        self.data_dir = d
        self._s = settings or {}

    def settings(self):
        return self._s

    def log(self, msg):
        pass


SESSION = {"id": "s1", "chat_id": 555, "origin": "dashboard", "cwd": None, "title": "release chat"}


@pytest.fixture()
def P():
    m = _mod()
    d = tempfile.mkdtemp()
    cwd = tempfile.mkdtemp()
    p = m.Plugin()
    p.ctx = Ctx(d, {"LATER_QUIET": ""})
    p.dir = d
    os.makedirs(os.path.join(d, "jobs"), exist_ok=True)
    p.woke, p.pings, p.spent = [], [], {}
    sessions = {"s1": {**SESSION, "cwd": cwd}, "auto": {**SESSION, "id": "auto", "origin": "automation", "cwd": cwd}}
    p.session = lambda sid: sessions.get(sid)
    p.enqueue = lambda s, text, job: p.woke.append((s["id"], text, job))
    p.notify = lambda s, kind, text: p.pings.append((kind, text))
    p.tokens_of = lambda ids: sum(p.spent.get(i, 0) for i in ids)
    p.live_claude_sid = lambda sid: ""
    p.m, p.cwd, p.sessions = m, cwd, sessions
    m.COALESCE_S = 0
    yield p
    for it in list(p._items.values()):
        if it["state"] == "running":
            p.job_cancel(None, it["id"])


def _wait(p, jid, states=("due", "silent", "delivered", "held"), t=10):
    end = time.time() + t
    while time.time() < end:
        p.tick()
        if p._items[jid]["state"] in states:
            return p._items[jid]
        time.sleep(0.1)
    raise AssertionError(p._items[jid])


def _start(p, cmd, **kw):
    out = p.job_start(p.sessions["s1"], {"command": cmd, "name": kw.pop("name", "build"), "then": "publish it", **kw})
    assert out.startswith("Started job "), out
    return out.split()[2]


def test_job_ends_and_wakes_same_session_with_exit_code_and_tail(P):
    jid = _start(P, "echo compiling; echo 'artifact ok'; exit 3")
    it = _wait(P, jid, ("delivered",))
    assert it["rc"] == 3 and it["outcome"] == "exit"
    (sid, text, job), = P.woke
    assert sid == "s1" and "exit 3" in text and "artifact ok" in text and "publish it" in text
    assert job == it["wake_job"]


def test_job_runs_in_its_own_process_group_and_log_file(P):
    jid = _start(P, "sleep 30")
    it = P._items[jid]
    assert os.getpgid(it["pid"]) == it["pid"] != os.getpgrp()
    assert os.path.isfile(it["log"])
    P.job_cancel(P.sessions["s1"], jid)


def test_adopted_after_restart_by_rc_file(P):
    jid = _start(P, "sleep 1; echo done-late; exit 0")
    P._save()
    p2 = P.m.Plugin()                    # a restarted bridge: a fresh instance, the same data dir
    for k in ("session", "enqueue", "notify", "tokens_of", "live_claude_sid"):
        setattr(p2, k, getattr(P, k))
    p2.ctx, p2.dir = P.ctx, P.dir
    p2._load()
    P._stop.set()
    P._items.clear()                     # the old instance stops looking
    assert p2._items[jid]["state"] == "running"
    it = _wait(p2, jid, ("delivered",))
    assert it["bridge_restarted"] and it["rc"] == 0
    assert "done-late" in P.woke[-1][1]


def test_lost_when_pid_gone_without_rc(P):
    jid = _start(P, "sleep 30")
    it = P._items[jid]
    os.killpg(it["pid"], 9)
    P._procs.clear()
    it = _wait(P, jid, ("delivered",))
    assert it["outcome"] == "lost" and "without an exit code" in P.woke[-1][1]


def test_pid_reuse_is_not_the_job(P):
    jid = _start(P, "sleep 30")
    it = P._items[jid]
    assert P.m._alive(it)
    assert not P.m._alive({**it, "pid_start": "Mon Jan  1 00:00:00 2001"})
    P.job_cancel(P.sessions["s1"], jid)


def test_cancel_stops_group_and_never_wakes(P):
    jid = _start(P, "sleep 60")
    pid = P._items[jid]["pid"]
    assert "Cancelled" in P.job_cancel(P.sessions["s1"], jid)
    time.sleep(0.5)
    for _ in range(20):
        P.tick()
    assert P._items[jid]["state"] == "cancelled" and not P.woke
    with pytest.raises(ProcessLookupError):
        for _ in range(40):
            os.kill(pid, 0)
            time.sleep(0.25)


def test_wake_fail_is_silent_on_success(P):
    jid = _start(P, "true", wake="fail")
    assert _wait(P, jid, ("silent",))["state"] == "silent" and not P.woke


def test_caps_items_per_session(P):
    P.ctx._s["LATER_MAX_PER_SESSION"] = "2"
    a, b = _start(P, "sleep 30"), _start(P, "sleep 30")
    out = P.job_start(P.sessions["s1"], {"command": "true", "name": "c", "then": "x"})
    assert "limit is 2" in out and a in out
    P.job_cancel(None, a)
    assert _start(P, "sleep 30")


def test_caps_wakes_per_hour_hold_then_release(P):
    P.ctx._s["LATER_WAKES_PER_HOUR"] = "1"
    j1 = _start(P, "true", name="one")
    _wait(P, j1, ("delivered",))
    j2 = _start(P, "true", name="two")
    it = _wait(P, j2, ("held",))
    assert "wake-ups in the last hour" in it["note"] and len(P.woke) == 1
    assert any(k == "parked" for k, _ in P.pings)
    P.release(j2)
    assert P._items[j2]["state"] == "delivered" and len(P.woke) == 2


def test_caps_daily_tokens(P):
    P.ctx._s["LATER_DAILY_TOKENS"] = "1000"
    j1 = _start(P, "true", name="one")
    it = _wait(P, j1, ("delivered",))
    P.spent[it["wake_job"]] = 5000
    j2 = _start(P, "true", name="two")
    assert "tokens today" in _wait(P, j2, ("held",))["note"]


def test_refuses_bridge_restart_commands(P):
    for cmd in ("mystical restart", "launchctl kickstart -k gui/501/cloud.ainurhq.mystical.bridge",
                "pkill -f claude_telegram_bridge", f"kill -TERM {os.getpid()}"):
        out = P.job_start(P.sessions["s1"], {"command": cmd, "name": "x", "then": "y"})
        assert not out.startswith("Started"), cmd


def test_only_owner_started_sessions(P):
    out = P.job_start(P.sessions["auto"], {"command": "true", "name": "x", "then": "y"})
    assert "unattended" in out


def test_job_env_has_no_bridge_tokens(P, monkeypatch):
    monkeypatch.setenv("MYSTICAL_DASH_TOKEN", "secret-dash")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-tg")
    jid = _start(P, "env")
    _wait(P, jid, ("delivered",))
    with open(P._items[jid]["log"]) as f:
        log = f.read()
    assert "secret-dash" not in log and "secret-tg" not in log and f"MYSTICAL_LATER_JOB={jid}" in log


def test_coalesces_two_jobs_into_one_wake(P):
    P.m.COALESCE_S = 2
    a, b = _start(P, "true", name="a"), _start(P, "sleep 1", name="b")
    _wait(P, b, ("delivered",))
    assert len(P.woke) == 1 and '"a"' in P.woke[0][1] and '"b"' in P.woke[0][1]


def test_quiet_hours_hold_wake_pings_unless_urgent(P):
    P.ctx._s["LATER_QUIET"] = "00:00-23:59"
    P._waking["s1"] = {"job": "w", "urgent": False, "at": time.time()}
    assert P.holds_ping("s1", "done", "✅ finished")
    assert not P.holds_ping("s1", "approve", "needs you")
    P._waking["s1"]["urgent"] = True
    assert not P.holds_ping("s1", "done", "✅ finished")
    assert not P.holds_ping("other", "done", "x")
    sent = []
    P.send_held = lambda chat, text: sent.append((chat, text))
    P.ctx._s["LATER_QUIET"] = ""
    P._flush_held()
    assert sent and sent[0][0] == 555 and "finished" in sent[0][1]


def test_message_names_sub_agent(P, monkeypatch):
    jid = _start(P, "true", agent_label="Desktop 1.2.2 release")
    it = _wait(P, jid, ("delivered",))
    assert "Desktop 1.2.2 release" in P.woke[0][1]
    root = tempfile.mkdtemp()
    sub = os.path.join(root, "csid", "subagents")
    os.makedirs(sub)
    with open(os.path.join(sub, "agent-abc123.jsonl"), "w") as f:
        f.write(json.dumps({"content": f"Started job {jid}"}))
    with open(os.path.join(sub, "agent-abc123.meta.json"), "w") as f:
        json.dump({"description": "Ship desktop"}, f)
    from bridge import transcript_jsonl
    monkeypatch.setattr(transcript_jsonl, "find_transcript", lambda sid: os.path.join(root, "csid.jsonl"))
    it["claude_sid"] = "csid"
    msg = P.message(it)
    assert "SendMessage to abc123" in msg and "Ship desktop" in msg


def test_persistence_survives_corrupt_file(P):
    jid = _start(P, "true")
    P._save()
    P._save()                             # a second save leaves a .bak of the first
    with open(P._path(), "w") as f:
        f.write("{not json")
    p2 = P.m.Plugin()
    p2.ctx, p2.dir = P.ctx, P.dir
    p2._load()
    assert jid in p2._items


def test_manifest_validates():
    from bridge import plugins_ext
    d = os.path.dirname(_PATH)
    with open(os.path.join(d, "plugin.json")) as f:
        m = plugins_ext._validate(json.load(f), d, "bundled")
    assert m["id"] == "later" and "later" in m["telegram"]


def test_telegram_listing_cancel_release(P):
    jid = _start(P, "sleep 30", name="long build")
    out = P._tg(555, "")
    assert "Pending" in out and "long build" in out and "release chat" in out
    assert "Cancelled" in P._tg(555, f"cancel {jid}")
    assert "Recent" in P._tg(555, "") and "cancelled" in P._tg(555, "")
    assert "isn't held" in P._tg(555, f"release {jid}")


def test_owner_message_does_not_drop_a_job(P):
    """Nothing in the plugin listens to turn starts: a job ends by exiting or by cancel only."""
    jid = _start(P, "sleep 1; echo ok")
    assert not hasattr(P, "on_turn_start")
    assert _wait(P, jid, ("delivered",))["outcome"] == "exit"
