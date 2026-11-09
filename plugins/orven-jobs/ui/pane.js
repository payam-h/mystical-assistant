// JOBS pane — the Orven panel's Claude jobs as this machine runs them: agent state, the current run, recent runs.
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
  root.classList.add("oj");
  let v = null, err = "", busy = "";
  const render = () => {
    root.replaceChildren();
    if (err) root.append(h("div", { class: "oj-err" }, err));
    if (!v) { root.append(h("div", { class: "oj-dim" }, "Reading…")); return; }
    root.append(h("section", { class: "oj-sec" }, h("h3", {}, "AGENT"),
      h("div", { class: "oj-grid" },
        h("span", { class: "oj-k" }, "panel"), h("span", {}, v.url),
        h("span", { class: "oj-k" }, "state"), h("span", { class: !v.hasToken ? "oj-warn" : v.online ? "oj-ok" : "oj-warn" }, !v.hasToken ? "no gate token — enrol in the ORVEN tab" : v.online ? "online" : "offline", v.paused ? " · paused" : ""),
        h("span", { class: "oj-k" }, "heartbeat"), h("span", { class: "oj-dim" }, ago(v.lastBeatAt)),
        h("span", { class: "oj-k" }, "last claim"), h("span", { class: "oj-dim" }, ago(v.lastClaimAt)),
        h("span", { class: "oj-k" }, "sessions"), h("span", { class: "oj-dim" }, `${v.sessions} job session${v.sessions === 1 ? "" : "s"} kept`),
        v.error ? h("span", { class: "oj-k" }, "error") : null, v.error ? h("span", { class: "oj-warn" }, v.error) : null),
      h("div", { class: "oj-row" },
        h("button", { class: "oj-btn", disabled: busy || !v.hasToken ? "" : null, onclick: () => act("claim", () => ctx.api("/claim", { json: {} })) }, busy === "claim" ? "CLAIMING…" : "CLAIM NOW"),
        h("button", { class: "oj-btn oj-ghost", disabled: busy ? "" : null, onclick: () => act("pause", () => ctx.api("/pause", { json: { paused: !v.paused } })) }, v.paused ? "RESUME" : "PAUSE"))));
    root.append(h("section", { class: "oj-sec" }, h("h3", {}, "NOW"),
      v.current ? h("div", { class: "oj-run oj-busy" }, h("span", {}, v.current.name), h("span", { class: "oj-dim" }, mins(v.current.seconds)),
        v.current.session ? h("a", { class: "oj-lnk", href: `/?session=${v.current.session}` }, "session") : h("span", {})) : h("div", { class: "oj-dim" }, "idle")));
    root.append(h("section", { class: "oj-sec" }, h("h3", {}, "RECENT"),
      v.recent.length ? h("div", { class: "oj-list" }, ...v.recent.map((x) => h("div", { class: "oj-run " + (x.status === "ok" ? "oj-ok-b" : "oj-bad-b") },
        h("span", {}, x.name),
        h("span", { class: x.status === "ok" ? "oj-ok" : "oj-warn" }, x.status, x.why ? ` — ${x.why}` : ""),
        h("span", { class: "oj-dim" }, `${mins(x.seconds)}${x.cost != null ? ` · $${Number(x.cost).toFixed(2)}` : ""} · ${ago(x.at)}`)))) : h("div", { class: "oj-dim" }, "No runs yet.")));
  };
  async function act(label, fn) { busy = label; err = ""; render(); try { v = await fn(); } catch (e) { err = e.message || String(e); } busy = ""; render(); }
  async function load() { try { v = await ctx.api("/state"); } catch (e) { err = e.message || String(e); } render(); }
  void load();
  const t = setInterval(() => void load(), 15_000);
  return () => clearInterval(t);
}
