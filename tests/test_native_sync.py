"""bridge/native_sync.py: a bridge session catches up with turns its Claude
transcript gained in a terminal. conftest pins the DB and base before config loads.
"""

import json
import os
import tempfile
import time
import uuid
from contextlib import closing

import pytest

from bridge import native_sync, store, transcript_jsonl

store.init()
OWNER = 555
T0 = 1_790_000_000.0


def _iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + ".000Z"


_RUN = uuid.uuid4().hex[:6]


def _user(uid, text, ts, **extra):
    uid = f"{_RUN}-{text[:12]}-{uid}"            # turns.id is global across the suite's one DB
    return {"type": "user", "uuid": uid, "timestamp": _iso(ts),
            "message": {"role": "user", "content": text}, **extra}


def _asst(text, ts):
    return {"type": "assistant", "uuid": uuid.uuid4().hex, "timestamp": _iso(ts),
            "message": {"role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 1, "output_tokens": 2}}}


_made: list = []


@pytest.fixture(autouse=True)
def _leave_no_turns_behind():
    """The suite shares one DB and store.prompt_history reads every session's
    turns: drop ours, some of which are stamped in the future on purpose."""
    yield
    with closing(store._connect()) as c:
        for sid in _made:
            c.execute("DELETE FROM turns WHERE session_id=?", (sid,))
            c.execute("DELETE FROM events WHERE session_id=?", (sid,))
    _made.clear()


class _Session:
    """A bridge-owned session with its own JSONL under a private projects dir."""

    def __init__(self, monkeypatch):
        global _RUN
        _RUN = uuid.uuid4().hex[:6]
        root = tempfile.mkdtemp()
        monkeypatch.setattr(transcript_jsonl, "PROJECTS_DIR", root)
        monkeypatch.setattr(native_sync, "_MIN_INTERVAL", 0.0)
        self.csid = str(uuid.uuid4())
        os.makedirs(os.path.join(root, "p"), exist_ok=True)
        self.path = os.path.join(root, "p", self.csid + ".jsonl")
        open(self.path, "w").close()
        s = store.create_session(OWNER, "/proj", origin="bridge", cwd="/tmp")
        store.set_claude_session_id(s["id"], self.csid)
        self.id = s["id"]
        _made.append(self.id)

    def write(self, *recs):
        with open(self.path, "a") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")

    def adopt(self):
        data = transcript_jsonl.parse_jsonl(self.path)
        store.import_transcript(self.id, data["turns"], data["events"])

    def sync(self, **kw):
        return native_sync.catch_up(store.get_session(self.id), **kw)

    def prompts(self):
        return [t["prompt"] for t in store.transcript(self.id)["turns"]]

    def texts(self):
        return [e["text"] for e in store.transcript(self.id)["events"] if e.get("type") == "text"]


def test_appends_turns_added_outside_the_bridge(monkeypatch):
    s = _Session(monkeypatch)
    s.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    s.adopt()
    s.write(_user("u-2", "second", T0 + 100), _asst("two", T0 + 101),
            _user("u-3", "third", T0 + 200), _asst("three", T0 + 201))
    assert s.sync() == 2
    assert s.prompts() == ["first", "second", "third"]
    assert s.texts() == ["one", "two", "three"]
    seqs = [e["seq"] for e in store.transcript(s.id)["events"]]
    assert seqs == sorted(set(seqs))


def test_is_idempotent_and_cheap_when_unchanged(monkeypatch):
    s = _Session(monkeypatch)
    s.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    s.adopt()
    s.write(_user("u-2", "second", T0 + 100), _asst("two", T0 + 101))
    assert s.sync() == 1
    assert s.sync() == 0
    monkeypatch.setattr(native_sync, "_sync", lambda *a: (_ for _ in ()).throw(AssertionError("re-read")))
    assert s.sync() == 0
    assert s.prompts() == ["first", "second"]


def test_skips_the_bridges_own_turn(monkeypatch):
    s = _Session(monkeypatch)
    s.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    s.adopt()
    job = uuid.uuid4().hex                       # a bridge-run turn: job id, not the record uuid
    store.start_turn(s.id, job, "from the bridge", [])
    store.append_event(s.id, job, {"type": "text", "text": "bridge answer"})
    store.finish_turn(job, "done", 0.0, 5)
    started = [t for t in store.transcript(s.id)["turns"] if t["id"] == job][0]["started"]
    s.write(_user("u-b", "from the bridge", started + 1), _asst("bridge answer", started + 3),
            _user("u-4", "from the terminal", started + 500), _asst("terminal answer", started + 501))
    assert s.sync() == 1
    assert s.prompts() == ["first", "from the bridge", "from the terminal"]
    assert s.texts() == ["one", "bridge answer", "terminal answer"]


def test_tops_up_a_turn_that_was_still_being_written(monkeypatch):
    s = _Session(monkeypatch)
    s.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    s.adopt()
    s.write(_user("u-2", "second", T0 + 100), _asst("part a", T0 + 101))
    assert s.sync() == 1
    s.write(_asst("part b", T0 + 102))
    assert s.sync() == 0                         # no new turn, one more event on the same one
    assert s.prompts() == ["first", "second"]
    assert s.texts() == ["one", "part a", "part b"]


def test_ignores_a_half_written_line_and_compact_summaries(monkeypatch):
    s = _Session(monkeypatch)
    s.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    s.adopt()
    s.write(_user("u-c", "This session is being continued…", T0 + 50, isCompactSummary=True),
            _user("u-2", "second", T0 + 100), _asst("two", T0 + 101))
    with open(s.path, "a") as fh:
        fh.write('{"type":"user","uuid":"u-9","timestamp":"')     # the CLI is mid-write
    assert s.sync() == 1
    assert s.prompts() == ["first", "second"]


def test_leaves_native_and_running_sessions_alone(monkeypatch):
    s = _Session(monkeypatch)
    s.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    assert s.sync() == 0                         # nothing journaled yet: the JSONL fallback renders it
    s.adopt()
    s.write(_user("u-2", "second", T0 + 100), _asst("two", T0 + 101))
    assert native_sync.catch_up({**store.get_session(s.id), "origin": "terminal"}) == 0
    job = uuid.uuid4().hex
    store.start_turn(s.id, job, "running", [])
    assert s.sync() == 0                         # our own turn is writing the file
    assert s.prompts() == ["first", "running"]


def test_first_sync_bisects_a_large_file(monkeypatch):
    monkeypatch.setattr(native_sync, "_BISECT_STOP", 2048)
    monkeypatch.setattr(native_sync, "_BISECT_SLACK", 5.0)
    s = _Session(monkeypatch)
    recs = []
    for i in range(200):
        recs += [_user(f"u-{i}", f"p{i}", T0 + i * 60), _asst("x" * 300, T0 + i * 60 + 1)]
    s.write(*recs)
    s.adopt()
    s.write(_user("u-new", "after", T0 + 200 * 60), _asst("new", T0 + 200 * 60 + 1))
    seen = []
    real = native_sync._slice
    monkeypatch.setattr(native_sync, "_slice", lambda p, start, c: (seen.append(start), real(p, start, c))[1])
    assert s.sync(wait=True) == 1
    assert seen[0] > os.path.getsize(s.path) // 2      # it did not start from byte 0
    assert s.prompts()[-2:] == ["p199", "after"]
    assert len(s.prompts()) == 201


def test_a_turn_journaled_under_another_session_is_not_stolen(monkeypatch):
    a = _Session(monkeypatch)
    a.write(_user("u-1", "first", T0), _asst("one", T0 + 1))
    a.adopt()
    shared = _user("u-2", "forked away", T0 + 100)
    other = store.create_session(OWNER, "/proj", origin="bridge", cwd="/tmp")
    _made.append(other["id"])
    store.import_transcript(other["id"], [{"id": shared["uuid"], "seq": 0, "prompt": "forked away",
                                           "started": T0 + 100}], [])
    a.write(shared, _asst("two", T0 + 101), _user("u-3", "mine", T0 + 200), _asst("three", T0 + 201))
    assert a.sync() == 1
    assert a.prompts() == ["first", "mine"]
