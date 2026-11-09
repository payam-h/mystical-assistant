"""Out-of-tree plugins (bridge/plugins_ext.py): discovery, the install table, the doors, isolation."""

import json
import os
import tempfile

import pytest

from bridge import config, integrations, plugins_ext

GOOD = '''
class Plugin:
    started = False
    def start(self, ctx):
        self.ctx = ctx
        Plugin.started = True
    def stop(self):
        Plugin.started = False
    def routes(self):
        return {("GET", "/hello"): lambda req: ({"hello": req["qs"].get("name", ["world"])[0], "chat": req["chat"]}, 200),
                ("POST", "/echo"): lambda req: ({"echo": req["body"]}, 200)}
    def tools(self):
        return [{"name": "ping", "description": "pong", "inputSchema": {"type": "object", "properties": {}}}]
    def call_tool(self, s, name, args):
        return f"pong:{name}:{self.ctx.settings().get('GREETING', '')}"
    def telegram(self):
        return {"/hello": lambda chat_id, rest: f"hi {rest or 'there'} from {chat_id}"}
    def status(self):
        return {"line": "3 things", "ok": True}
'''

BROKEN = "raise RuntimeError('boom at import')"


def _plugin(root, pid, code=GOOD, manifest=None, ui=True):
    d = os.path.join(root, pid)
    os.makedirs(os.path.join(d, "ui"), exist_ok=True)
    m = {"id": pid, "label": pid.title(), "blurb": "a test plugin", "needs": "nothing", "entry": "plugin.py",
         "settings": [{"key": "GREETING", "label": "Greeting", "default": "hey"},
                      {"key": "SECRET_KEY", "label": "Secret", "secret": True}],
         "telegram": ["hello"]}
    if ui:
        m["pane"] = {"title": "TEST", "entry": "pane.js"}
        with open(os.path.join(d, "ui", "pane.js"), "w") as f:
            f.write("export function mount(el){ el.textContent = 'hi' }")
    if manifest:
        m.update(manifest)
    with open(os.path.join(d, "plugin.json"), "w") as f:
        json.dump(m, f)
    with open(os.path.join(d, "plugin.py"), "w") as f:
        f.write(code)
    return d


@pytest.fixture
def ext(monkeypatch):
    """A fresh external plugin dir and a cold registry; integrations' table is rebuilt from it."""
    root = tempfile.mkdtemp()
    monkeypatch.setattr(plugins_ext, "_ext_dir", lambda: root)
    monkeypatch.setattr(plugins_ext, "_REPO_DIR", os.path.join(root, "no-bundled"))
    plugins_ext._registry = None
    plugins_ext._live.clear()
    plugins_ext._broken.clear()
    yield root
    plugins_ext.stop_all()
    plugins_ext._registry = None
    plugins_ext._live.clear()
    plugins_ext._broken.clear()


def _rebuild_table(monkeypatch):
    """integrations builds PLUGINS at import; after adding a plugin dir, rebuild the way a restart would."""
    ext = tuple({"id": m["id"], "label": m["label"], "blurb": m["blurb"], "needs": m["needs"],
                 "env_keys": m["env_keys"], "origins": m["origins"], "routes": m["routes"]}
                for m in plugins_ext.registry(refresh=True))
    monkeypatch.setattr(integrations, "PLUGINS", integrations.BUILTIN + ext)
    monkeypatch.setattr(integrations, "_EXT", {m["id"] for m in plugins_ext.registry()})
    monkeypatch.setattr(integrations, "_IDS", tuple(p["id"] for p in integrations.PLUGINS))
    integrations._cache = None
    monkeypatch.setattr(integrations, "_path", lambda: os.path.join(tempfile.mkdtemp(), "plugins.json"))


def test_discovery_validates_manifests(ext):
    _plugin(ext, "good")
    _plugin(ext, "bad-route", manifest={"routes": ["/local/evil"]})
    _plugin(ext, "no-entry", code="", manifest={"entry": "missing.py"})
    ids = {m["id"] for m in plugins_ext.registry(refresh=True)}
    assert "good" in ids
    # a bad manifest is listed as broken under its folder name rather than vanishing
    assert "bad-route" in ids and "must be under /plugin/bad-route" in plugins_ext.broken("bad-route")
    assert "no-entry" in ids and "entry" in plugins_ext.broken("no-entry")
    good = plugins_ext.manifest("good")
    assert good["routes"] == ("/plugin/good",)
    assert good["env_keys"] == ("GREETING", "SECRET_KEY")
    assert good["pane"]["entry"] == "pane.js"


