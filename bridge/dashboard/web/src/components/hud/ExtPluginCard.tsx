import { useState } from "react";
import { api, type Integration } from "../../api";

/* fork: plugins — the inside of an installed out-of-tree plugin's card in SETTINGS ▸
   INTEGRATIONS: its status line (or why it is broken), and its settings from the
   manifest. Secrets come back masked to a tail and are only ever sent, never
   read back; an empty save clears the override and the manifest default returns.
   The plugin is told (settings_changed) by the bridge, so a save is live. */

const INPUT = {
  flex: 1, minWidth: 0, boxSizing: "border-box", background: "color-mix(in srgb, var(--panel2) 60%, transparent)",
  border: "1px solid color-mix(in srgb, var(--acc) 16%, transparent)", outline: "none", color: "var(--txb)",
  fontFamily: "'JetBrains Mono',monospace", fontSize: "var(--t10)", padding: "5px 8px",
} as const;

export function ExtPluginCard({ p, onChange }: { p: Integration; onChange: () => void }) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const rows = p.settings ?? [];
  const dirty = Object.keys(draft).length > 0;

  async function save() {
    setBusy(true); setErr("");
    try {
      await api.pluginSettings(p.id, draft);
      setDraft({});
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 10 }}>
      <div style={{ fontSize: "var(--t10)", color: p.broken || p.status?.ok === false ? "var(--err)" : "var(--txd)" }}>
        {p.broken ? `broken: ${p.broken}` : p.status?.line || (p.summary || "on")}
        <span style={{ marginInlineStart: 8, color: "var(--txl)" }}>v{p.version} · {p.source}</span>
      </div>
      {rows.map((s) => (
        <label key={s.key} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--t10)" }}>
          <span style={{ width: 150, color: "var(--txd)", letterSpacing: 0.5, flex: "none" }} title={s.hint || s.key}>{s.label}</span>
          <input
            type={s.secret ? "password" : "text"}
            autoComplete="off"
            placeholder={s.secret ? (s.set ? `set (${s.value}) — paste to replace` : "paste the secret") : (s.default || "")}
            value={draft[s.key] ?? (s.secret ? "" : s.value)}
            onChange={(e) => setDraft((d) => ({ ...d, [s.key]: e.target.value }))}
            style={INPUT} />
          {s.set && s.secret && <span style={{ color: "var(--ok)", fontSize: "var(--t9)" }}>set</span>}
        </label>
      ))}
      {err && <div style={{ fontSize: "var(--t10)", color: "var(--err)" }}>{err}</div>}
      {rows.length > 0 && (
        <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
          <button type="button" disabled={!dirty || busy} onClick={() => setDraft({})}
            style={{ appearance: "none", cursor: "pointer", border: "1px solid var(--line2)", background: "transparent", color: "var(--txd)", fontFamily: "inherit", fontSize: "var(--t9)", letterSpacing: 1, padding: "5px 10px", opacity: dirty ? 1 : 0.4 }}>
            RESET
          </button>
          <button type="button" disabled={!dirty || busy} onClick={() => void save()}
            style={{ appearance: "none", cursor: "pointer", border: "1px solid var(--acc)", background: "color-mix(in srgb, var(--acc) 10%, transparent)", color: "var(--txb)", fontFamily: "inherit", fontSize: "var(--t9)", letterSpacing: 1, padding: "5px 10px", opacity: dirty ? 1 : 0.4 }}>
            {busy ? "SAVING…" : "SAVE"}
          </button>
        </div>
      )}
    </div>
  );
}
