// DEV TEAM pane — Pixie's dev tickets as this machine works them: state, the current ticket and its run, recent results.
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
  root.classList.add("pd");
  let v = null, err = "", busy = "";
  const render = () => {
    root.replaceChildren();
    if (err) root.append(h("div", { class: "pd-err" }, err));
    if (!v) { root.append(h("div", { class: "pd-dim" }, "Reading…")); return; }
    root.append(h("section", { class: "pd-sec" }, h("h3", {}, "EXECUTOR"),
      h("div", { class: "pd-grid" },
        h("span", { class: "pd-k" }, "pixie"), h("a", { class: "pd-lnk", href: `${v.url}/tasks`, target: "_blank", rel: "noopener" }, v.url),
        h("span", { class: "pd-k" }, "state"), h("span", { class: !v.configured ? "pd-dim" : v.online ? "pd-ok" : "pd-warn" }, !v.configured ? "no token (card in SETTINGS ▸ INTEGRATIONS)" : v.online ? "online" : "offline", v.paused ? " · paused" : ""),
        h("span", { class: "pd-k" }, "last poll"), h("span", { class: "pd-dim" }, ago(v.lastPollAt)),
        h("span", { class: "pd-k" }, "managers"), h("span", { class: "pd-dim" }, `${v.managers} repo chat${v.managers === 1 ? "" : "s"} kept`),
        v.error ? h("span", { class: "pd-k" }, "error") : null, v.error ? h("span", { class: "pd-warn" }, v.error) : null),
      h("div", { class: "pd-row" },
        h("button", { class: "pd-btn", disabled: busy || !v.configured ? "" : null, onclick: () => act("poll", () => ctx.api("/poll", { json: {} })) }, busy === "poll" ? "POLLING…" : "POLL NOW"),
        h("button", { class: "pd-btn pd-ghost", disabled: busy ? "" : null, onclick: () => act("pause", () => ctx.api("/pause", { json: { paused: !v.paused } })) }, v.paused ? "RESUME" : "PAUSE"))));
    root.append(h("section", { class: "pd-sec" }, h("h3", {}, "NOW"),
      v.current ? h("div", { class: "pd-run pd-busy" },
        h("span", {}, `#${v.current.ticket} ${v.current.title || ""}`, h("span", { class: "pd-dim" }, ` · ${v.current.repo} · ${v.current.state}`)),
        h("span", { class: "pd-dim" }, mins(v.current.seconds)),
        v.current.chat ? h("a", { class: "pd-lnk", href: `/?session=${v.current.chat}` }, "manager chat") : h("span", {})) : h("div", { class: "pd-dim" }, "idle")));
    root.append(h("section", { class: "pd-sec" }, h("h3", {}, "RECENT"),
      v.recent.length ? h("div", { class: "pd-list" }, ...v.recent.map((x) => h("div", { class: "pd-run " + (x.status === "review" ? "pd-ok-b" : "pd-bad-b") },
        h("span", {}, `#${x.ticket} ${x.title}`),
        h("span", { class: x.status === "review" ? "pd-ok" : "pd-warn" }, x.status === "review" ? "sent for review" : x.status, x.why ? ` — ${x.why}` : ""),
        x.pr ? h("a", { class: "pd-lnk", href: x.pr, target: "_blank", rel: "noopener" }, "PR") : h("span", { class: "pd-dim" }, ago(x.at))))) : h("div", { class: "pd-dim" }, "No tickets yet.")));
  };
  async function act(label, fn) { busy = label; err = ""; render(); try { v = await fn(); } catch (e) { err = e.message || String(e); } busy = ""; render(); }
  async function load() { try { v = await ctx.api("/state"); } catch (e) { err = e.message || String(e); } render(); }
  void load();
  const t = setInterval(() => void load(), 15_000);
  return () => clearInterval(t);
}
