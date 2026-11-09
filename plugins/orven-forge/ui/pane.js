// FORGE pane — slots and what runs in them, the last runs, pause/claim. Plain DOM via ctx.api().
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};
const ago = (ts) => (ts ? `${Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))} min ago` : "never");
const mins = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

export function mount(root, ctx) {
  root.classList.add("fg");
  let v = null, err = "", busy = "";
  const render = () => {
    root.replaceChildren();
    if (err) root.append(h("div", { class: "fg-err" }, err));
    if (!v) { root.append(h("div", { class: "fg-dim" }, "Reading…")); return; }
    root.append(h("section", { class: "fg-sec" },
      h("h3", {}, "AGENT"),
      h("div", { class: "fg-grid" },
        h("span", { class: "fg-k" }, "forge"), h("span", {}, v.url),
        h("span", { class: "fg-k" }, "state"), h("span", { class: v.configured ? (v.online ? "fg-ok" : "fg-warn") : "fg-dim" }, !v.configured ? "no agent token (card in SETTINGS ▸ INTEGRATIONS)" : v.online ? "online" : "offline", v.paused ? " · paused" : ""),
        h("span", { class: "fg-k" }, "heartbeat"), h("span", { class: "fg-dim" }, ago(v.lastBeatAt)),
        h("span", { class: "fg-k" }, "last claim"), h("span", { class: "fg-dim" }, ago(v.lastClaimAt)),
        h("span", { class: "fg-k" }, "agent id"), h("code", {}, v.agentId || "—"),
        v.error ? h("span", { class: "fg-k" }, "error") : null, v.error ? h("span", { class: "fg-warn" }, v.error) : null),
      h("div", { class: "fg-row" },
        h("button", { class: "fg-btn", disabled: busy || !v.configured ? "" : null, onclick: () => act("claim", () => ctx.api("/claim", { json: {} })) }, busy === "claim" ? "CLAIMING…" : "CLAIM NOW"),
        h("button", { class: "fg-btn fg-ghost", disabled: busy ? "" : null, onclick: () => act("pause", () => ctx.api("/pause", { json: { paused: !v.paused } })) }, v.paused ? "RESUME" : "PAUSE"))));
    const slots = Array.from({ length: v.slots }, (_, i) => v.running.find((r) => r.slot === i) || null);
    root.append(h("section", { class: "fg-sec" }, h("h3", {}, `SLOTS · ${v.running.length}/${v.slots}`),
      h("div", { class: "fg-slots" }, ...slots.map((r, i) => h("div", { class: "fg-slot" + (r ? " fg-busy" : "") },
        h("span", { class: "fg-k" }, `slot ${i}`),
        r ? h("span", {}, r.label, h("span", { class: "fg-dim" }, ` · ${r.state} · ${mins(r.seconds)}`)) : h("span", { class: "fg-dim" }, "idle"),
        r && r.session ? h("a", { class: "fg-lnk", href: `/?session=${r.session}` }, "session") : null)))));
    root.append(h("section", { class: "fg-sec" }, h("h3", {}, "RECENT"),
      v.recent.length ? h("div", { class: "fg-list" }, ...v.recent.map((x) => h("div", { class: "fg-run " + (x.status === "ok" ? "fg-ok-b" : "fg-bad-b") },
        h("span", {}, x.label, h("span", { class: "fg-dim" }, ` · ${x.task.slice(0, 8)}`)),
        h("span", { class: x.status === "ok" ? "fg-ok" : "fg-warn" }, x.status, x.why ? ` — ${x.why}` : ""),
        h("span", { class: "fg-dim" }, `${mins(x.seconds)}${x.cost != null ? ` · $${Number(x.cost).toFixed(2)}` : ""} · ${ago(x.at)}`)))) : h("div", { class: "fg-dim" }, "No runs yet.")));
  };
  async function act(label, fn) {
    busy = label; err = ""; render();
    try { v = await fn(); } catch (e) { err = e.message || String(e); }
    busy = ""; render();
  }
  async function load() { try { v = await ctx.api("/state"); } catch (e) { err = e.message || String(e); } render(); }
  void load();
  const t = setInterval(() => void load(), 10_000);
  return () => clearInterval(t);
}