def test_install_starts_and_remove_stops(ext, monkeypatch):
    _plugin(ext, "good")
    _rebuild_table(monkeypatch)
    assert integrations.installed("good") is False      # discovered plugins start OFF
    assert plugins_ext.is_live("good") is False
    integrations.set_installed("good", True)
    assert plugins_ext.is_live("good") and integrations.installed("good")
    card = next(p for p in integrations.state() if p["id"] == "good")
    assert card["ext"] is True and card["summary"] == "3 things" and card["pane"]["title"] == "TEST"
    assert [s["key"] for s in card["settings"]] == ["GREETING", "SECRET_KEY"]
    integrations.set_installed("good", False)
    assert plugins_ext.is_live("good") is False


def test_doors_route_only_while_installed(ext, monkeypatch):
    _plugin(ext, "good")
    _rebuild_table(monkeypatch)
    assert integrations.route_owner("/plugin/good/hello") == "good"
    assert integrations.route_owner("/api/plugin/good/hello") == "good"
    assert integrations.route_owner("/plugin/nobody/x") == "nobody"   # unknown owner is never installed → 404
    # not started: 503 with a reason, never a raise
    body, code = plugins_ext.dispatch("GET", "/plugin/good/hello", {}, None, 555)
    assert code == 503
    plugins_ext.start("good")
    body, code = plugins_ext.dispatch("GET", "/plugin/good/hello", {"name": ["Pixie"]}, None, 555)
    assert (body, code) == ({"hello": "Pixie", "chat": 555}, 200)
    body, code = plugins_ext.dispatch("POST", "/api/plugin/good/echo", {}, {"a": 1}, 555)
    assert (body, code) == ({"echo": {"a": 1}}, 200)
    assert plugins_ext.dispatch("GET", "/plugin/good/nope", {}, None, 555)[1] == 404


def test_settings_are_masked_and_reach_the_plugin(ext, monkeypatch):
    _plugin(ext, "good")
    dirs: dict = {}
    monkeypatch.setattr(plugins_ext, "data_dir", lambda pid: dirs.setdefault(pid, tempfile.mkdtemp()))
    plugins_ext.registry(refresh=True)
    plugins_ext.start("good")
    body, code = plugins_ext.dispatch("POST", "/plugin/good/settings", {}, {"values": {"GREETING": "salam", "SECRET_KEY": "abcdef1234"}}, 555)
    assert code == 200
    view = {s["key"]: s for s in body["settings"]}
    assert view["GREETING"]["value"] == "salam"
    assert view["SECRET_KEY"]["value"] == "•••1234" and view["SECRET_KEY"]["set"] is True
    assert "abcdef1234" not in json.dumps(body)                    # a secret never leaves in full
    assert plugins_ext.call_tool({}, "good__ping", {}) == "pong:ping:salam"
    assert plugins_ext.dispatch("POST", "/plugin/good/settings", {}, {"values": {"NOPE": "x"}}, 555)[1] == 400


def test_tools_are_prefixed_and_commands_namespaced(ext):
    _plugin(ext, "good")
    plugins_ext.registry(refresh=True)
    plugins_ext.start("good")
    names = [t["name"] for t in plugins_ext.tools_for({})]
    assert names == ["good__ping"]
    assert plugins_ext.owns_tool("good__ping") == "good" and plugins_ext.owns_tool("send_message") is None
    assert plugins_ext.on_command(555, "/hello Payam") == "hi Payam from 555"
    assert plugins_ext.on_command(555, "/unknown") is None


def test_a_broken_plugin_is_contained(ext):
    _plugin(ext, "boom", code=BROKEN)
    _plugin(ext, "good")
    plugins_ext.registry(refresh=True)
    assert plugins_ext.start("boom") is False
    assert "boom at import" in plugins_ext.broken("boom")
    assert plugins_ext.status("boom")["ok"] is False
    assert plugins_ext.start("good") is True                        # a neighbour's fault is not mine
    body, code = plugins_ext.dispatch("GET", "/plugin/boom/hello", {}, None, 555)
    assert code == 503 and "boom at import" in body["error"]


def test_assets_stay_inside_ui(ext):
    _plugin(ext, "good")
    plugins_ext.registry(refresh=True)
    got = plugins_ext.asset("/plugin/good/ui/pane.js")
    assert got and got[1].startswith("text/javascript") and b"mount" in got[0]
    assert plugins_ext.asset("/plugin/good/ui/../plugin.py") is None
    assert plugins_ext.asset("/plugin/good/ui/missing.js") is None


def test_stop_refusal_becomes_an_integration_error(ext, monkeypatch):
    _plugin(ext, "sticky", code=GOOD.replace("def stop(self):\n        Plugin.started = False",
                                             "def stop(self):\n        from bridge import plugins_ext\n        raise plugins_ext.PluginError('a run is in flight')"))
    _rebuild_table(monkeypatch)
    integrations.set_installed("sticky", True)
    with pytest.raises(integrations.IntegrationError):
        integrations.set_installed("sticky", False)
    assert integrations.installed("sticky") is True                 # the flag did not flip
