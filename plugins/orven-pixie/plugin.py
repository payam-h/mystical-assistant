"""Pixie dev team plugin: Pixie's dev tickets, worked here through the work list and a run.

Pixie (the Telegram ops assistant, pixie.orven.am) keeps a board. A ticket of kind `dev` names a
workspace (one of the owner's umbrella folders, e.g. ~/Desktop/flixdrama), a repo path inside it, a
spec, acceptance checks and whether screenshots are wanted. This plugin polls `/api/dev/claim` with
the dev-team token and, per ticket:

  1. puts one item on the repo's **work list** (bridge/worklist.py) with source {kind: "pixie"},
  2. starts a **run** (bridge/workrun.py) in a manager chat of its own for that repo — a helper in
     its own worktree does the work, the manager reviews and merges into the run branch; evidence
     (screenshots, logs under .mystical/scratch/) is kept per item; the project's check command
     runs before and after,
  3. when the run finishes, publishes the run branch as `pixie/<id>-<slug>`, opens a PR
     (bridge/github.create_pr) and reports back to Pixie: summary, the helper's decisions and
     "not verified", the checks, the PR, the screenshots — Pixie shows the owner ✅ / 🔁 / ❌.
  4. a 🔁 note comes back as a revision: a helper session in a worktree of the PR branch applies the
     note, pushes, and the report goes back again.

Why upstream's run machinery and not a bare session: it already gives isolation (worktrees), a
reviewer (the manager), reports shaped for a decision, kept evidence and before/after checks — the
exact things a dev ticket needs — and the owner can watch it in the sidebar. Only a person may start
a run; the owner's ✅ on Pixie's approval card is that person, so starting from here is that tap.

What the helper does NOT get for free: the workspace's CLAUDE.md and memory live in the umbrella
folder, and a worktree under BASE_PATH/.worktrees is outside it. So the item's detail names the
knowledge files to read first (PIXIE_KNOWLEDGE), and the manager chat's cwd is the repo inside the
umbrella folder, where Claude Code loads the parents' CLAUDE.md on its own.

The work list is an experimental feature; starting this plugin switches it on (experimental.set_enabled).
Stdlib only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable

PICTURE = (".png", ".jpg", ".jpeg", ".webp")
MAX_SHOTS = 8


def http(url: str, body: dict | None = None, headers: dict | None = None, method: str | None = None, timeout: int = 60) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text, code = r.read().decode("utf-8", "replace"), r.status
    except urllib.error.HTTPError as e:
        text, code = e.read().decode("utf-8", "replace"), e.code
    try:
        return code, (json.loads(text) if text else {})
    except ValueError:
        return code, text


def first_paragraph(text: str, limit: int = 700) -> str:
    """The lead of a report: up to its first blank line, bullets and headings stripped of markdown noise."""
    lead = (text or "").strip().split("\n\n")[0].strip()
    return re.sub(r"\*\*|`", "", lead)[:limit]


def check_line(v: dict) -> str:
    """One line for a check record from workrun: `pytest: 41 passed, 0 failed` plus new failures."""
    cmd = str(v.get("command") or "check").split()[0]
    if v.get("state") == "failed":
        return f"{cmd}: could not run"
    bits = []
    if v.get("passed") is not None or v.get("failed") is not None:
        bits.append(f"{v.get('passed') or 0} passed, {v.get('failed') or 0} failed")
    elif v.get("code") is not None:
        bits.append("exit 0" if v.get("code") == 0 else f"exit {v.get('code')}")
    if v.get("new"):
        bits.append("NEW failures: " + "; ".join(str(x)[:80] for x in v["new"][:5]))
    if "ok" in v:
        bits.append("ok" if v["ok"] else "not ok")
    return f"{cmd}: " + (", ".join(bits) if bits else "ran")


def slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s or "ticket")[:40]


def resolve_paths(ticket: dict, allowed: list[str]) -> tuple[str, str]:
    """(workspace dir, repo dir) — the workspace must be one of the allowed umbrella folders, the repo inside it."""
    ws_raw = str(ticket.get("workspace") or "")
    ws = os.path.realpath(os.path.expanduser(ws_raw))
    allowed_real = {os.path.realpath(os.path.expanduser(a)) for a in allowed if a}
    if ws not in allowed_real:
        raise RuntimeError(f"workspace {ws_raw} is not one of this machine's dev workspaces")
    repo = os.path.realpath(os.path.join(ws, str(ticket.get("repo") or "")))
    # "." (or empty) means the workspace is itself the repo, e.g. ~/Desktop/pixie
    if not (repo == ws or repo.startswith(ws + os.sep)) or not os.path.isdir(repo):
        raise RuntimeError(f"repo {ticket.get('repo')} is not a folder inside {ws_raw}")
    return ws, repo


def item_detail(ticket: dict, ws: str, knowledge: list[str]) -> str:
    """What the helper is told, beyond the title: the spec, the acceptance checks, the knowledge files,
    the screenshot ask, and the report shape Pixie parses."""
    files = []
    for k in knowledge:
        p = os.path.expanduser(k.strip())
        if not os.path.isabs(p):
            p = os.path.join(ws, p)
        if os.path.isfile(p):
            files.append(p)
    parts = [str(ticket.get("details") or "").strip()]
    if ticket.get("acceptance"):
        parts.append(f"Acceptance — it is done when:\n{ticket['acceptance']}")
    if files:
        parts.append("Read these first; they hold how this platform works and what has gone wrong before:\n" + "\n".join(f"- {f}" for f in files))
    parts.append("Work on this repo only. Run the repo's own checks (tests, typecheck, build) and say what you ran. "
                 "Do not deploy, do not touch servers, do not change .env files.")
    if ticket.get("screenshots"):
        parts.append("This touches something people see: capture BEFORE and AFTER screenshots of the changed screen(s) as PNG files into "
                     ".mystical/scratch/ (e.g. .mystical/scratch/after-episode-page.png). For a web app start its dev server and use the "
                     "bridge-eyes approach (node .claude/skills/bridge-eyes/shot.mjs or headless chrome); for the Android app, attach Compose "
                     "previews or golden images where they exist and say so if a screenshot was not possible.")
    parts.append(f"This is Pixie dev ticket #{ticket.get('id')} for {ticket.get('reporter') or 'the owner'}.")
    return "\n\n".join(p for p in parts if p)


class Work:
    def __init__(self, ticket: dict, ws: str, repo: str):
        self.ticket, self.ws, self.repo = ticket, ws, repo
        self.id = int(ticket["id"])
        self.started = time.time()
        self.root: str | None = None
        self.chat: dict | None = None
        self.item_id: str | None = None
        self.run_id: str | None = None
        self.state = "starting"
        self.branch: str | None = None
        self.pr_url: str | None = None


class Plugin:
    def __init__(self):
        self.ctx = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.current: Work | None = None
        self.online = False
        self.last_error: str | None = None
        self.last_poll_at: float | None = None
        self.recent: list[dict] = []
        self.chats: dict[str, str] = {}       # repo dir -> manager chat session id
        self.http: Callable = http
        # injectable for tests
        self.start_run: Callable | None = None       # (work) -> None  (puts item, starts run + manager turn)
        self.wait_run: Callable | None = None        # (work, timeout) -> dict (final item/run facts)
        self.publish: Callable | None = None         # (work, facts) -> (branch, pr_url)
        self.revise_run: Callable | None = None      # (work, note) -> facts

    # --- lifecycle ---------------------------------------------------------------------------
    def start(self, ctx) -> None:
        self.ctx = ctx
        self._load()
        try:
            from bridge import experimental
            if not experimental.enabled():
                experimental.set_enabled(True)
                ctx.log("experimental features switched on: the work list is one")
        except Exception as e:  # noqa: BLE001
            ctx.log(f"could not check experimental: {e}")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pixie-devteam", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self.current:
            from bridge import plugins_ext
            raise plugins_ext.PluginError(f"dev ticket #{self.current.id} is being worked on — let it finish, then remove the plugin.")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def settings_changed(self, _s: dict) -> None:
        pass

    def _s(self) -> dict:
        return self.ctx.settings()

    def _url(self) -> str:
        return (self._s().get("PIXIE_URL") or "https://pixie.orven.am").rstrip("/")

    def _api(self, path: str, body: dict | None = None, method: str | None = None) -> tuple[int, object]:
        tok = self._s().get("PIXIE_DEV_TOKEN", "")
        if not tok:
            raise RuntimeError("no dev-team token")
        return self.http(f"{self._url()}/api/dev{path}", body, {"X-Dev-Token": tok}, method)

    # --- the loop ----------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                every = max(10, int(self._s().get("PIXIE_POLL_SEC") or 30))
            except ValueError:
                every = 30
            paused = str(self._s().get("PIXIE_PAUSED") or "0") not in ("0", "", "false", "no")
            if self._s().get("PIXIE_DEV_TOKEN") and not paused:
                try:
                    self.poll_once()
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)[:200]
                    self.online = False
            self._stop.wait(every)

    def poll_once(self) -> str:
        """Claim a ticket when free (revisions come through claim too: Pixie orders them first)."""
        self.last_poll_at = time.time()
        with self._lock:
            if self.current:
                return "busy"
        code, data = self._api("/claim", {})
        self.online = code < 400
        if code >= 400:
            self.last_error = f"claim {code}: {data if isinstance(data, str) else (data or {}).get('error', '')}"[:200]
            return "error"
        self.last_error = None
        ticket = (data or {}).get("ticket") if isinstance(data, dict) else None
        if not ticket:
            return "empty"
        allowed = [str(w) for w in ((data or {}).get("workspaces") or [])] or [w.strip() for w in str(self._s().get("PIXIE_WORKSPACES") or "").split(",") if w.strip()]
        try:
            ws, repo = resolve_paths(ticket, allowed)
        except RuntimeError as e:
            self._api(f"/{ticket['id']}/fail", {"error": str(e)})
            self._remember(int(ticket["id"]), str(ticket.get("title") or ""), "failed", str(e), None, None)
            return "failed"
        work = Work(ticket, ws, repo)
        with self._lock:
            self.current = work
        threading.Thread(target=self._work, args=(work,), name=f"pixie-dev-{work.id}", daemon=True).start()
        return "claimed"

    # --- one ticket --------------------------------------------------------------------------
    def _work(self, w: Work) -> None:
        t = w.ticket
        try:
            try:
                max_min = int(self._s().get("PIXIE_MAX_RUN_MIN") or 90)
            except ValueError:
                max_min = 90
            if t.get("dev_state") == "revise" or t.get("revise_note"):
                facts = (self.revise_run or self._revise)(w, str(t.get("revise_note") or ""), max_min * 60)
            else:
                (self.start_run or self._start)(w)
                self._api(f"/{w.id}/progress", {"note": f"run {w.run_id} started in {os.path.relpath(w.repo, w.ws)} (manager chat {w.chat['id'][:8] if w.chat else '?'})",
                                                 "mystical_session": (w.chat or {}).get("id"), "mystical_run": w.run_id})
                facts = (self.wait_run or self._wait)(w, max_min * 60)
                if facts.get("ok"):
                    w.branch, w.pr_url = (self.publish or self._publish)(w, facts)
                    facts["branch"], facts["pr_url"] = w.branch, w.pr_url
            if not facts.get("ok"):
                self._api(f"/{w.id}/fail", {"error": facts.get("why") or "the run did not finish"})
                self._remember(w.id, str(t.get("title") or ""), "failed", facts.get("why") or "", w.run_id, None)
                return
            shots = [{"name": os.path.basename(p), "data": base64.b64encode(open(p, "rb").read()).decode()} for p in facts.get("pictures", [])[:MAX_SHOTS] if os.path.isfile(p) and os.path.getsize(p) < 6_000_000]
            body = {"summary": facts.get("summary") or "(no summary)", "report": facts.get("report"), "tests": facts.get("tests"), "checks": facts.get("checks"), "pr_url": facts.get("pr_url"),
                    "branch": facts.get("branch"), "decisions": facts.get("decisions"), "not_verified": facts.get("not_verified"), "cost_usd": facts.get("cost"), "screenshots": shots}
            code, out = self._api(f"/{w.id}/finish", body)
            if code >= 400:
                raise RuntimeError(f"Pixie refused the report ({code}): {out}")
            self._remember(w.id, str(t.get("title") or ""), "review", "", w.run_id, facts.get("pr_url"))
        except Exception as e:  # noqa: BLE001
            self.ctx.log(f"ticket #{w.id} failed: {e}")
            try:
                self._api(f"/{w.id}/fail", {"error": str(e)[:1500]})
            except Exception:  # noqa: BLE001
                pass
            self._remember(w.id, str(t.get("title") or ""), "failed", str(e)[:200], w.run_id, None)
        finally:
            with self._lock:
                self.current = None

    # --- the run (upstream's worklist + workrun) ----------------------------------------------
    def _manager_chat(self, w: Work) -> dict:
        """One manager chat per repo, kept across tickets; cwd is the repo inside the umbrella folder."""
        from bridge import config, profiles, store
        from bridge.browser import rel
        sid = self.chats.get(w.repo)
        s = store.get_session(sid) if sid else None
        if not s:
            s = store.create_session(config.DASH_CHAT_ID, rel(w.repo), session_id=uuid.uuid4().hex, origin="pixie:devteam",
                                     cwd=w.repo, permission_mode="bypassPermissions", profile_id=profiles.claude_default(rel(w.repo)))
            store.rename(s["id"], f"Dev team · {os.path.basename(w.repo)}")
            self.chats[w.repo] = s["id"]
            self._save()
        return s

    def _start(self, w: Work) -> None:
        from bridge import config, runner, worklist, workrun
        w.chat = self._manager_chat(w)
        w.root = worklist.root_of(w.repo)
        if not w.root:
            raise RuntimeError(f"{w.repo} is not a git repository")
        knowledge = [k for k in str(self._s().get("PIXIE_KNOWLEDGE") or "").split(",") if k.strip()]
        row = {"title": str(w.ticket.get("title") or f"ticket {w.id}")[:200], "detail": item_detail(w.ticket, w.ws, knowledge),
               "source": {"kind": "pixie", "ref": str(w.id), "url": f"{self._url()}/tasks"}}
        items = worklist.add(w.root, [row], session=w.chat["id"])
        w.item_id = items[0]["id"]
        run = workrun.start(w.chat, [w.item_id], 1)
        w.run_id = run["id"]
        w.state = "running"
        prompt = workrun._manager_prompt(w.root, run)
        job = runner.start_streaming_job(config.DASH_CHAT_ID, prompt, [], project=w.repo, permission_mode="bypassPermissions",
                                         session_id=w.chat["id"], origin="pixie:devteam", hang_timeout=3600)
        if job is None:
            raise RuntimeError("the manager chat is busy")

    def _wait(self, w: Work, timeout: float) -> dict:
        """Until the run ends (the manager called finish, or it paused/stopped), then the facts Pixie needs."""
        from bridge import worklist, workrun
        deadline = time.time() + timeout
        last_state = None
        while time.time() < deadline and not self._stop.is_set():
            data = worklist.load(w.root)
            run = next((r for r in data["runs"] if r["id"] == w.run_id), None)
            item = next((i for i in data["items"] if i["id"] == w.item_id), None)
            if run is None:
                return {"ok": False, "why": "the run disappeared"}
            state = (run.get("state"), (item or {}).get("state"))
            if state != last_state:
                last_state = state
                try:
                    self._api(f"/{w.id}/progress", {"note": f"run {run.get('state')} · item {(item or {}).get('state')}"})
                except Exception:  # noqa: BLE001
                    pass
            if run.get("state") == "finished":
                run = self._settled(w, run, min(600, max(30, deadline - time.time())))
                return self._facts(w, run, item or {}, ok=(item or {}).get("state") == "done")
            if run.get("state") in ("paused", "stopped"):
                return {"ok": False, "why": f"the run {run.get('state')}: {run.get('paused_why') or 'see the work list'}", **self._facts(w, run, item or {}, ok=False)}
            self._stop.wait(10)
        # out of time: stop the run so the repo is not left mid-flight
        try:
            data = worklist.load(w.root)
            run = next((r for r in data["runs"] if r["id"] == w.run_id), None)
            if run and run.get("state") == "running":
                workrun._stop(w.root, run)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "why": f"the ticket exceeded {int(timeout // 60)} minutes"}

    def _settled(self, w: Work, run: dict, timeout: float) -> dict:
        """The finished run once its final check (run on the whole branch, in the background) has an answer."""
        from bridge import worklist
        end = time.time() + timeout
        while time.time() < end and not self._stop.is_set():
            if (run.get("check") or {}).get("state") != "running":
                return run
            self._stop.wait(5)
            data = worklist.load(w.root)
            run = next((r for r in data["runs"] if r["id"] == w.run_id), run)
        return run

    def _facts(self, w: Work, run: dict, item: dict, ok: bool) -> dict:
        root = w.root or w.repo
        pictures = []
        ev = item.get("evidence") or {}
        for rel_p in ev.get("pictures") or []:
            p = os.path.join(root, rel_p)
            if os.path.isfile(p):
                pictures.append(p)
        if not pictures:
            d = os.path.join(root, ".mystical", "runs", str(run.get("id")), str(item.get("id") or ""))
            if os.path.isdir(d):
                for dp, _, names in os.walk(d):
                    for n in sorted(names):
                        if n.lower().endswith(PICTURE):
                            pictures.append(os.path.join(dp, n))
        checks = {}
        for k, label in (("baseline", "before"), ("check", "after")):
            v = run.get(k)
            if isinstance(v, dict) and v.get("state") not in (None, "running"):
                checks[label] = check_line(v)
        # The card the owner sees carries the manager's short summary; the helper's whole report goes along
        # as `report` for the board, so a long reply never spills the card into several messages.
        summary = (run.get("summary") or "").strip() or first_paragraph(item.get("summary") or item.get("report") or "")
        report = (item.get("report") or "").strip()
        return {"ok": ok, "summary": summary[:1200] or "(the run left no summary)", "report": report[:12000] or None, "decisions": item.get("decisions") or None,
                "not_verified": item.get("unverified") or None, "checks": checks or None, "pictures": pictures, "tests": checks.get("after"), "cost": None}

    def _publish(self, w: Work, facts: dict) -> tuple[str | None, str | None]:
        """The run branch becomes pixie/<id>-<slug>, pushed, with a PR into the branch the run started from."""
        from bridge import github, worklist
        data = worklist.load(w.root)
        run = next((r for r in data["runs"] if r["id"] == w.run_id), None)
        if not run:
            return None, None
        top = run["top"]
        branch = f"pixie/{w.id}-{slug(str(w.ticket.get('title') or ''))}"
        subprocess.run(["git", "-C", top, "branch", "-f", branch, run["branch"]], capture_output=True, text=True, timeout=30)
        base = run.get("into") or str(w.ticket.get("base_branch") or "main")
        title = f"[dev team #{w.id}] {str(w.ticket.get('title') or '')[:70]}"
        body = (f"Pixie dev ticket #{w.id} — {self._url()}/tasks\n\n{facts.get('summary') or ''}\n\n"
                + (f"Decisions: {facts.get('decisions')}\n" if facts.get("decisions") else "") + (f"Not verified: {facts.get('not_verified')}\n" if facts.get("not_verified") else "")
                + (f"\nChecks: {json.dumps(facts.get('checks'))}\n" if facts.get("checks") else "") + "\nMade by Mystical's work run; review before merging. Nothing was deployed.")
        ok, info = github.create_pr(top, branch, base, title, body)
        if not ok:
            self.ctx.log(f"PR for #{w.id} failed: {info.get('output', '')[:200]}")
            return branch, None
        return branch, info.get("url") or None

    def _revise(self, w: Work, note: str, timeout: float) -> dict:
        """A helper session in a worktree of the PR branch applies the owner's note and pushes."""
        from bridge import config, git, profiles, runner, store
        from bridge.browser import rel
        branch = str(w.ticket.get("result", {}).get("branch") or f"pixie/{w.id}-{slug(str(w.ticket.get('title') or ''))}") if isinstance(w.ticket.get("result"), dict) else f"pixie/{w.id}-{slug(str(w.ticket.get('title') or ''))}"
        top = subprocess.run(["git", "-C", w.repo, "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or w.repo
        path = os.path.join(config.BASE_PATH, ".worktrees", os.path.basename(top), f"pixie-{w.id}")
        if not os.path.isdir(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ok, msg = git.worktree_add(top, path, branch, "", create=False)
            if not ok:
                return {"ok": False, "why": f"could not check out {branch}: {msg[:200]}"}
        session = store.create_session(config.DASH_CHAT_ID, rel(path), session_id=uuid.uuid4().hex, origin="pixie:devteam", cwd=path,
                                       permission_mode="bypassPermissions", profile_id=profiles.claude_default(rel(path)))
        store.rename(session["id"], f"Dev team · revise #{w.id}")
        prev = w.ticket.get("result") if isinstance(w.ticket.get("result"), dict) else {}
        prompt = (f"You are revising Pixie dev ticket #{w.id} «{w.ticket.get('title')}» on branch {branch} (this worktree). The previous report said:\n\n"
                  f"{(prev.get('summary') or '')[:2500]}\n\nThe owner reviewed it and asks:\n\n{note}\n\nApply exactly that. Run the repo's checks, "
                  f"commit with a clear message, and `git push`. If screenshots are relevant, save PNGs under .mystical/scratch/. End your final reply with "
                  f"\"Decisions:\" and \"Not verified:\" lists (say none if so). Do not deploy.")
        job = runner.start_streaming_job(config.DASH_CHAT_ID, prompt, [], project=path, permission_mode="bypassPermissions", session_id=session["id"],
                                         origin="pixie:devteam", hang_timeout=timeout)
        if job is None:
            return {"ok": False, "why": "could not start the revision session"}
        deadline = time.time() + timeout
        while not job.exited.is_set() and time.time() < deadline and not self._stop.is_set():
            self._stop.wait(5)
        ok = job.exited.is_set() and job.status == "done" and not job.interrupted and not job.timed_out
        pictures = []
        scratch = os.path.join(path, ".mystical", "scratch")
        if os.path.isdir(scratch):
            for n in sorted(os.listdir(scratch)):
                if n.lower().endswith(PICTURE):
                    pictures.append(os.path.join(scratch, n))
        text = (job.result or "\n".join(job.texts or [])).strip()
        from bridge import workrun
        parsed = workrun.read_report(text) if text else {}
        git.push(path)
        return {"ok": ok, "why": None if ok else (job.error_msg or "the revision did not finish"), "summary": (parsed.get("summary") or first_paragraph(text))[:1200], "report": text[:12000] or None, "decisions": parsed.get("decisions") or None,
                "not_verified": parsed.get("unverified") or None, "pictures": pictures, "branch": branch, "pr_url": w.ticket.get("pr_url"), "cost": job.cost, "checks": None, "tests": None}

    # --- state -------------------------------------------------------------------------------
    def _path(self) -> str:
        return os.path.join(self.ctx.data_dir, "state.json")

    def _load(self) -> None:
        try:
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            self.chats = dict(d.get("chats") or {})
            self.recent = list(d.get("recent") or [])[:20]
        except (OSError, ValueError):
            self.chats, self.recent = {}, []

    def _save(self) -> None:
        try:
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump({"chats": self.chats, "recent": self.recent[:20]}, f)
        except OSError:
            pass

    def _remember(self, tid: int, title: str, status: str, why: str, run_id, pr_url) -> None:
        self.recent.insert(0, {"ticket": tid, "title": title[:80], "status": status, "why": (why or "")[:200], "run": run_id, "pr": pr_url, "at": time.time()})
        self.recent = self.recent[:20]
        self._save()

    # --- routes, telegram, status ------------------------------------------------------------
    def _view(self) -> dict:
        s = self._s()
        c = self.current
        return {"url": self._url(), "configured": bool(s.get("PIXIE_DEV_TOKEN")), "paused": str(s.get("PIXIE_PAUSED") or "0") not in ("0", "", "false", "no"),
                "online": self.online, "lastPollAt": self.last_poll_at, "error": self.last_error,
                "current": {"ticket": c.id, "title": c.ticket.get("title"), "repo": os.path.relpath(c.repo, c.ws), "state": c.state, "run": c.run_id, "chat": (c.chat or {}).get("id"), "seconds": int(time.time() - c.started)} if c else None,
                "recent": self.recent, "managers": len(self.chats)}

    def routes(self) -> dict:
        return {
            ("GET", "/state"): lambda req: (self._view(), 200),
            ("POST", "/poll"): lambda req: ({"did": self.poll_once(), **self._view()}, 200),
            ("POST", "/pause"): self._r_pause,
        }

    def _r_pause(self, req):
        from bridge import plugins_ext
        plugins_ext.save_settings("orven-pixie", {"PIXIE_PAUSED": "1" if req["body"].get("paused") else ""})
        return self._view(), 200

    def telegram(self) -> dict:
        def devteam(chat_id: int, rest: str) -> str:
            v = self._view()
            if not v["configured"]:
                return "Dev team: no token yet — SETTINGS ▸ INTEGRATIONS ▸ Pixie dev team."
            cur = f"working on #{v['current']['ticket']} «{v['current']['title']}» ({v['current']['state']}, {v['current']['seconds'] // 60} min)" if v["current"] else "idle"
            last = v["recent"][0] if v["recent"] else None
            return (f"Dev team: {'online' if v['online'] else 'offline'} · {cur}" + (f"\nlast: #{last['ticket']} {last['status']}" + (f" — {last['why']}" if last.get('why') else "") + (f" {last['pr']}" if last.get('pr') else "") if last else "")
                    + (f"\nerror: {v['error']}" if v["error"] else "") + ("\n(paused)" if v["paused"] else ""))
        return {"/devteam": devteam}

    def status(self) -> dict:
        v = self._view()
        if not v["configured"]:
            return {"line": "no token", "ok": True}
        if v["error"] and not v["online"]:
            return {"line": f"offline — {v['error']}", "ok": False}
        return {"line": ("online" if v["online"] else "starting") + (f" · #{v['current']['ticket']} {v['current']['state']}" if v["current"] else " · idle") + (" · paused" if v["paused"] else ""), "ok": True}
