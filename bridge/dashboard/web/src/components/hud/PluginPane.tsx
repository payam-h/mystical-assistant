import { useEffect, useRef, useState } from "react";
import type { Integration } from "../../api";

/* fork: plugins — the right-rail pane an out-of-tree plugin brings (bridge/plugins_ext.py).
   The plugin ships an ES module at /plugin/<id>/ui/<entry> exporting
   `mount(el, ctx)` (and optionally the function it returns, to unmount). It
   draws into `el` with plain DOM; the HUD's CSS variables are in scope, so the
   plugin's own stylesheet (pane.css) can lean on --acc, --txb, --panel… The
   module is loaded once per plugin and cached by the browser like any asset.
   `ctx.api(path, init)` reaches the plugin's own routes with the dashboard
   token, so a pane never handles auth itself. Nothing here knows what the
   plugin does — a broken module shows its error in the pane, not in the HUD. */

export interface PaneCtx {
  id: string;
  api: (path: string, init?: RequestInit & { json?: unknown }) => Promise<unknown>;
  settings: Integration["settings"];
  theme: () => string;
}

function token(): string {
  try { return new URLSearchParams(location.search).get("token") || localStorage.getItem("dash-token") || ""; } catch { return ""; }
}

export function PluginPane({ p }: { p: Integration }) {
  const ref = useRef<HTMLDivElement>(null);
  const [err, setErr] = useState("");

  useEffect(() => {
    const el = ref.current;
    if (!el || !p.pane) return;
    let dead = false;
    let unmount: (() => void) | undefined;
    const ctx: PaneCtx = {
      id: p.id,
      settings: p.settings,
      theme: () => document.documentElement.dataset.theme || "",
      api: async (path, init) => {
        const headers: Record<string, string> = { ...(init?.headers as Record<string, string> | undefined) };
        const t = token();
        if (t) headers["X-Dash-Token"] = t;
        let body = init?.body;
        if (init && "json" in init) { headers["Content-Type"] = "application/json"; body = JSON.stringify(init.json); }
        const r = await fetch(`/plugin/${p.id}${path.startsWith("/") ? path : "/" + path}`, { ...init, headers, body, method: init?.method || (body ? "POST" : "GET") });
        const j = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((j as { error?: string }).error || `HTTP ${r.status}`);
        return j;
      },
    };
    if (p.pane.css) {
      const id = `plugin-css-${p.id}`;
      if (!document.getElementById(id)) {
        const link = document.createElement("link");
        link.id = id; link.rel = "stylesheet"; link.href = `/plugin/${p.id}/ui/${p.pane.css}`;
        document.head.appendChild(link);
      }
    }
    import(/* @vite-ignore */ `/plugin/${p.id}/ui/${p.pane.entry}`)
      .then((mod: { mount?: (el: HTMLElement, ctx: PaneCtx) => void | (() => void) }) => {
        if (dead) return;
        if (typeof mod.mount !== "function") throw new Error(`${p.pane!.entry} exports no mount()`);
        const out = mod.mount(el, ctx);
        if (typeof out === "function") unmount = out;
      })
      .catch((e: Error) => { if (!dead) setErr(e.message); });
    return () => { dead = true; try { unmount?.(); } catch { /* a pane's teardown is its own */ } el.replaceChildren(); };
  }, [p.id, p.pane?.entry, p.pane?.css]);  // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}>
      {err && <div style={{ fontSize: "var(--t10)", color: "var(--err)", padding: "8px 10px" }}>{p.label}: {err}</div>}
      <div ref={ref} style={{ flex: 1, minHeight: 0, overflow: "auto" }} />
    </div>
  );
}

/** Several plugins' panes as one tab (manifest pane.group): stacked sections, each with the
    plugin's name and status line, every pane still mounted by its own module. */
export function PluginGroupPane({ ps }: { ps: Integration[] }) {
  return (
    <div style={{ height: "100%", overflow: "auto", display: "flex", flexDirection: "column" }}>
      {ps.map((p) => (
        <section key={p.id} style={{ borderBottom: "1px solid color-mix(in srgb, var(--acc) 12%, transparent)", display: "flex", flexDirection: "column" }}>
          <header style={{ display: "flex", alignItems: "baseline", gap: 8, padding: "8px 12px 0", fontSize: "var(--t85, 9px)", letterSpacing: "1.5px", color: "var(--txl)", fontWeight: 600 }}>
            <span>{p.pane?.title || p.label.toUpperCase()}</span>
            {p.status?.line ? <span style={{ fontWeight: 400, letterSpacing: 0, textTransform: "none", color: p.status.ok === false ? "var(--err)" : "var(--txd)", fontSize: "var(--t9, 10px)" }}>{p.status.line}</span> : null}
          </header>
          <PluginPane p={p} />
        </section>
      ))}
    </div>
  );
}
