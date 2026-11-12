// NOTEBOOK pane — the door to the book (ui/book.html, shown as a popup over Mystical) and a
// plain list of what is still open in it. Plain DOM via ctx.api(). The popup and its Alt+N
// shortcut live at module level, so once the tab has been shown they work from anywhere.
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
const COLORS = ["#c2452d", "#2f6fb5", "#2f8a57", "#b3791a", "#7b4bb0", "#c0407e", "#3c8b99"];

let ov = null;
const watchers = new Set();
export function openBook(goto) {
  if (ov) { if (goto) ov.firstChild.contentWindow?.postMessage({ nb: "goto", ...goto }, location.origin); return; }
  ov = h("div", { class: "nbk-ov", role: "dialog", "aria-label": "Notebook" });
  const frame = h("iframe", { src: "/plugin/notebook/ui/book.html", title: "Notebook", allowtransparency: "true" });
  frame.addEventListener("load", () => { frame.focus(); if (goto) setTimeout(() => frame.contentWindow?.postMessage({ nb: "goto", ...goto }, location.origin), 1500); });
  ov.append(frame); document.body.append(ov);
  requestAnimationFrame(() => ov && ov.classList.add("on"));
}
export function closeBook() {
  if (!ov) return; const el = ov; ov = null;
  el.classList.remove("on"); setTimeout(() => el.remove(), 230);
  watchers.forEach((fn) => fn());
}
if (!window.__nbkWired) {
  window.__nbkWired = true;
  window.addEventListener("message", (ev) => {
    if (ev.origin !== location.origin || !ev.data) return;
    if (ev.data.nb === "close") closeBook();
    else if (ev.data.nb === "saved") watchers.forEach((fn) => fn());
  });
  window.addEventListener("keydown", (ev) => {
    if (ev.altKey && !ev.metaKey && !ev.ctrlKey && ev.code === "KeyN") { ev.preventDefault(); ov ? closeBook() : openBook(); }
  });
}

export function mount(root, ctx) {
  root.classList.add("nbk");
  let state = null, err = "", adding = false;
  const render = () => {
    root.replaceChildren();
    const groups = new Map(); let open = 0, done = 0; const order = [];
    for (const [pi, pg] of ((state && state.pages) || []).entries()) for (const e of pg.entries || []) {
      if (!String(e.text || "").trim()) continue;
      if (e.project && !order.includes(e.project)) order.push(e.project);
      if (e.done) { done++; continue; }
      open++; const k = e.project || ""; if (!groups.has(k)) groups.set(k, []); groups.get(k).push({ e, p: pi + 1 });
    }
    root.append(h("button", { class: "nbk-open", onclick: () => openBook() },
      h("span", { class: "nbk-mini", "data-skin": (state && state.skin) || "noir" }),
      h("span", {}, h("b", {}, "Open the notebook"), h("small", {}, `${open} open · ${done} crossed out`), h("small", {}, "Alt+N from anywhere"))));
    const input = h("input", { placeholder: "Write a line… #project files it", dir: "auto", "aria-label": "Write a line in the notebook" });
    const add = async () => {
      const text = input.value.trim(); if (!text || adding) return; adding = true;
      try { await ctx.api("/line", { json: { text } }); input.value = ""; await load(); } catch (e) { err = e.message; render(); } finally { adding = false; }
    };
    input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") add(); });
    root.append(h("div", { class: "nbk-add" }, input, h("button", { onclick: add }, "Write")));
    if (err) root.append(h("div", { class: "nbk-err" }, err));
    if (!state) { root.append(h("div", { class: "nbk-dim" }, "The notebook is empty. Open it and write the first line.")); return; }
    if (!open) root.append(h("div", { class: "nbk-dim" }, "Nothing open. Everything written so far is crossed out."));
    for (const k of [...groups.keys()].sort((a, b) => (a === "") - (b === "") || a.localeCompare(b))) {
      root.append(h("div", { class: "nbk-grp" },
        h("h4", { style: k ? `--c:${COLORS[Math.max(0, order.indexOf(k)) % COLORS.length]}` : null }, k || "everything else"),
        groups.get(k).map(({ e, p }) => h("button", { class: "nbk-line", onclick: () => openBook({ p, id: e.id }) }, h("span", { dir: "auto" }, e.text), h("em", {}, `p${p}`)))));
    }
  };
  const load = async () => { try { state = (await ctx.api("/state")).state; err = ""; } catch (e) { err = e.message; } render(); };
  watchers.add(load); load();
  if (navigator.userActivation?.isActive) openBook();     // the tab was just clicked: that click opens the book
  return () => watchers.delete(load);
}
