// ORVEN pane — plain DOM, mounted by the dashboard's PluginPane with ctx.api() reaching /plugin/orven/*.
// Three blocks: this machine (enrolled? ip, allowed until, unlock now), the gate (mode, TTLs, static
// allow-list you can edit), devices (revoke). Everything reads from /state and /gate; writes go
// through the plugin, which holds the admin session.

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
const when = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "—");
const ago = (ts) => (ts ? `${Math.max(0, Math.round((Date.now() / 1000 - ts) / 60))} min ago` : "never");

export function mount(root, ctx) {
  root.classList.add("orv");
  let state = null, gate = null, err = "", busy = "";
  const render = () => {
    root.replaceChildren();
    if (err) root.append(h("div", { class: "orv-err" }, err));
    if (!state) { root.append(h("div", { class: "orv-dim" }, "Reading…")); return; }

    // --- this machine ---
    const me = h("section", { class: "orv-sec" },
      h("h3", {}, "THIS MACHINE"),
      state.enrolled
        ? h("div", { class: "orv-grid" },
            h("span", { class: "orv-k" }, "device"), h("span", {}, `${state.deviceName || ""} `, h("code", {}, state.deviceId)),
            h("span", { class: "orv-k" }, "public ip"), h("span", {}, state.ip || "?", h("span", { class: "orv-dim" }, ` · checked ${ago(state.checkedAt)}`)),
            h("span", { class: "orv-k" }, "allowed"), h("span", { class: state.allowedUntil && new Date(state.allowedUntil) > new Date() ? "orv-ok" : "orv-warn" }, `${state.allowedIp || "—"} until ${when(state.allowedUntil)}`),
            h("span", { class: "orv-k" }, "last unlock"), h("span", {}, ago(state.lastUnlockAt)),
            state.error ? h("span", { class: "orv-k" }, "error") : null, state.error ? h("span", { class: "orv-warn" }, state.error) : null)
        : h("div", { class: "orv-dim" }, "Not enrolled. Set the admin email and password on the card in SETTINGS ▸ INTEGRATIONS, or type them once below; the key never leaves this machine."),
      h("div", { class: "orv-row" },
        state.enrolled
          ? h("button", { class: "orv-btn", disabled: busy ? "" : null, onclick: () => act("unlock", () => ctx.api("/unlock", { json: {} })) }, busy === "unlock" ? "UNLOCKING…" : "UNLOCK NOW")
          : enrolForm(),
        state.enrolled ? h("button", { class: "orv-btn orv-ghost", disabled: busy ? "" : null, onclick: () => confirm("Forget this device's enrolment on this machine? (The panel still lists it; revoke it there.)") && act("forget", () => ctx.api("/forget", { json: {} })) }, "FORGET") : null));
    root.append(me);

    // --- the gate ---
    const g = h("section", { class: "orv-sec" }, h("h3", {}, "GATE"));
    if (!state.adminConfigured) g.append(h("div", { class: "orv-dim" }, "Admin email/password not set — the card in SETTINGS ▸ INTEGRATIONS. Needed to read the gate and devices."));
    else if (!gate) g.append(h("div", { class: "orv-dim" }, "Reading the gate…"));
    else if (gate.error) g.append(h("div", { class: "orv-warn" }, gate.error));
    else {
      const s = gate.settings || {};
      g.append(h("div", { class: "orv-grid" },
        h("span", { class: "orv-k" }, "mode"), h("span", {}, modeSelect(s.mode)),
        h("span", { class: "orv-k" }, "session ttl"), h("span", {}, `${s.sessionTtlSec}s`),
        h("span", { class: "orv-k" }, "unlock ip ttl"), h("span", {}, `${s.unlockIpTtlSec}s`),
        h("span", { class: "orv-k" }, "recovery"), h("span", {}, s.hasRecovery ? "configured" : h("span", { class: "orv-warn" }, "none")),
        h("span", { class: "orv-k" }, "last blocked"), h("span", { class: "orv-dim" }, s.lastBlockedInfo ? `${s.lastBlockedInfo} · ${when(s.lastBlockedAt)}` : "—")));
      g.append(h("h4", {}, "STATIC ALLOW-LIST"));
      const ips = Array.isArray(s.allowIps) ? s.allowIps : [];
      g.append(h("div", { class: "orv-ips" },
        ...ips.map((ip) => h("span", { class: "orv-chip" }, ip, h("button", { class: "orv-x", title: "remove", onclick: () => patchGate({ allowIps: ips.filter((x) => x !== ip) }) }, "✕"))),
        ipAdder(ips)));
      const devs = Array.isArray(gate.devices) ? gate.devices : [];
      g.append(h("h4", {}, `DEVICES · ${devs.length}`));
      g.append(h("div", { class: "orv-list" }, ...devs.map((d) => h("div", { class: "orv-dev" + (d.id === state.deviceId ? " orv-me" : "") + (d.revokedAt ? " orv-revoked" : "") },
        h("span", {}, h("button", { class: "orv-name", title: "rename", onclick: () => { const n = prompt(`Name for this device`, d.name); if (n && n.trim() && n.trim() !== d.name) act("rename", () => ctx.api("/devices/rename", { json: { id: d.id, name: n.trim() } }).then(loadGate)); } }, d.name), d.id === state.deviceId ? h("span", { class: "orv-dim" }, " · this machine") : null),
        h("span", { class: "orv-dim" }, d.platform || "", d.lastSeenAt ? ` · seen ${when(d.lastSeenAt)}` : ""),
        d.revokedAt ? h("span", { class: "orv-dim" }, "revoked") : h("button", { class: "orv-x", title: "revoke", onclick: () => confirm(`Revoke ${d.name}?`) && act("revoke", () => ctx.api("/devices/revoke", { json: { id: d.id } }).then(loadGate)) }, "revoke")))));
    }
    root.append(g);
  };

  const enrolForm = () => {
    const email = h("input", { class: "orv-in", placeholder: "admin email", autocomplete: "off" });
    const pass = h("input", { class: "orv-in", placeholder: "password (sent once, not stored)", type: "password", autocomplete: "off" });
    const name = h("input", { class: "orv-in", placeholder: "device name", value: "Mystical on this Mac" });
    return h("form", { class: "orv-form", onsubmit: (e) => { e.preventDefault(); act("enroll", () => ctx.api("/enroll", { json: { email: email.value, password: pass.value, name: name.value } })); } },
      email, pass, name, h("button", { class: "orv-btn" }, busy === "enroll" ? "ENROLLING…" : "ENROL THIS MACHINE"));
  };
  const modeSelect = (mode) => {
    const sel = h("select", { class: "orv-in", onchange: (e) => patchGate({ mode: e.target.value }) },
      ...["off", "monitor", "enforce"].map((m) => h("option", { value: m, selected: m === mode ? "" : null }, m)));
    return sel;
  };
  const ipAdder = (ips) => {
    const inp = h("input", { class: "orv-in orv-ipin", placeholder: "add ip" });
    return h("form", { class: "orv-inline", onsubmit: (e) => { e.preventDefault(); const v = inp.value.trim(); if (/^\d{1,3}(\.\d{1,3}){3}$/.test(v)) patchGate({ allowIps: [...ips, v] }); } }, inp, h("button", { class: "orv-btn orv-sm" }, "+"));
  };

  async function act(label, fn) {
    busy = label; err = ""; render();
    try { const out = await fn(); if (out && out.ip !== undefined) state = { ...state, ...out }; }
    catch (e) { err = e.message || String(e); }
    busy = ""; await load();
  }
  async function patchGate(patch) {
    err = "";
    try { const out = await ctx.api("/gate/settings", { json: patch }); gate = { ...gate, settings: out.settings }; }
    catch (e) { err = e.message || String(e); }
    render();
  }
  async function loadGate() {
    if (!state || !state.adminConfigured) return;
    try { gate = await ctx.api("/gate"); } catch (e) { gate = { error: e.message || String(e) }; }
    render();
  }
  async function load() {
    try { state = await ctx.api("/state"); } catch (e) { err = e.message || String(e); }
    render();
    void loadGate();
  }
  void load();
  const timer = setInterval(() => { void load(); }, 60_000);
  return () => clearInterval(timer);
}
