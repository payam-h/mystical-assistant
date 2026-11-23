"""Catch a bridge session up with turns its Claude transcript gained elsewhere.

A session the bridge owns renders from the store. `runner._adopt_native` copies
the JSONL into the store exactly once, on the first continuation, and after that
the store only learns the turns the bridge runs itself. But the same session can
still be resumed from a terminal (`claude --resume`, Orven's Cockpit tabs): those
turns land in ~/.claude/projects/<enc>/<sid>.jsonl and nowhere else, so every
surface shows the conversation frozen at the last bridge turn while the real one
is weeks ahead.

`catch_up(session)` closes that gap. It reads only the part of the JSONL the
store has not seen, parses it with the same parser adoption uses, and appends the
turns that are not the bridge's own.

Why it is shaped this way:

- **Only the tail is read.** These files reach hundreds of MB. The store keeps,
  per session, the byte offset of the last prompt it has synced (settings key
  `native_sync:<sid>`), so a later sync starts there. The first sync has no
  offset and bisects the file by timestamp instead of scanning it.
- **The tail is copied to a temp file and handed to `transcript_jsonl._parse_full`**
  rather than re-implementing the parse: an appended turn must look exactly like
  an adopted one. A temp file because the parser takes a path, and /dev/fd tricks
  re-open from byte 0 on Linux.
- **Telling our own turns apart.** A bridge-run turn is also in the JSONL. Its
  store id is a job id (uuid4().hex, no dashes) and never matches the record's
  uuid, so it is recognised by time: a parsed turn that starts inside a bridge
  turn's [started, end] window is that turn (or a steer inside it) and is
  skipped. Imported turns carry the record uuid as their id and match by id.
- **A turn still being written is topped up, not duplicated.** The saved offset
  points at the *start* of the last prompt seen, so that turn is re-parsed each
  time; events beyond the count already stored for it are appended. The file is
  append-only, so the stored prefix never changes.
- **One short transaction per turn**, seq taken as MAX+1 inside it, the same way
  `store.append_event` does. A first sync can be a thousand turns; one long
  write transaction would push a running turn's own appends past busy_timeout.
- **A big backlog runs on a thread** unless the caller needs it done (`wait`),
  so opening the session is not held for the parse. A send waits, because a
  bridge turn stored before the backlog would sit above older work forever.

Not handled, on purpose: a sync that only happens *after* a bridge turn was
stored on top of unsynced native turns appends them below it (seq is append-only
and surfaces order by it). The send paths sync first, so this needs a surface
that does not call `catch_up` (Telegram) to hit.
ponytail: reorder by `started` if that ever shows up in practice.

Stdlib only. Everything here is the fork's; upstream files only call catch_up().
"""

import json
import os
import re
import sys
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone

from bridge import store, transcript_jsonl

_KEY = "native_sync:"
_BG_BYTES = 16 * 1024 * 1024      # a tail larger than this parses off the request thread
_MIN_INTERVAL = 3.0               # per session, between two looks at the file
_BISECT_STOP = 4 * 1024 * 1024    # bisect until the window is this small, then read lines
_BISECT_SLACK = 600.0             # seconds of margin under the cutoff while bisecting
_WINDOW_SLACK = 2.0               # seconds either side of a bridge turn's window
_TS_RE = re.compile(rb'"timestamp":\s*"([^"]+)"')
_USER_RE = re.compile(rb'"type":\s*"user"')         # the CLI writes compact JSON; don't depend on it

_lock = threading.Lock()
_session_locks: "dict[str, threading.Lock]" = {}
_last_look: "dict[str, float]" = {}
_threads: "dict[str, threading.Thread]" = {}


def _session_lock(sid: str) -> threading.Lock:
    with _lock:
        return _session_locks.setdefault(sid, threading.Lock())


def _state(sid: str) -> dict:
    return store.parse_json(store.get_setting(_KEY + sid))


def _is_bridge_turn(turn_id: str) -> bool:
    """A job id (uuid4().hex); an imported turn carries the record's dashed uuid."""
    return len(turn_id) == 32 and "-" not in turn_id


def _iso(ts: float) -> bytes:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S").encode()


def _bisect(fh, size: int, cutoff: float) -> int:
    """A byte offset, on a line boundary, safely before the first record at
    `cutoff`. Record timestamps only grow down the file, near enough; the slack
    covers the rest and the line pass after this applies the exact test."""
    want = _iso(max(0.0, cutoff - _BISECT_SLACK))
    lo, hi = 0, size
    while hi - lo > _BISECT_STOP:
        mid = (lo + hi) // 2
        fh.seek(mid)
        fh.readline()                               # the rest of a cut line
        stamp = None
        while stamp is None and fh.tell() < hi:
            m = _TS_RE.search(fh.readline())
            stamp = m.group(1)[:19] if m else None
        if stamp is not None and stamp < want:
            lo = mid
        else:
            hi = mid
    if lo == 0:
        return 0
    fh.seek(lo)
    fh.readline()
    return fh.tell()


