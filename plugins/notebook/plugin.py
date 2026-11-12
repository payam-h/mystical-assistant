"""The notebook (plugins/notebook): one JSON file of pages beside the bridge's database.

The book itself is ui/book.html, drawn in the browser; this side only keeps it. A page is
{date, entries: [{id, text, done, project, ink, by}], strokes, stickers}. Every write is
whole-state and atomic, and the first write of a day keeps yesterday's file, so a bad save
or a second window never costs more than a day. /note on Telegram writes a line on the
last written page; the book moves it on if that page is full.

Stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import urllib.request

MAX_BYTES = 40 * 1024 * 1024
KEEP_DAYS = 14
PROJECTS_TTL = 3600        # how long the Orven projects list is good for
LINES_A_PAGE = 12          # /note stops adding to a page here; the book reflows what still overflows
# A page is a grid of rows. A line has `gap` (empty rows above it) and `ind` (indent steps); the book owns both.
_TAG = re.compile(r"(?:^|\s)#([^\W\d][\w-]{1,})(?=\s|$)", re.UNICODE)


def blank_page() -> dict:
    return {"date": "", "entries": [], "strokes": [], "stickers": []}


def counts(state: dict | None) -> tuple[int, int]:
    """(open, crossed) over every written line."""
    todo = done = 0
    for pg in (state or {}).get("pages") or []:
        for e in pg.get("entries") or []:
            if not str(e.get("text") or "").strip():
                continue
            if e.get("done"):
                done += 1
            else:
                todo += 1
    return todo, done


def fetch_orven_projects(url: str, token: str) -> list:
    """The panel's projects (name, group, icon) through the device's gate token. Raises on any failure."""
    req = urllib.request.Request(f"{url.rstrip('/')}/api/claude-jobs/agent/projects", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json", "x-gate-token": token, "Cookie": f"orven_gate={token}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r).get("projects") or []


class Plugin:
    def __init__(self):
        self.ctx = None
        self.path = ""
        self._lock = threading.RLock()
        self.now = time.time          # injectable
        self.fetch_projects = fetch_orven_projects   # injectable
        self.gate_token = None        # injectable: () -> token

    def start(self, ctx) -> None:
        self.ctx = ctx
        self.path = os.path.join(ctx.data_dir, "notebook.json")

    # --- the file ------------------------------------------------------------------------
    def read(self) -> dict | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            return None
        return state if isinstance(state, dict) and isinstance(state.get("pages"), list) else None

    def write(self, state: dict) -> None:
        if not isinstance(state, dict) or not isinstance(state.get("pages"), list):
            raise ValueError("a notebook is {pages: [...]}")
        if any(not isinstance(p, dict) for p in state["pages"]):
            raise ValueError("a page is an object")
        raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("the notebook is too large to save")
        with self._lock:
            self._keep_yesterday()
            tmp = self.path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def _keep_yesterday(self) -> None:
        """Before a day's first write, copy the file as it stood; keep the last KEEP_DAYS."""
        if not os.path.isfile(self.path):
            return
        d = os.path.join(os.path.dirname(self.path), "kept")
        os.makedirs(d, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.localtime(self.now()))
        dst = os.path.join(d, f"notebook-{day}.json")
        if os.path.exists(dst):
            return
        shutil.copy2(self.path, dst)
        for old in sorted(f for f in os.listdir(d) if f.startswith("notebook-"))[:-KEEP_DAYS]:
            try:
                os.remove(os.path.join(d, old))
            except OSError:
                pass

    # --- Orven's projects, to label a line with --------------------------------------------
    def _gate(self) -> str | None:
        """This machine's Orven gate token, kept by the Orven plugin. None when it is not enrolled."""
        if self.gate_token:
            return self.gate_token()
        try:
            from bridge import plugins_ext
            with open(os.path.join(plugins_ext.data_dir("orven"), "gate.json"), encoding="utf-8") as f:
                return json.load(f).get("token") or None
        except (OSError, ValueError, ImportError):
            return None

    def orven_projects(self) -> list:
        """[{name, group, logo}] from the Orven panel, kept for an hour; the last good list when the
        panel cannot be reached; [] when this machine is not enrolled. Only icons leave the panel."""
        cache = os.path.join(self.ctx.data_dir, "orven-projects.json")
        old = None
        try:
            with open(cache, encoding="utf-8") as f:
                old = json.load(f)
            if self.now() - os.path.getmtime(cache) < PROJECTS_TTL:
                return old
        except (OSError, ValueError):
            pass
        tok = self._gate()
        if not tok:
            return old or []
        url = (self.ctx.settings().get("NOTEBOOK_ORVEN_URL") if hasattr(self.ctx, "settings") else "") or "https://console.orven.am"
        try:
            rows = self.fetch_projects(url, tok)
        except Exception as e:  # noqa: BLE001 - a panel that is down never breaks the notebook
            if hasattr(self.ctx, "log"):
                self.ctx.log(f"orven projects: {e}")
            return old or []
        out = [{"name": str(p.get("name") or ""), "group": str(p.get("group") or ""),
                "logo": p["imageUrl"] if str(p.get("imageUrl") or "").startswith(("data:image/", "https://")) else ""}
               for p in rows if isinstance(p, dict) and p.get("name")]
        tmp = cache + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
        os.replace(tmp, cache)
        return out

    # --- a line from outside the book ------------------------------------------------------
    def add_line(self, text: str) -> dict:
        text = " ".join(str(text or "").split())
        if not text:
            raise ValueError("nothing to write")
        project = ""
        m = _TAG.search(text)
        if m:
            project = m.group(1).lower()
            text = (text[:m.start()] + " " + text[m.end():]).strip()
        with self._lock:
            return self._add_line(text, project)

    def _add_line(self, text: str, project: str) -> dict:
        state = self.read() or {"v": 1, "skin": "noir", "pages": [blank_page() for _ in range(24)]}
        pages = state["pages"]
        last = max((i for i, p in enumerate(pages) if p.get("entries")), default=0)
        if len(pages[last].get("entries") or []) >= LINES_A_PAGE:
            last += 1
        while len(pages) <= last + 4 or len(pages) % 2:
            pages.append(blank_page())
        pg = pages[last]
        pg.setdefault("entries", [])
        if not pg.get("date"):
            pg["date"] = time.strftime("%a %-d %b", time.localtime(self.now()))
        entry = {"id": f"t{int(self.now() * 1000):x}", "text": text, "done": False, "project": project, "ink": 0}
        pg["entries"].append(entry)
        with self._lock:
            state["rev"] = int(state.get("rev") or 0) + 1
            self.write(state)
        return {"page": last + 1, "entry": entry}

    # --- the plugin contract ---------------------------------------------------------------
    def routes(self) -> dict:
        return {
            ("GET", "/state"): lambda req: ({"state": self.read()}, 200),
            ("POST", "/state"): self._r_save,
            ("POST", "/line"): self._r_line,
            ("GET", "/projects"): lambda req: ({"projects": self.orven_projects()}, 200),
        }

    def _r_save(self, req):
        """Whole-state save. `rev` is the revision the window loaded; when the file has moved on since
        (a /note, the side pane, another window) nothing is written and the window gets the file back to
        merge. A save without `rev` always wins (an older window)."""
        state, base = req["body"].get("state"), req["body"].get("rev")
        with self._lock:
            cur = self.read()
            rev = int((cur or {}).get("rev") or 0)
            if base is not None and cur is not None and int(base) != rev:
                return {"error": "the notebook changed elsewhere", "state": cur, "rev": rev}, 409
            try:
                if isinstance(state, dict):
                    state["rev"] = rev + 1
                self.write(state)
            except ValueError as e:
                return {"error": str(e)}, 400
            return {"ok": True, "rev": rev + 1}, 200

    def _r_line(self, req):
        try:
            return {"ok": True, **self.add_line(req["body"].get("text"))}, 200
        except ValueError as e:
            return {"error": str(e)}, 400

    def telegram(self) -> dict:
        def note(chat_id: int, rest: str) -> str:
            if not rest:
                todo, done = counts(self.read())
                return f"Notebook: {todo} open, {done} crossed out. Write a line with /note <text>; #name files it under a project."
            got = self.add_line(rest)
            return f"Written on page {got['page']}" + (f" under {got['entry']['project']}" if got["entry"]["project"] else "") + "."
        return {"/note": note}

    def status(self) -> dict:
        todo, done = counts(self.read())
        return {"line": f"{todo} open · {done} crossed out", "ok": True}
