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

MAX_BYTES = 40 * 1024 * 1024
KEEP_DAYS = 14
LINES_A_PAGE = 12          # /note stops adding to a page here; the book reflows what still overflows
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


class Plugin:
    def __init__(self):
        self.ctx = None
        self.path = ""
        self._lock = threading.Lock()
        self.now = time.time          # injectable

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
        self.write(state)
        return {"page": last + 1, "entry": entry}

    # --- the plugin contract ---------------------------------------------------------------
    def routes(self) -> dict:
        return {
            ("GET", "/state"): lambda req: ({"state": self.read()}, 200),
            ("POST", "/state"): self._r_save,
            ("POST", "/line"): self._r_line,
        }

    def _r_save(self, req):
        try:
            self.write(req["body"].get("state"))
        except ValueError as e:
            return {"error": str(e)}, 400
        return {"ok": True}, 200

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
