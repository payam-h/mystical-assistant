"""Out-of-tree plugins: a manifest, an entry module, and the few seams that reach them.

bridge/integrations.py already says what a plugin IS to the rest of the bridge — an id with
a label, the routes it owns, the env keys it brings, the session origins its workers stamp,
and an install flag in plugins.json — and its docstring says the table is shaped to take
plugins from outside the repo but nothing loads them. This module is that loader, and
nothing more: integrations.py keeps the table, the servers keep their doors, and a plugin
reaches the bridge only through the `ctx` it is handed.

Where plugins come from. Two directories, same shape:
  - `plugins/<id>/` in this repo — bundled, OFF until installed (the Rivendell rule);
  - `<state>/plugins/<id>/` beside the DB (`mystical plugin add <git-url>`) — external, also OFF
    until installed. State dir, not ~/.mystical: that one is per-repo runtime (mystical_dir).
Each holds `plugin.json` (the manifest, validated here, every key optional but id/label/entry)
and the entry module, imported with importlib under the name `mystical_plugin_<id>` so two
plugins may both have a `plugin.py`.

What a plugin gets. A Plugin object with any of: start(ctx), stop(), routes() -> {(METHOD,
"/path"): handler(req)}, tools() -> [tooldef], call_tool(s, name, args) -> str, telegram() ->
{"/cmd": handler(chat_id, rest)}, status() -> {"line": str, "ok": bool}. All optional; a
plugin that is just a pane needs none. `ctx` carries runner/store/config/log/notify, its
settings (per-plugin JSON beside the DB, secrets 0600, never in the manifest) and its state
dir. Handlers receive a plain dict {method, path, qs, body, chat} and return (body, status).

Why in-process and not a subprocess. The bundled plugins run in-process (rivendell.py holds a
websocket and starts sessions through runner); a plugin that could not do the same would be
a different, smaller thing. The price is isolation, paid here: every call into a plugin is
wrapped, an exception marks the plugin `broken` with its message on the card and answers the
caller with an error, and a broken plugin's routes 503 instead of raising into a server.
A plugin that misbehaves on purpose is the same trust question as a Claude Code plugin.

Why the servers get one dispatcher. Both doors already ask integrations.route_owner() and
404 while the owner is off. `/plugin/<id>/...` is one more prefix there; the dashboard serves
`/plugin/<id>/ui/*` as static files from the plugin dir (its pane), everything else is a
route. One function, two callers (bridge-feature-slice's "two servers, one shape").

Stdlib only.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import traceback
from typing import Any, Callable

from bridge import config

_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_REPO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins")

Handler = Callable[[dict], "tuple[dict, int]"]

_lock = threading.RLock()
_registry: "dict[str, dict] | None" = None     # id -> manifest (+ "dir", "source")
_live: "dict[str, Any]" = {}                   # id -> Plugin instance, started
_broken: "dict[str, str]" = {}                 # id -> why (import or start failed)


class PluginError(Exception):
    """One line the card or a route can show."""


# ---------- paths ---------------------------------------------------------------------------

def state_dir() -> str:
    return os.path.join(os.path.dirname(config.BRIDGE_DB), "plugins")


def _ext_dir() -> str:
    return state_dir()


def data_dir(pid: str) -> str:
    d = os.path.join(os.path.dirname(config.BRIDGE_DB), "plugin-data", pid)
    os.makedirs(d, exist_ok=True)
    return d


# ---------- manifests -----------------------------------------------------------------------

def _validate(m: dict, d: str, source: str) -> dict:
    pid = str(m.get("id") or "")
    if not _ID.match(pid):
        raise PluginError(f"{d}: id must match {_ID.pattern}")
    entry = str(m.get("entry") or "plugin.py")
    if "/" in entry or not entry.endswith(".py") or not os.path.isfile(os.path.join(d, entry)):
        raise PluginError(f"{pid}: entry {entry!r} is not a .py file in the plugin dir")
    routes = tuple(str(r) for r in m.get("routes") or ())
    pref = f"/plugin/{pid}"
    for r in routes:
        if r != pref and not r.startswith(pref + "/"):
            raise PluginError(f"{pid}: route {r!r} must be under {pref}")
    settings = []
    for row in m.get("settings") or []:
        if not isinstance(row, dict) or not re.match(r"^[A-Z][A-Z0-9_]{1,40}$", str(row.get("key") or "")):
            raise PluginError(f"{pid}: a settings row needs an UPPER_CASE key")
        settings.append({"key": row["key"], "label": str(row.get("label") or row["key"]),
                         "secret": bool(row.get("secret")), "default": str(row.get("default") or ""),
                         "hint": str(row.get("hint") or "")})
    pane = m.get("pane")
    if pane is not None:
        if not isinstance(pane, dict) or not str(pane.get("entry") or "").endswith(".js"):
            raise PluginError(f"{pid}: pane needs an entry ending in .js")
        if not os.path.isfile(os.path.join(d, "ui", str(pane["entry"]))):
            raise PluginError(f"{pid}: ui/{pane['entry']} is missing")
        icon = None
        if pane.get("icon"):      # the rail tab's own icon: a small inline SVG drawn with currentColor, from ui/
            ipath = os.path.join(d, "ui", str(pane["icon"]))
            try:
                with open(ipath, encoding="utf-8") as f:
                    icon = f.read(8192).strip()
            except OSError:
                raise PluginError(f"{pid}: ui/{pane['icon']} is missing")
            if not icon.startswith("<svg") or len(icon) > 6000 or "<script" in icon.lower() or "javascript:" in icon.lower() or re.search(r"\son\w+\s*=", icon):
                raise PluginError(f"{pid}: pane icon must be a small plain <svg> (no scripts or handlers)")
        pane = {"title": str(pane.get("title") or pid.upper()), "entry": str(pane["entry"]), "icon": icon,
                "css": str(pane.get("css") or "") or None, "scope": pane.get("scope") if pane.get("scope") in ("project", "worktree") else None}
    return {
        "id": pid, "label": str(m.get("label") or pid), "version": str(m.get("version") or "0"),
        "blurb": str(m.get("blurb") or ""), "needs": str(m.get("needs") or ""),
        "entry": entry, "routes": routes or (pref,), "origins": tuple(str(o) for o in m.get("origins") or ()),
        "env_keys": tuple(s["key"] for s in settings), "settings": settings, "pane": pane,
        "telegram": [str(c) for c in m.get("telegram") or []],
        "dir": d, "source": source,
    }


def _scan(root: str, source: str, found: dict) -> None:
    if not os.path.isdir(root):
        return
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        mf = os.path.join(d, "plugin.json")
        if not os.path.isfile(mf):
            continue
        try:
            with open(mf, encoding="utf-8") as f:
                m = _validate(json.load(f), d, source)
        except (OSError, ValueError, PluginError) as e:
            # A bad manifest shows on the page as broken, under the folder's name, instead of vanishing.
            pid = name if _ID.match(name) else None
            if pid:
                found[pid] = {"id": pid, "label": name, "version": "0", "blurb": "", "needs": "",
                              "entry": "", "routes": (f"/plugin/{pid}",), "origins": (), "env_keys": (),
                              "settings": [], "pane": None, "telegram": [], "dir": d, "source": source}
                _broken[pid] = f"manifest: {e}"
            continue
        if m["id"] in found:
            _broken[m["id"]] = f"two plugins claim id {m['id']} ({found[m['id']]['dir']} and {d})"
            continue
        found[m["id"]] = m


def registry(refresh: bool = False) -> "tuple[dict, ...]":
    """Every discovered plugin's manifest, bundled first. Cached; `refresh` rescans
    (the CLI adds a plugin into a running bridge's view this way)."""
    global _registry
    with _lock:
        if _registry is None or refresh:
            found: dict = {}
            if os.environ.get("MYSTICAL_PLUGINS_BUNDLED", "1") not in ("0", "false", "no", ""):   # tests: upstream's suite counts two bundled plugins
                _scan(_REPO_DIR, "bundled", found)
            _scan(_ext_dir(), "external", found)
            _registry = found
        return tuple(_registry.values())


def manifest(pid: str) -> "dict | None":
    return next((m for m in registry() if m["id"] == pid), None)


def ids() -> "tuple[str, ...]":
    return tuple(m["id"] for m in registry())


# ---------- settings ------------------------------------------------------------------------

def _settings_path(pid: str) -> str:
    return os.path.join(data_dir(pid), "settings.json")


def settings(pid: str) -> dict:
    """Effective settings: saved value, else the manifest default."""
    m = manifest(pid)
    out = {s["key"]: s["default"] for s in (m["settings"] if m else [])}
    try:
        with open(_settings_path(pid), encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            out.update({k: str(v) for k, v in saved.items() if k in out})
    except (OSError, ValueError):
        pass
    return out


def save_settings(pid: str, values: dict) -> dict:
    m = manifest(pid)
    if not m:
        raise ValueError(f"unknown plugin {pid!r}")
    keys = {s["key"] for s in m["settings"]}
    cur = {}
    try:
        with open(_settings_path(pid), encoding="utf-8") as f:
            cur = json.load(f) or {}
    except (OSError, ValueError):
        pass
    for k, v in (values or {}).items():
        if k not in keys:
            raise ValueError(f"{pid} has no setting {k}")
        if v is None or v == "":
            cur.pop(k, None)
        else:
            cur[k] = str(v)
    tmp = _settings_path(pid) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cur, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, _settings_path(pid))
    live = _live.get(pid)
    if live is not None and hasattr(live, "settings_changed"):
        _guard(pid, lambda: live.settings_changed(settings(pid)))
    return settings(pid)


def settings_view(pid: str) -> list:
    """For the card: every key with its value, secrets masked to a tail."""
    m = manifest(pid)
    vals = settings(pid)
    out = []
    for s in (m["settings"] if m else []):
        v = vals.get(s["key"], "")
        shown = (("•••" + v[-4:]) if len(v) > 4 else ("•••" if v else "")) if s["secret"] else v
        out.append({**s, "value": shown, "set": bool(v)})
    return out


# ---------- lifecycle -----------------------------------------------------------------------

class Ctx:
    """What a plugin may use. Thin on purpose: these are the bridge modules a bundled plugin
    uses today, plus its own settings and state. Anything else is an import the plugin owns."""

    def __init__(self, pid: str):
        self.id = pid
        self.config = config
        self.data_dir = data_dir(pid)
        self.manifest = manifest(pid)

    def settings(self) -> dict:
        return settings(self.id)

    @property
    def runner(self):
        from bridge import runner
        return runner

    @property
    def store(self):
        from bridge import store
        return store

    def log(self, msg: str) -> None:
        print(f"[plugin:{self.id}] {msg}", file=sys.stderr, flush=True)

    def notify(self, text: str) -> None:
        """A line to the owner on Telegram (best effort; no Telegram, no line)."""
        try:
            from bridge import dispatch
            for cid in sorted(config.ALLOWED_CHAT_IDS)[:1]:
                dispatch.send(cid, text)
        except Exception as e:  # noqa: BLE001 - a notification never fails a plugin
            self.log(f"notify failed: {e}")


def _guard(pid: str, fn: Callable[[], Any], default: Any = None) -> Any:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - a plugin's fault stays its own
        _broken[pid] = f"{type(e).__name__}: {str(e)[:200]}"
        print(f"[plugin:{pid}] failed: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        return default


def _load(pid: str) -> Any:
    m = manifest(pid)
    if not m or not m["entry"]:
        raise PluginError(_broken.get(pid) or f"unknown plugin {pid}")
    modname = f"mystical_plugin_{pid.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(modname, os.path.join(m["dir"], m["entry"]))
    if spec is None or spec.loader is None:
        raise PluginError(f"{pid}: cannot load {m['entry']}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    factory = getattr(mod, "Plugin", None) or getattr(mod, "plugin", None)
    if factory is None:
        raise PluginError(f"{pid}: {m['entry']} defines no Plugin class or plugin() factory")
    return factory() if callable(factory) else factory


def start(pid: str) -> bool:
    """Import and start one plugin. False (and `broken` set) on any failure."""
    with _lock:
        if pid in _live:
            return True
        _broken.pop(pid, None)
        inst = _guard(pid, lambda: _load(pid))
        if inst is None:
            return False
        ok = _guard(pid, lambda: (inst.start(Ctx(pid)) if hasattr(inst, "start") else None) or True, default=False)
        if ok is False:
            return False
        _live[pid] = inst
        return True


def stop(pid: str) -> None:
    """Stop one plugin. A plugin that refuses (work in flight) raises PluginError up to the caller."""
    with _lock:
        inst = _live.get(pid)
        if inst is None:
            return
        if hasattr(inst, "stop"):
            try:
                inst.stop()
            except PluginError:
                raise
            except Exception as e:  # noqa: BLE001
                _broken[pid] = f"stop: {e}"
        _live.pop(pid, None)
        sys.modules.pop(f"mystical_plugin_{pid.replace('-', '_')}", None)


def set_enabled(pid: str, on: bool) -> None:
    if on:
        start(pid)
    else:
        stop(pid)


def start_installed(installed: Callable[[str], bool]) -> None:
    """At boot: start every plugin whose install flag is on."""
    for pid in ids():
        if installed(pid):
            start(pid)


def stop_all() -> None:
    for pid in list(_live):
        try:
            stop(pid)
        except PluginError:
            pass


def is_live(pid: str) -> bool:
    return pid in _live


def broken(pid: str) -> "str | None":
    return _broken.get(pid)


def status(pid: str) -> dict:
    inst = _live.get(pid)
    if _broken.get(pid):
        return {"line": _broken[pid], "ok": False}
    if inst is None:
        return {"line": "", "ok": True}
    if not hasattr(inst, "status"):
        return {"line": "on", "ok": True}
    out = _guard(pid, lambda: inst.status(), default={"line": _broken.get(pid, "status failed"), "ok": False})
    return out if isinstance(out, dict) else {"line": str(out), "ok": True}


def summary(pid: str) -> str:
    return str(status(pid).get("line") or "")


def card(pid: str, installed: bool) -> dict:
    """The extra fields integrations.state() adds for an external plugin."""
    m = manifest(pid) or {}
    return {"ext": True, "version": m.get("version"), "source": m.get("source"),
            "pane": m.get("pane"), "settings": settings_view(pid) if installed else [],
            "status": status(pid) if installed else {"line": "", "ok": True},
            "broken": _broken.get(pid)}


# ---------- the seams -----------------------------------------------------------------------

def owner_of(path: str) -> "str | None":
    m = re.match(r"^/(?:api/)?plugin/([a-z][a-z0-9-]{1,40})(?:/|$)", path)
    return m.group(1) if m else None


def dispatch(method: str, path: str, qs: dict, body: "dict | None", chat: "int | None") -> "tuple[dict, int]":
    """Both servers' `/plugin/<id>/...` door. The owner is installed (the door checked);
    here: settings routes the bridge answers for every plugin, then the plugin's own."""
    pid = owner_of(path)
    if not pid:
        return {"error": "not found"}, 404
    sub = re.sub(r"^/(?:api/)?plugin/[a-z0-9-]+", "", path) or "/"
    if sub == "/settings":
        if method == "GET":
            return {"settings": settings_view(pid)}, 200
        if method == "POST":
            try:
                save_settings(pid, (body or {}).get("values") or {})
            except ValueError as e:
                return {"error": str(e)}, 400
            return {"ok": True, "settings": settings_view(pid)}, 200
    if sub == "/status" and method == "GET":
        return {"status": status(pid), "live": is_live(pid)}, 200
    inst = _live.get(pid)
    if inst is None:
        return {"error": _broken.get(pid) or "plugin is not running"}, 503
    routes = _guard(pid, lambda: inst.routes() if hasattr(inst, "routes") else {}, default={}) or {}
    h = routes.get((method, sub))
    if h is None:
        return {"error": "not found"}, 404
    out = _guard(pid, lambda: h({"method": method, "path": sub, "qs": qs, "body": body or {}, "chat": chat}))
    if out is None:
        return {"error": _broken.get(pid) or "plugin failed"}, 500
    if isinstance(out, tuple) and len(out) == 2:
        return out[0], int(out[1])
    return out, 200


def asset(path: str) -> "tuple[bytes, str] | None":
    """`/plugin/<id>/ui/<file>` → (bytes, content-type), from the plugin's ui/ dir only."""
    m = re.match(r"^/plugin/([a-z][a-z0-9-]{1,40})/ui/(.+)$", path)
    if not m:
        return None
    mf = manifest(m.group(1))
    if not mf:
        return None
    root = os.path.realpath(os.path.join(mf["dir"], "ui"))
    fp = os.path.realpath(os.path.join(root, m.group(2)))
    if not fp.startswith(root + os.sep) or not os.path.isfile(fp):
        return None
    ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
    if fp.endswith(".js") or fp.endswith(".mjs"):
        ctype = "text/javascript; charset=utf-8"
    with open(fp, "rb") as f:
        return f.read(), ctype


def tools_for(s: dict) -> list:
    """Gateway: every live plugin's tool definitions (each name prefixed `<id>__` so two
    plugins can both offer `status`)."""
    out: list = []
    for pid, inst in list(_live.items()):
        if not hasattr(inst, "tools"):
            continue
        defs = _guard(pid, lambda: inst.tools(), default=[]) or []
        for t in defs:
            t = dict(t)
            t["name"] = f"{pid.replace('-', '_')}__{t['name']}"
            out.append(t)
    return out


def owns_tool(name: str) -> "str | None":
    if "__" not in name:
        return None
    pid = name.split("__", 1)[0].replace("_", "-")
    return pid if pid in _live else None


def call_tool(s: dict, name: str, args: dict) -> str:
    pid = owns_tool(name)
    inst = _live.get(pid) if pid else None
    if inst is None or not hasattr(inst, "call_tool"):
        return f"{name} isn't available."
    inner = name.split("__", 1)[1]
    out = _guard(pid, lambda: inst.call_tool(s, inner, args or {}))
    return str(out) if out is not None else f"{pid} failed: {_broken.get(pid, 'error')}"


def on_command(chat_id: int, text: str) -> "str | None":
    """Telegram: `/<cmd> rest` → the first live plugin whose manifest lists the command."""
    cmd, _, rest = text.strip().partition(" ")
    for pid, inst in list(_live.items()):
        m = manifest(pid) or {}
        if cmd.lstrip("/") in m.get("telegram", []) and hasattr(inst, "telegram"):
            handlers = _guard(pid, lambda: inst.telegram(), default={}) or {}
            h = handlers.get(cmd) or handlers.get(cmd.lstrip("/"))
            if h:
                out = _guard(pid, lambda: h(chat_id, rest.strip()))
                return str(out) if out is not None else f"{pid}: {_broken.get(pid, 'failed')}"
    return None


# ---------- the CLI (bin/mystical plugin …) ---------------------------------------------------

def cli(argv: list) -> int:
    """ls | add <git-url|path> | rm <id> | update [<id>]. External plugins only live in the
    state dir; bundled ones are listed, never added or removed here."""
    cmd = argv[0] if argv else "ls"
    if cmd == "ls":
        for m in registry(refresh=True):
            b = _broken.get(m["id"])
            print(f"{m['id']:18} {m['version']:8} {m['source']:9} {m['dir']}" + (f"  BROKEN: {b}" if b else ""))
        return 0
    if cmd == "add" and len(argv) > 1:
        src = argv[1]
        os.makedirs(_ext_dir(), exist_ok=True)
        name = re.sub(r"\.git$", "", os.path.basename(src.rstrip("/")))
        dest = os.path.join(_ext_dir(), name)
        if os.path.exists(dest):
            print(f"{dest} exists; use `plugin update {name}`")
            return 1
        if os.path.isdir(src):
            os.symlink(os.path.realpath(src), dest)
        else:
            r = subprocess.run(["git", "clone", "--depth", "1", src, dest], capture_output=True, text=True)
            if r.returncode:
                print(r.stderr.strip() or "clone failed")
                return 1
        m = next((x for x in registry(refresh=True) if x["dir"] == dest), None)
        print(f"added {m['id'] if m else name} at {dest}; install it in SETTINGS ▸ INTEGRATIONS (or press ⟳ first if the bridge predates it)")
        return 0
    if cmd == "rm" and len(argv) > 1:
        m = manifest(argv[1])
        if not m or m["source"] != "external":
            print("not an external plugin")
            return 1
        if os.path.islink(m["dir"]):
            os.unlink(m["dir"])
        else:
            import shutil
            shutil.rmtree(m["dir"])
        registry(refresh=True)
        print(f"removed {argv[1]} (its settings and state stay in {data_dir(argv[1])})")
        return 0
    if cmd == "update":
        rc = 0
        for m in registry(refresh=True):
            if m["source"] != "external" or (len(argv) > 1 and m["id"] != argv[1]) or os.path.islink(m["dir"]):
                continue
            r = subprocess.run(["git", "-C", m["dir"], "pull", "--ff-only"], capture_output=True, text=True)
            print(f"{m['id']}: {(r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else 'ok'}")
            rc = rc or r.returncode
        return rc
    print("usage: mystical plugin ls | add <git-url|path> | rm <id> | update [<id>]")
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
