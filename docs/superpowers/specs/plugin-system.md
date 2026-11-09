# Plugin system (fork)

Out-of-tree plugins for the bridge: a manifest, an entry module, one loader, and the
seams it reaches. `bridge/plugins_ext.py`; tests in `tests/test_plugins_ext.py`.

## Why

`bridge/integrations.py` already defines a plugin for the rest of the bridge — id, label,
routes it owns, env keys, session origins, an install flag — and says the table is shaped
to take out-of-tree plugins but nothing loads them. This fork needs four (Orven gate,
Forge, Orven jobs, Pixie dev team) and wants to keep merging upstream weekly, so the loader
keeps upstream's contract and lives in new files; upstream files get fenced blocks only
(`# --- fork: plugins ---` … `# --- /fork ---`): integrations.py, the two servers' doors,
gateway.py, dispatch.py, the boot/shutdown in `claude_telegram_bridge.py`, `bin/mystical`.

## A plugin

```
plugins/<id>/            (bundled, OFF until installed)   or   <state>/plugins/<id>/   (mystical plugin add)
  plugin.json            the manifest
  plugin.py              the entry: class Plugin with optional start/stop/routes/tools/call_tool/telegram/status
  ui/pane.js             optional: an ES module exporting mount(el, ctx) — the pane in the right rail
  (tests: tests/test_plugin_<id>.py, with the suite)
```

Manifest keys: `id` (`^[a-z][a-z0-9-]{1,40}$`), `label`, `version`, `blurb`, `needs`,
`entry` (a .py in the dir), `routes` (all under `/plugin/<id>`), `origins`, `settings`
(`[{key, label, secret, default, hint}]`, keys UPPER_CASE), `pane` (`{title, entry, css,
scope}`), `telegram` (command names).

## What a plugin gets

`ctx`: `config`, `data_dir` (`<state>/plugin-data/<id>/`), `settings()` (saved value or
manifest default; secrets 0600 in `settings.json`, masked to a tail everywhere they are
shown), `runner`, `store`, `log()`, `notify()` (a Telegram line to the owner).

Routes: `routes()` → `{("GET", "/x"): handler}`; the handler gets `{method, path, qs, body,
chat}` and returns `(body, status)`. Both servers reach them: `/plugin/<id>/…` on the
dashboard (same Host and Sec-Fetch-Site gate as `/local/`), `/api/plugin/<id>/…` on the
Mini App (its own auth). The bridge answers `/settings` (GET/POST) and `/status` for every
plugin; `/ui/*` is static from the plugin's `ui/` dir, path-checked.

Tools: `tools()` definitions join the gateway list as `<id>__<name>`; `call_tool(s, name,
args)` answers them. Telegram: `/<cmd>` listed in the manifest reaches `telegram()`'s handler,
owner only.

## Lifecycle and isolation

Discovered plugins start OFF (the Rivendell rule). Install = `start(ctx)`; remove =
`stop()`; a plugin that must not stop mid-work raises `PluginError`, which the page shows
as a refusal and the flag does not flip. Every call into a plugin is wrapped: an exception
marks it `broken` (the message on its card), its routes answer 503, and the bridge keeps
running. A bad manifest is listed as broken under its folder name rather than vanishing.
`mystical plugin ls|add|rm|update` manages the external dir; a plugin added while the
bridge runs appears after ⟳ (the registry is read at import, like `integrations.PLUGINS`).

## Not built

- Sandboxing: a plugin runs in-process with the bridge's rights, like the bundled ones.
- Hot reload of a plugin's code: stop/start re-imports the module, but a plugin added or
  edited is only seen after the bridge restarts, the way everything in this repo is.
- Settings in SETTINGS ▸ SYSTEM (envsettings): a plugin's settings live on its card.