def _user_record(line: bytes) -> "dict | None":
    """The decoded record if this line is a top-level user record. Only these
    lines are decoded: the cheap byte test keeps the pass over a big tail fast."""
    if not _USER_RE.search(line):
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) and rec.get("type") == "user" else None


def _opens_turn(rec: dict) -> bool:
    """Whether transcript_jsonl's parser opens a turn on this user record."""
    if rec.get("isSidechain") or rec.get("isMeta"):
        return False
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else {}
    return not transcript_jsonl._is_tool_result(msg.get("content"))


def _slice(path: str, start: int, cutoff: float) -> "tuple[str | None, dict, int]":
    """Copy the unseen tail to a temp file. It begins on the first prompt record
    at or after `cutoff` (so the parse never opens on the middle of a turn) and
    ends on the last complete line. Returns (temp path or None, {record uuid:
    byte offset of its line}, byte offset just past what was consumed)."""
    offsets: dict = {}
    end = start
    out = None
    tmp = None
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            while True:
                pos = fh.tell()
                line = fh.readline()
                if not line or not line.endswith(b"\n"):
                    break                           # EOF, or a line still being written
                end = fh.tell()
                rec = _user_record(line)
                if rec is not None and rec.get("isCompactSummary"):
                    continue                        # the CLI's summary, never something the user said
                if rec is not None and rec.get("uuid") and _opens_turn(rec):
                    if out is None and transcript_jsonl._ts(rec) < cutoff:
                        continue
                    offsets[rec["uuid"]] = pos
                    if out is None:
                        fd, tmp = tempfile.mkstemp(prefix="mystical-sync-", suffix=".jsonl")
                        out = os.fdopen(fd, "wb")
                if out is not None:
                    out.write(line)
    finally:
        if out is not None:
            out.close()
    return tmp, offsets, end


def _bridge_windows(c, sid: str) -> list:
    """[started, end] of every turn the bridge ran itself. The end is the later
    of started+elapsed and the turn's last stored event; a running turn has no
    end yet."""
    last_ev = {r["turn_id"]: r["t"] for r in c.execute(
        "SELECT turn_id, MAX(ts) t FROM events WHERE session_id=? GROUP BY turn_id", (sid,))}
    out = []
    for r in c.execute("SELECT id, started, elapsed, status FROM turns WHERE session_id=?", (sid,)):
        if not _is_bridge_turn(r["id"]):
            continue
        started = r["started"] or 0.0
        if r["status"] == "running":
            end = float("inf")
        else:
            end = max(started + (r["elapsed"] or 0), last_ev.get(r["id"]) or 0.0)
        out.append((started - _WINDOW_SLACK, end + _WINDOW_SLACK))
    return out


def _store_turn(sid: str, turn: dict, events: list, topup: bool = True) -> int:
    """Insert one new turn, or top up one already stored. Returns events added.
    `topup` False leaves a stored turn alone: on a first sync the stored copy may
    come from an older parser whose event count for the same records differs."""
    with closing(store._connect()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            have = c.execute("SELECT session_id FROM turns WHERE id=?", (turn["id"],)).fetchone()
            if have and have["session_id"] != sid:
                c.execute("COMMIT")                  # turns.id is global: a fork's copied records
                return 0                             # belong to the session they were journaled under
            tok = turn.get("tokens") or {}
            if have:
                done = c.execute("SELECT COUNT(*) n FROM events WHERE session_id=? AND turn_id=?",
                                 (sid, turn["id"])).fetchone()["n"]
                events = events[done:] if topup else []
                if events:
                    c.execute("UPDATE turns SET cost=?, elapsed=?, tok_in=?, tok_out=?, "
                              "tok_cache_w=?, tok_cache_r=? WHERE id=? AND session_id=?",
                              (turn.get("cost"), turn.get("elapsed"), tok.get("in"), tok.get("out"),
                               tok.get("cache_w"), tok.get("cache_r"), turn["id"], sid))
            else:
                seq = c.execute("SELECT COALESCE(MAX(seq),-1)+1 n FROM turns WHERE session_id=?",
                                (sid,)).fetchone()["n"]
                c.execute(
                    "INSERT INTO turns(id,session_id,seq,prompt,attachments,status,cost,"
                    "elapsed,started,model,tok_in,tok_out,tok_cache_w,tok_cache_r) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (turn["id"], sid, seq, turn.get("prompt", ""), "[]", "done",
                     turn.get("cost"), turn.get("elapsed"), turn.get("started") or time.time(),
                     turn.get("model"), tok.get("in"), tok.get("out"),
                     tok.get("cache_w"), tok.get("cache_r")))
            seq = c.execute("SELECT COALESCE(MAX(seq),-1)+1 n FROM events WHERE session_id=?",
                            (sid,)).fetchone()["n"]
            at = (turn.get("started") or time.time()) + (turn.get("elapsed") or 0)
            for i, e in enumerate(events):
                payload = {k: v for k, v in e.items() if k not in ("seq", "turn_id")}
                c.execute("INSERT INTO events(session_id,turn_id,seq,type,payload,ts) "
                          "VALUES(?,?,?,?,?,?)",
                          (sid, turn["id"], seq + i, e.get("type", ""), json.dumps(payload),
                           e.get("ts") or at))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return len(events)


