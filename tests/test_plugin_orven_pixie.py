"""The Pixie dev team plugin (plugins/orven-pixie): workspace fencing, the helper's brief, a ticket through a
fake run to Pixie's finish with screenshots, a failed run, a revision, one ticket at a time."""

import base64
import importlib.util
import os
import tempfile
import time

from bridge import plugins_ext

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod():
    spec = importlib.util.spec_from_file_location("pixie_plugin_under_test", os.path.join(HERE, "plugins", "orven-pixie", "plugin.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeCtx:
    def __init__(self, settings=None):
        self.data_dir = tempfile.mkdtemp()
        self._s = settings or {"PIXIE_URL": "https://pixie.test", "PIXIE_DEV_TOKEN": "tok", "PIXIE_MAX_RUN_MIN": "1", "PIXIE_KNOWLEDGE": "CLAUDE.md"}
        self.logs = []
    def settings(self):
        return self._s
    def log(self, m):
        self.logs.append(m)


def _ws(tmp_path):
    ws = tmp_path / "flixdrama"; (ws / "FlixDrama-PHP").mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# umbrella\n")
    return ws


def _plugin(m, calls, claims):
    p = m.Plugin()
    p.ctx = FakeCtx()
    def fake_http(url, body=None, headers=None, method=None, timeout=60):
        path = url.replace("https://pixie.test/api/dev", "")
        calls.append((path, body, dict(headers or {})))
        assert headers["X-Dev-Token"] == "tok"
        if path == "/claim":
            return 200, (claims.pop(0) if claims else {"ticket": None})
        return 200, {"ok": True}
    p.http = fake_http
    return p


def _settle(p):
    for _ in range(100):
        if not p.current:
            return
        time.sleep(0.03)


def test_workspace_fencing(tmp_path):
    m = _mod()
    ws = _ws(tmp_path)
    allowed = [str(ws)]
    w, r = m.resolve_paths({"workspace": str(ws), "repo": "FlixDrama-PHP"}, allowed)
    assert r.endswith("FlixDrama-PHP") and w == os.path.realpath(str(ws))
    for bad in ({"workspace": str(tmp_path), "repo": "flixdrama"}, {"workspace": str(ws), "repo": "../"}, {"workspace": str(ws), "repo": "nope"}):
        try:
            m.resolve_paths(bad, allowed)
            assert False, bad
        except RuntimeError:
            pass


def test_the_brief_carries_acceptance_knowledge_and_the_screenshot_ask(tmp_path):
    m = _mod()
    ws = _ws(tmp_path)
    t = {"id": 7, "title": "x", "details": "Make the episode page show the dub badge.", "acceptance": "badge visible on E1", "screenshots": True, "reporter": "payam"}
    d = m.item_detail(t, str(ws), ["CLAUDE.md", "missing.md"])
    assert "dub badge" in d and "Acceptance" in d and "badge visible" in d
    assert str(ws / "CLAUDE.md") in d and "missing.md" not in d
    assert "BEFORE and AFTER screenshots" in d and ".mystical/scratch/" in d and "Do not deploy" in d and "#7" in d
    assert "screenshots" not in m.item_detail({**t, "screenshots": False}, str(ws), [])


def test_check_line():
    m = _mod()
    assert m.check_line({"state": "done", "command": "pytest -q", "passed": 41, "failed": 0, "ok": True}) == "pytest: 41 passed, 0 failed, ok"
    assert "NEW failures: test_x" in m.check_line({"state": "done", "command": "npm test", "passed": 3, "failed": 1, "new": ["test_x"], "ok": False})
    assert m.check_line({"state": "failed", "command": "make check"}) == "make: could not run"


def test_a_ticket_runs_publishes_and_reports_with_screenshots(tmp_path):
    m = _mod()
    ws = _ws(tmp_path)
    shot = tmp_path / "after.png"; shot.write_bytes(b"\x89PNG fake")
    calls = []
    t = {"id": 12, "title": "Dub badge on episodes", "details": "d", "workspace": str(ws), "repo": "FlixDrama-PHP", "dev_state": "queued", "screenshots": True}
    p = _plugin(m, calls, [{"ticket": t, "workspaces": [str(ws)]}])
    seen = {}
    def start_run(w):
        w.run_id, w.item_id, w.chat = "r1", "i1", {"id": "chat-1"}
        seen["repo"] = w.repo
    p.start_run = start_run
    p.wait_run = lambda w, timeout: {"ok": True, "summary": "Added the badge.", "decisions": ["kept the old css"], "not_verified": None, "checks": {"before": "phpunit: 10 passed, 0 failed", "after": "phpunit: 11 passed, 0 failed, ok"}, "tests": "phpunit: 11 passed, 0 failed, ok", "pictures": [str(shot)], "cost": None}
    p.publish = lambda w, facts: ("pixie/12-dub-badge-on-episodes", "https://github.com/x/y/pull/3")
    assert p.poll_once() == "claimed"
    _settle(p)
    assert seen["repo"].endswith("FlixDrama-PHP")
    prog = [c for c in calls if c[0] == "/12/progress"]
    assert prog and prog[0][1]["mystical_run"] == "r1" and prog[0][1]["mystical_session"] == "chat-1"
    fin = next(c for c in calls if c[0] == "/12/finish")
    b = fin[1]
    assert b["summary"] == "Added the badge." and b["pr_url"].endswith("/pull/3") and b["branch"].startswith("pixie/12-") and b["tests"].startswith("phpunit")
    assert b["screenshots"][0]["name"] == "after.png" and base64.b64decode(b["screenshots"][0]["data"]) == b"\x89PNG fake"
    assert p.recent[0]["status"] == "review" and p.recent[0]["pr"].endswith("/pull/3") and p.current is None
    assert p.poll_once() == "empty"


def test_a_run_that_does_not_finish_fails_the_ticket(tmp_path):
    m = _mod()
    ws = _ws(tmp_path)
    calls = []
    p = _plugin(m, calls, [{"ticket": {"id": 13, "title": "t", "workspace": str(ws), "repo": "FlixDrama-PHP"}, "workspaces": [str(ws)]}])
    p.start_run = lambda w: None
    p.wait_run = lambda w, timeout: {"ok": False, "why": "the run paused: The manager stopped moving."}
    p.publish = lambda w, f: (_ for _ in ()).throw(AssertionError("must not publish"))
    assert p.poll_once() == "claimed"
    _settle(p)
    fin = next(c for c in calls if c[0] == "/13/fail")
    assert "stopped moving" in fin[1]["error"] and p.recent[0]["status"] == "failed"


def test_a_workspace_that_is_not_here_fails_without_starting(tmp_path):
    m = _mod()
    calls = []
    p = _plugin(m, calls, [{"ticket": {"id": 14, "title": "t", "workspace": "/elsewhere", "repo": "x"}, "workspaces": [str(tmp_path)]}])
    p.start_run = lambda w: (_ for _ in ()).throw(AssertionError("must not start"))
    assert p.poll_once() == "failed"
    assert calls[-1][0] == "/14/fail" and "not one of this machine" in calls[-1][1]["error"] and p.current is None


def test_a_revision_goes_through_revise_run(tmp_path):
    m = _mod()
    ws = _ws(tmp_path)
    calls = []
    t = {"id": 12, "title": "Dub badge", "workspace": str(ws), "repo": "FlixDrama-PHP", "dev_state": "revise", "revise_note": "make it red", "pr_url": "https://github.com/x/y/pull/3", "result": {"branch": "pixie/12-dub-badge"}}
    p = _plugin(m, calls, [{"ticket": t, "workspaces": [str(ws)]}])
    p.start_run = lambda w: (_ for _ in ()).throw(AssertionError("a revision is not a new run"))
    got = {}
    def revise(w, note, timeout):
        got["note"], got["timeout"] = note, timeout
        return {"ok": True, "summary": "Now red.", "pictures": [], "branch": "pixie/12-dub-badge", "pr_url": t["pr_url"]}
    p.revise_run = revise
    assert p.poll_once() == "claimed"
    _settle(p)
    assert got == {"note": "make it red", "timeout": 60}
    fin = next(c for c in calls if c[0] == "/12/finish")
    assert fin[1]["summary"] == "Now red." and fin[1]["pr_url"].endswith("/pull/3")


def test_one_ticket_at_a_time_and_stop_refuses(tmp_path):
    m = _mod()
    calls = []
    p = _plugin(m, calls, [])
    p.current = m.Work({"id": 5, "title": "busy"}, str(tmp_path), str(tmp_path))
    assert p.poll_once() == "busy" and calls == []
    try:
        p.stop()
        assert False
    except plugins_ext.PluginError as e:
        assert "#5" in str(e)
    assert p.status()["line"].startswith("starting · #5") and "#5" in p.telegram()["/devteam"](1, "")
    p.ctx._s["PIXIE_DEV_TOKEN"] = ""
    assert p.status() == {"line": "no token", "ok": True}
