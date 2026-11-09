# mystical-assistant

A bridge that runs Claude Code on this machine, driven from three surfaces:
a **Telegram bot** (`bridge/dispatch.py`), a **Telegram Mini App**
(`bridge/miniapp/`, port 8787), and a **localhost dashboard**
(`bridge/dashboard/`, port 8790). Landing page on 8791. Python stdlib only on
the backend — no framework, no ORM, no async runtime. On macOS, **Mystical.app**
(`macos/`, Swift, system frameworks only) wraps the dashboard in a native window
+ menu bar; `macos/probe.sh` snapshots it without Screen Recording permission,
`macos/capture.sh` captures the real screen through the app (it holds that
permission; a session's shell never can), and `cd macos && swift test` runs its tests.
On Windows (the bridge in WSL), **Mystical.exe** (`windows/app`, C#, WinForms +
WebView2, Microsoft frameworks only) is the same shell, built on the Windows side
by `windows/build.sh` through `dotnet.exe`; `windows/probe.sh` snapshots it,
`windows/capture.sh` captures the real screen through it, and
`windows/build.sh --check` plus `node windows/shim.check.mjs` run its checks.
On a Linux desktop, **linux/window.py** (system python3 + GTK 3/WebKitGTK 4.1, no
build) is the same window: links go to the desktop's default browser, the page
draws its title bar (`isLinuxApp()`), and `mystical app` puts it in the app
launcher. `tests/test_linux_window.py` covers its link policy; the GTK half was
checked on a private Xvfb + dbus-run-session, never the user's own display.

## The one thing that bites everyone

**The running bridge is a snapshot of the code from when it launched.** A
committed fix does nothing until the bridge restarts, and restarting is not
`mystical restart` when you are a session *inside* that bridge — you share its
systemd cgroup and would kill yourself mid-turn. The dashboard's ⟳ (right rail,
above the changelog) shows what's on disk but not running and makes it live: point
the user at it rather than restarting. Details: the **bridge-ship** skill.

## Tests

```sh
python3 -m pytest tests/ -q      # 1007 passing, ~19s (2026-08-18)
```

Fully green. Anything red is your change — there is no known-failure floor.

`tests/conftest.py` pins the environment **before** `bridge.config` is imported,
because config freezes every setting into module constants at import time. A
test that imports `bridge.config` (directly or transitively) before conftest's
pins would run against your real `~/.bridge_state` DB, your real accounts, and
your real chat-id allow-list. Never add env setup inside a test module's
preamble — put it in conftest, above the first bridge import.

## Conventions

- **`ponytail:` comments are load-bearing.** They mark a deliberate shortcut and
  name its ceiling. Don't "fix" one without reading what it defers; `/ponytail-debt`
  harvests them.
- **Module docstrings carry the design rationale** — the *why not* as much as the
  what. Read the docstring before changing a module; write one for a new one.
- Specs and plans live in `docs/superpowers/{specs,plans}/slug.md` — named by topic, no date prefix (git has the date, and you search by subject).
- Per-repo runtime state (git-ignored) goes in `.mystical/`: `dev.log`,
  `learn/` lessons, design-sync state, `docs/` for reports and other markdown
  written to be read. It self-ignores from git (`.mystical/.gitignore` is
  `*`), so anything that must survive in the repo — specs, plans, releases —
  stays in `docs/`.
- No external MCP servers load by default (startup cost) — the `mcp-on` skill
  turns one on for a session.

## Project skills

- **bridge-worktree** — where feature work lives, and keeping its data off the live DB.
- **bridge-ship** — build, restart, and confirm a change is actually live.
- **bridge-feature-slice** — wiring a capability across all three surfaces.
- **bridge-eyes** — screenshotting/driving the UIs headlessly to verify frontend work.
- **mystical-assistant-design** — the CRT-HUD brand system, tokens, components.

<!-- FORK NOTES (payam-h/mystical-assistant) — keep this block; everything above is upstream's -->
## Fork notes

This is payam-h's fork. `master` mirrors `ainurdev/master` and is never committed to; `payam` is the
branch the live bridge runs and where our work lands. A weekly action opens the merge PR master → payam.

**Delta budget.** Our changes to upstream files are confined to small fenced blocks
(`# --- fork: plugins ---` … `# --- /fork ---`) in at most these files: `bridge/integrations.py`,
`bridge/dashboard/server.py`, `bridge/miniapp/server.py`, `bridge/gateway.py`, `bridge/dispatch.py`,
`bin/mystical`, one dashboard web component. Everything else of ours is a new file: `bridge/plugins_ext.py`
and `plugins/<id>/`. A change that needs a bigger seam is first proposed upstream.

**Plugins.** `plugins/<id>/plugin.json` + `plugin.py` (spec: `docs/superpowers/specs/plugin-system.md`).
Bundled: `orven` (gate watcher, ORVEN tab), `orven-forge`, `orven-jobs`, `orven-pixie` (Pixie's dev team →
the work list). Their tests live in `plugins/<id>/tests/` and run with the suite.

**Where it lives.** The live fork runs from `~/mystical` (launchd `cloud.ainurhq.mystical.bridge`,
`~/.local/bin/mystical` → its `bin/mystical`); `~/Desktop/mystical` is a symlink to it for the eye only — macOS
denies launchd-spawned processes `~/Desktop` ("Operation not permitted" on run.sh, 2026-11-09), so the real
checkout must stay outside the protected folders. Feature worktrees go under `~/Desktop/.worktrees/mystical/<branch>`
(inside BASE_PATH, as bridge-worktree says; sessions, not launchd, open those). **Updates:** `payam` tracks `upstream/master`, so the right rail's
UPDATE shows what ainurdev shipped; PULL is disabled on a fork (`selfupdate._is_fork`) — merge upstream by hand via
the weekly upstream-sync PR, run the suite, then ⟳. `remote.pushDefault=origin` keeps `git push` on the fork.

**Workspace.** `BASE_PATH` is `~/Desktop`; the owner's umbrella folders (`flixdrama`, `Orven`, `pixie`) are
projects here, and dev-team sessions start in those folders, not in a single repo.