def _sync(sid: str, path: str, size: int, state: dict) -> int:
    """Do the work for one session. Returns the number of turns added."""
    with closing(store._connect()) as c:
        row = c.execute("SELECT MAX(started) s FROM turns WHERE session_id=?", (sid,)).fetchone()
        windows = _bridge_windows(c, sid)
    start = state.get("offset")
    resumed = isinstance(start, int) and 0 <= start <= size
    if resumed:
        cutoff = 0.0                                 # the offset is a prompt line: take it from there
    else:
        cutoff = max(0.0, (row["s"] or 0.0) - 1.0)   # first sync: from the last stored turn on
        with open(path, "rb") as fh:
            start = _bisect(fh, size, cutoff)
    tmp, offsets, end = _slice(path, start, cutoff)
    added = 0
    last_offset = start
    if tmp:
        try:
            parsed = transcript_jsonl._parse_full(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        by_turn: dict = {}
        for e in parsed["events"]:
            by_turn.setdefault(e["turn_id"], []).append(e)
        for t in parsed["turns"]:
            if t["id"] in offsets:
                last_offset = offsets[t["id"]]
            ts = t.get("started") or 0.0
            if any(lo <= ts <= hi for lo, hi in windows):
                continue                             # the bridge's own turn, already in the store
            was_new = not _has_turn(t["id"])
            _store_turn(sid, t, by_turn.get(t["id"], []), topup=resumed)
            added += was_new
    store.set_setting(_KEY + sid, json.dumps({"offset": last_offset, "size": end}))
    return added


def _has_turn(turn_id: str) -> bool:
    with closing(store._connect()) as c:
        return c.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)).fetchone() is not None


def _run(sid: str, path: str, size: int) -> int:
    with _session_lock(sid):
        try:
            return _sync(sid, path, size, _state(sid))
        except Exception as e:                       # never take a transcript request down
            print(f"[native_sync] {sid}: {type(e).__name__}: {e}", file=sys.stderr)
            return 0


def catch_up(session: "dict | None", *, wait: bool = False) -> int:
    """Append to the store what this session's JSONL gained outside the bridge.
    Cheap when nothing changed (one stat). Returns the turns added now; 0 when
    the work was handed to a thread. `wait` runs it here whatever its size and
    joins a thread already on it."""
    if not session or session.get("origin") in ("vscode", "terminal"):
        return 0                                     # still native: rendered from the JSONL itself
    sid, csid = session.get("id"), session.get("claude_session_id")
    if not sid or not csid:
        return 0
    with _lock:
        running = _threads.get(sid)
        now = time.monotonic()
        if not wait and now - _last_look.get(sid, 0.0) < _MIN_INTERVAL:
            return 0
        _last_look[sid] = now
    if running is not None and running.is_alive():
        if not wait:
            return 0
        running.join()
    path = transcript_jsonl.find_transcript(csid)
    if not path:
        return 0
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    state = _state(sid)
    if state.get("size") == size:
        return 0
    with closing(store._connect()) as c:
        rows = c.execute("SELECT status FROM turns WHERE session_id=?", (sid,)).fetchall()
    if not rows:
        return 0                                     # nothing journaled: transcript_for reads the JSONL
    if any(r["status"] == "running" for r in rows):
        return 0                                     # the file is growing with our own turn
    known = state.get("offset") if isinstance(state.get("offset"), int) else None
    backlog = size - known if known is not None and known <= size else size
    if wait or backlog <= _BG_BYTES:
        return _run(sid, path, size)
    th = threading.Thread(target=_run, args=(sid, path, size), daemon=True,
                          name=f"native-sync-{sid[:8]}")
    with _lock:
        _threads[sid] = th
    th.start()
    return 0


def catch_up_id(session_id: "str | None", *, wait: bool = False) -> int:
    """catch_up for a caller that holds only the session id (the send paths)."""
    return catch_up(store.get_session(session_id), wait=wait) if session_id else 0
