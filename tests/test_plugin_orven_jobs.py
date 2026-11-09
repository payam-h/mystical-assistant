"""The Orven jobs plugin (plugins/orven-jobs): gate-token auth with refresh, one run at a time, the
fixes framing, a run through a fake session, and a job whose folder is not here."""

import importlib.util
import os
import tempfile
import threading
import time

from bridge import config, plugins_ext

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod():
    spec = importlib.util.spec_from_file_location("jobs_plugin_under_test", os.path.join(HERE, "plugins", "orven-jobs", "plugin.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeCtx:
    def __init__(self, settings=None):
        self.data_dir = tempfile.mkdtemp()
        self._s = settings or {"JOBS_PANEL_URL": "https://panel.test", "JOBS_MAX_RUN_MIN": "1"}
        self.logs = []
    def settings(self):
        return self._s
    def log(self, m):
        self.logs.append(m)
    def notify(self, t):
        pass


class FakeJob:
    def __init__(self, **kw):
        self.texts = ["report line"]
        self.events = [{"type": "text", "text": "report line"}]
        self.result = "the report"
        self.status = "done"
        self.exited = threading.Event(); self.exited.set()
        self.store_session_id = "store-1"
        self.session_id = "claude-1"
        self.cost = 0.3
        self.error_msg = None
        self.timed_out = False
        self.interrupted = False
        self.refusal = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_fixes_are_framed_before_the_prompt():
    m = _mod()
    assert m.prompt_with_fixes("check the site", None) == "check the site"
    out = m.prompt_with_fixes("check the site", ["cache cleared", {"body": "disk freed"}])
    assert out.startswith("FIXES APPLIED SINCE YOUR LAST RUN") and "- cache cleared" in out and "- disk freed" in out and out.endswith("check the site")


def _plugin(m, calls, claim_answers, token_calls=None, first_code=200):
    p = m.Plugin()
    p.ctx = FakeCtx()
    tokens = iter(["tok-old", "tok-new"])
    def gate_token(refresh):
        (token_calls if token_calls is not None else []).append(refresh)
        return next(tokens) if refresh else "tok-old"
    p.gate_token = gate_token
    codes = {"first": first_code}
    def fake_http(url, body=None, headers=None, method=None, timeout=30):
        path = url.replace("https://panel.test/api", "")
        calls.append((path, body, dict(headers or {})))
        if codes["first"] != 200:
            codes["first"] = 200
            return 401, {"message": "invalid gate token"}
        if path == "/claude-jobs/agent/claim":
            return 200, (claim_answers.pop(0) if claim_answers else None)
        if path.endswith("/poll"):
            return 200, {"canceled": False}
        return 200, {"ok": True}
    p.http = fake_http
    return p


def test_a_refused_token_is_refreshed_through_the_orven_plugin():
    m = _mod()
    calls, tokens = [], []
    p = _plugin(m, calls, [], token_calls=tokens, first_code=401)
    p.beat()
    assert p.online is True
    assert tokens == [False, True]                                   # read, then refresh once
    assert calls[0][2]["x-gate-token"] == "tok-old" and calls[1][2]["x-gate-token"] == "tok-new"


def test_a_run_uses_the_jobs_own_session_and_reports_back(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setattr(config, "BASE_PATH", str(tmp_path))
    cwd = tmp_path / "flixdrama"; cwd.mkdir()
    calls = []
    p = _plugin(m, calls, [{"run": {"id": "run-1"}, "job": {"id": "job-9", "name": "site health", "prompt": "check", "cwd": str(cwd), "model": "sonnet", "fixes": ["x fixed"]}}])
    seen = {}
    def start_run(job_id, name, prompt, cwd_, model, pm, hang):
        seen.update(job_id=job_id, name=name, prompt=prompt, cwd=cwd_, model=model, pm=pm, hang=hang)
        return FakeJob()
    p.start_run = start_run
    p.wait_job = lambda j, c, t: p._stream(c, j)
    assert p.claim_once() == "claimed"
    for _ in range(50):
        if not p.current:
            break
        time.sleep(0.05)
    assert seen["job_id"] == "job-9" and seen["cwd"] == os.path.realpath(str(cwd)) and seen["pm"] == "bypassPermissions" and seen["hang"] == 60
    assert seen["prompt"].startswith("FIXES APPLIED") and seen["prompt"].endswith("check")
    fin = next(c for c in calls if c[0] == "/claude-jobs/runs/run-1/finish")
    assert fin[1]["status"] == "ok" and fin[1]["summary"] == "the report" and fin[1]["sessionId"] == "claude-1" and fin[1]["costUsd"] == 0.3
    assert any(c[0] == "/claude-jobs/runs/run-1/output" and "report line" in c[1]["chunk"] for c in calls)
    assert p.recent[0]["status"] == "ok" and p.current is None
    assert p.claim_once() == "empty"


def test_a_job_whose_folder_is_not_here_fails_cleanly(monkeypatch, tmp_path):
    m = _mod()
    monkeypatch.setattr(config, "BASE_PATH", str(tmp_path))
    calls = []
    p = _plugin(m, calls, [{"run": {"id": "run-2"}, "job": {"id": "j", "name": "x", "prompt": "p", "cwd": "/nowhere/at/all"}}])
    p.start_run = lambda *a: (_ for _ in ()).throw(AssertionError("must not start"))
    assert p.claim_once() == "claimed"
    for _ in range(50):
        if not p.current:
            break
        time.sleep(0.05)
    fin = next(c for c in calls if c[0].endswith("/finish"))
    assert fin[1]["status"] == "failed" and "does not exist" in fin[1]["error"]


def test_one_run_at_a_time_and_stop_refuses():
    m = _mod()
    calls = []
    p = _plugin(m, calls, [])
    p.current = m.Current("r", {"name": "busy job"})
    assert p.claim_once() == "busy" and calls == []
    try:
        p.stop(); assert False
    except plugins_ext.PluginError as e:
        assert "busy job" in str(e)
    assert "running busy job" in p.status()["line"]


def test_loader_sees_the_manifest(monkeypatch):
    monkeypatch.setattr(plugins_ext, "_ext_dir", lambda: tempfile.mkdtemp())
    dirs = {}
    monkeypatch.setattr(plugins_ext, "data_dir", lambda pid: dirs.setdefault(pid, tempfile.mkdtemp()))
    plugins_ext._registry = None; plugins_ext._live.clear(); plugins_ext._broken.clear()
    try:
        man = plugins_ext.manifest("orven-jobs")
        assert man and man["origins"] == ("orven-job",) and "jobs" in man["telegram"]
        assert plugins_ext.start("orven-jobs"), plugins_ext.broken("orven-jobs")
        body, code = plugins_ext.dispatch("GET", "/plugin/orven-jobs/state", {}, None, 555)
        assert code == 200 and body["hasToken"] is False            # no Orven plugin state in a test env
        assert "no gate token" in plugins_ext.on_command(555, "/jobs")
    finally:
        plugins_ext.stop_all(); plugins_ext._registry = None
