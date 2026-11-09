"""The Forge plugin (plugins/orven-forge): the natural-end contract, slot bookkeeping, the task
pipeline with a fake Forge and a fake session — no network, no claude."""

import importlib.util
import json
import os
import tempfile
import threading
import time

from bridge import plugins_ext

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod():
    spec = importlib.util.spec_from_file_location("forge_plugin_under_test", os.path.join(HERE, "plugins", "orven-forge", "plugin.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeCtx:
    def __init__(self, settings):
        self.data_dir = tempfile.mkdtemp()
        self._s = settings
        self.logs = []
    def settings(self):
        return self._s
    def log(self, m):
        self.logs.append(m)
    def notify(self, t):
        pass


class FakeJob:
    def __init__(self, texts=("hello",), result="done text", status="done", **kw):
        self.texts = list(texts)
        self.events = [{"type": "text", "text": t} for t in texts] + [{"type": "tool", "name": "Write"}]
        self.result = result
        self.status = status
        self.exited = threading.Event()
        self.exited.set()
        self.store_session_id = "sess1"
        self.session_id = "claude-sess"
        self.cost = 0.12
        self.error_msg = None
        self.timed_out = False
        self.interrupted = False
        self.refusal = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_natural_end_contract():
    m = _mod()
    assert m.natural_end(FakeJob()) == (True, "")
    assert m.natural_end(FakeJob(interrupted=True))[0] is False
    assert m.natural_end(FakeJob(timed_out=True))[0] is False
    assert m.natural_end(FakeJob(status="error", error_msg="exit 1")) == (False, "exit 1")
    assert m.natural_end(FakeJob(texts=(), result=None))[0] is False
    assert "refused" in m.natural_end(FakeJob(refusal="no login"))[1]


def test_heavy_picks_sessions_for_opus_tools_or_named_templates():
    m = _mod()
    names = "actor-bio,caption-translate"
    assert m.heavy({"templateKey": "comment-moderation", "model": "haiku"}, names) is False
    assert m.heavy({"templateKey": "actor-bio", "model": "opus"}, names) is True
    assert m.heavy({"templateKey": "caption-translate", "model": "sonnet"}, names) is True
    assert m.heavy({"templateKey": "comment-audit", "model": "sonnet", "allowedTools": "Read Write"}, names) is True
    assert m.heavy({"templateKey": "x", "model": "claude-opus-5-5"}, "") is True


def test_one_shot_result_shapes_like_a_job():
    m = _mod()
    ok = m.OneShot("verdicts written", "sid", 0.01, False)
    assert m.natural_end(ok) == (True, "")
    assert m.natural_end(m.OneShot("❌ No output.", None, None, True))[0] is False
    to = m.OneShot("⏱️ Timed out after 2 min.", None, None, True)
    assert to.timed_out and "watchdog" in m.natural_end(to)[1]


def test_multipart_has_fields_and_file():
    m = _mod()
    body, ctype = m.multipart({"name": "translated"}, "out.srt", b"1\n00:00 --> 00:01\nhi")
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="name"\r\n\r\ntranslated' in body and b'filename="out.srt"' in body and b"hi\r\n--" in body


def _plugin(m, calls, claim_answers, settings=None, base_path=None):
    p = m.Plugin()
    p.ctx = FakeCtx(settings or {"FORGE_URL": "https://forge.test", "FORGE_AGENT_TOKEN": "tok", "FORGE_SLOTS": "2", "FORGE_MAX_RUN_MIN": "1"})
    def fake_http(url, body=None, headers=None, method=None, timeout=30, raw=None):
        calls.append((url.replace("https://forge.test/api", ""), body, dict(headers or {}), raw))
        if url.endswith("/agent/claim"):
            return 200, (claim_answers.pop(0) if claim_answers else {})
        if url.endswith("/poll"):
            return 200, {"canceled": False}
        return 200, {"ok": True}
    p.http = fake_http
    return p


def test_claim_reserves_capacity_and_runs_the_pipeline(monkeypatch, tmp_path):
    m = _mod()
    from bridge import config
    monkeypatch.setattr(config, "BASE_PATH", str(tmp_path))
    calls = []
    job = {"templateKey": "srt-translate", "renderedPrompt": "translate", "model": "sonnet", "permissionMode": "bypassPermissions",
           "allowedTools": "", "slot": 0, "inputs": [], "outputs": [{"name": "translated", "path": "output/translated.srt"}]}
    p = _plugin(m, calls, [{"task": {"id": "task-1234567890"}, "job": job}, {}],
                settings={"FORGE_URL": "https://forge.test", "FORGE_AGENT_TOKEN": "tok", "FORGE_SLOTS": "2", "FORGE_MAX_RUN_MIN": "1", "FORGE_SESSION_TEMPLATES": "srt-translate"})
    started = {}
    def start_run(prompt, workdir, model, pm, tools, origin, hang):
        started.update(prompt=prompt, workdir=workdir, model=model, pm=pm, origin=origin, hang=hang)
        os.makedirs(os.path.join(workdir, "output"), exist_ok=True)
        with open(os.path.join(workdir, "output", "translated.srt"), "w") as f:
            f.write("1\n00:00 --> 00:01\nسلام\n")
        return FakeJob()
    p.start_run = start_run
    p.wait_job = lambda job_obj, run, timeout: p._stream(run, job_obj)
    assert p.claim_once() == "claimed"
    # the run thread finishes quickly
    for _ in range(50):
        if not p.runs:
            break
        time.sleep(0.05)
    assert not p.runs and p.reserved == 0
    assert started["workdir"] == os.path.join(str(tmp_path), ".forge", "anvil") and started["origin"] == "forge:srt-translate" and started["hang"] == 60
    paths = [c[0] for c in calls]
    assert paths[0] == "/agent/claim"
    assert any(pth == "/agent/tasks/task-1234567890/output" for pth in paths)
    art = next(c for c in calls if c[0] == "/agent/tasks/task-1234567890/artifact")
    assert art[3] is not None and b'filename="translated.srt"' in art[3] and art[2]["Content-Type"].startswith("multipart/")
    fin = next(c for c in calls if c[0] == "/agent/tasks/task-1234567890/finish")
    assert fin[1]["status"] == "ok" and fin[1]["sessionId"] == "claude-sess" and fin[1]["costUsd"] == 0.12 and fin[1]["summary"] == "done text"
    assert calls[0][2]["x-agent-slots"] == "2" and calls[0][2]["x-agent-token"] == "tok"
    assert p.recent[0]["status"] == "ok" and p.recent[0]["label"] == "srt-translate"
    assert p.claim_once() == "empty"
    assert not os.path.exists(started["workdir"])           # the slot's folder is wiped after the run


def test_a_light_task_runs_as_a_one_shot(monkeypatch, tmp_path):
    m = _mod()
    from bridge import config
    monkeypatch.setattr(config, "BASE_PATH", str(tmp_path))
    calls = []
    job = {"templateKey": "comment-moderation", "renderedPrompt": "judge", "model": "haiku", "slot": 0,
           "outputs": [{"name": "verdicts", "path": "output/verdicts.json"}]}
    p = _plugin(m, calls, [{"task": {"id": "task-light"}, "job": job}])
    seen = {}
    def run_once(prompt, workdir, model, pm, hang):
        seen.update(prompt=prompt, model=model, pm=pm, hang=hang)
        with open(os.path.join(workdir, "output", "verdicts.json"), "w") as f:
            f.write('{"ok":true}')
        return m.OneShot("done", "sid-1", 0.002, False)
    p.run_once = run_once
    p.start_run = lambda *a: (_ for _ in ()).throw(AssertionError("a light task must not open a session"))
    assert p.claim_once() == "claimed"
    for _ in range(50):
        if not p.runs:
            break
        time.sleep(0.05)
    assert seen == {"prompt": "judge", "model": "haiku", "pm": "bypassPermissions", "hang": 60}
    fin = next(c for c in calls if c[0].endswith("/finish"))
    assert fin[1]["status"] == "ok" and fin[1]["costUsd"] == 0.002
    assert any(c[0].endswith("/artifact") for c in calls)
    assert p.recent[0]["mode"] == "one-shot"


def test_a_cut_off_run_fails_and_uploads_nothing(monkeypatch, tmp_path):
    m = _mod()
    from bridge import config
    monkeypatch.setattr(config, "BASE_PATH", str(tmp_path))
    calls = []
    job = {"templateKey": "actor-bio", "renderedPrompt": "x", "model": "opus", "slot": 1, "outputs": [{"name": "report", "path": "output/report.md"}]}
    p = _plugin(m, calls, [{"task": {"id": "task-2"}, "job": job}])
    def start_run(prompt, workdir, *a):
        os.makedirs(os.path.join(workdir, "output"), exist_ok=True)
        with open(os.path.join(workdir, "output", "report.md"), "w") as f:
            f.write("half")
        return FakeJob(interrupted=True)
    p.start_run = start_run
    p.wait_job = lambda j, r, t: None
    assert p.claim_once() == "claimed"
    for _ in range(50):
        if not p.runs:
            break
        time.sleep(0.05)
    paths = [c[0] for c in calls]
    assert not any(pth.endswith("/artifact") for pth in paths)
    fin = next(c for c in calls if c[0].endswith("/finish"))
    assert fin[1]["status"] == "failed" and "stopped" in fin[1]["error"]
    assert any(c[1] and "not uploaded" in str(c[1].get("chunk", "")) for c in calls if c[0].endswith("/output"))


def test_full_slots_do_not_claim_and_stop_refuses_mid_run():
    m = _mod()
    calls = []
    p = _plugin(m, calls, [], settings={"FORGE_URL": "https://forge.test", "FORGE_AGENT_TOKEN": "tok", "FORGE_SLOTS": "1"})
    p.runs[0] = m.Run("t", {"templateKey": "x"}, 0, "/tmp/x")
    assert p.claim_once() == "full" and calls == []
    try:
        p.stop()
        assert False, "stop should refuse"
    except plugins_ext.PluginError as e:
        assert "still running" in str(e)
    assert p.status()["line"].endswith("1/1 slots")


def test_routes_and_manifest_through_the_loader(monkeypatch):
    monkeypatch.setattr(plugins_ext, "_ext_dir", lambda: tempfile.mkdtemp())
    dirs = {}
    monkeypatch.setattr(plugins_ext, "data_dir", lambda pid: dirs.setdefault(pid, tempfile.mkdtemp()))
    monkeypatch.setenv("MYSTICAL_PLUGINS_BUNDLED", "1")   # this test wants the shipped manifest
    plugins_ext._registry = None
    plugins_ext._live.clear()
    plugins_ext._broken.clear()
    try:
        man = plugins_ext.manifest("orven-forge")
        assert man and man["origins"] == ("forge",) and man["pane"]["title"] == "FORGE"
        assert plugins_ext.start("orven-forge"), plugins_ext.broken("orven-forge")
        body, code = plugins_ext.dispatch("GET", "/plugin/orven-forge/state", {}, None, 555)
        assert code == 200 and body["configured"] is False and body["slots"] == 1
        assert "no agent token" in plugins_ext.on_command(555, "/forge")
    finally:
        plugins_ext.stop_all()
        plugins_ext._registry = None


def test_one_shots_carry_the_internal_tag_so_the_scanner_skips_them(monkeypatch):
    m = _mod()
    from bridge import native, runner
    seen = {}
    monkeypatch.setattr(runner, "run_blocking", lambda chat, prompt, **kw: seen.update(prompt=prompt, kw=kw) or ("ok", "sid", 0.001, False))
    p = m.Plugin()
    out = p._run_once("judge these", "/tmp/w", "haiku", "bypassPermissions", 60)
    assert out.result == "ok" and seen["prompt"] == f"{native.INTERNAL_ONESHOT_TAG}\njudge these"
    assert seen["kw"]["skip_pack"] is True and seen["kw"]["cwd"] == "/tmp/w"
    assert native._is_internal_oneshot(seen["prompt"])
