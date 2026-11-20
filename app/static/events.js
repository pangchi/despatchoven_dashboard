"use strict";
const $ = (id) => document.getElementById(id);
const PAGE = 100;
let offset = 0, severity = "", cats = new Set(), timer = null, newestId = null, open = new Set();

async function get(path) {
  const r = await fetch(path, { credentials: "same-origin", headers: { "X-Requested-With": "p3-dashboard" } });
  if (r.status === 401) { location.href = "/login?next=/events"; throw new Error("not logged in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function params(withPaging = true) {
  const f = new FormData($("filters")), p = new URLSearchParams();
  for (const k of ["q", "source"]) { const v = String(f.get(k) || "").trim(); if (v) p.set(k, v); }
  for (const k of ["from", "to"]) { const v = f.get(k); if (v) p.set(k, new Date(v).toISOString()); }
  if (f.get("failed")) p.set("failed", "1");
  if (cats.size) p.set("category", [...cats].join(","));
  if (severity) p.set("severity", severity);
  if (withPaging) { p.set("limit", PAGE); p.set("offset", offset); }
  return p;
}

// remember filters in the URL so a filtered view can be bookmarked / shared
function syncUrl() {
  const p = params(false);
  history.replaceState(null, "", "/events" + (p.toString() ? "?" + p : ""));
  $("csv").href = "/api/v1/events.csv" + (p.toString() ? "?" + p : "");
}
function restoreFromUrl() {
  const p = new URLSearchParams(location.search), f = $("filters");
  for (const k of ["q", "source"]) if (p.get(k)) f.elements[k].value = p.get(k);
  const local = (iso) => { const d = new Date(iso); d.setMinutes(d.getMinutes() - d.getTimezoneOffset()); return d.toISOString().slice(0, 16); };
  for (const k of ["from", "to"]) if (p.get(k)) f.elements[k].value = local(p.get(k));
  if (p.get("failed")) f.elements.failed.checked = true;
  (p.get("category") || "").split(",").filter(Boolean).forEach((c) => cats.add(c));
  severity = p.get("severity") || "";
  paintChips();
}
function paintChips() {
  document.querySelectorAll("#cats [data-cat]").forEach((b) => b.classList.toggle("on", cats.has(b.dataset.cat)));
  document.querySelectorAll("#sevbar [data-sev]").forEach((b) => b.classList.toggle("on", b.dataset.sev === severity));
}

function detailText(e) {
  return e.detail ? JSON.stringify(e.detail, null, 2) : "(no detail)";
}

async function load() {
  syncUrl();
  let d;
  try { d = await get("/api/v1/events?" + params()); }
  catch (e) { $("rows").replaceChildren(row1(e.message, "err")); return; }
  const c = d.counts || {};
  $("c_all").textContent = (c.info || 0) + (c.warning || 0) + (c.alarm || 0);
  ["alarm", "warning", "info"].forEach((s) => { $("c_" + s).textContent = c[s] || 0; });
  $("count").textContent = `${d.total} event${d.total === 1 ? "" : "s"}`;
  $("page").textContent = d.total ? `${offset + 1}–${Math.min(offset + PAGE, d.total)} of ${d.total}` : "";
  $("prev").disabled = offset === 0; $("next").disabled = offset + PAGE >= d.total;
  if (!d.events.length) { $("rows").replaceChildren(row1("No events match these filters.", "muted")); return; }

  const prevNewest = newestId;
  newestId = d.events[0].id;
  $("rows").replaceChildren(...d.events.flatMap((e) => {
    const tr = document.createElement("tr");
    tr.className = "click sev-" + e.severity + (e.ok ? "" : " failed") + (prevNewest && e.id > prevNewest && offset === 0 ? " fresh" : "");
    tr.tabIndex = 0;
    const t = new Date(e.ts);
    const cell = (text, cls) => Object.assign(document.createElement("td"), { textContent: text, className: cls || "" });
    const sev = document.createElement("td");
    sev.append(Object.assign(document.createElement("span"), { className: "sevdot " + e.severity }), " " + e.severity);
    const ev = document.createElement("td");
    ev.className = "evtext";
    const runId = e.detail && e.detail.run;
    if (runId && String(e.action).startsWith("run_")) {
      ev.append(Object.assign(document.createElement("a"), { href: "/runs/" + runId, textContent: describeEvent(e) }));
    } else ev.textContent = describeEvent(e);
    tr.append(cell(t.toLocaleString([], { dateStyle: "short", timeStyle: "medium" }), "nowrap"), sev,
      cell(CAT_LABEL[e.category] || e.category || "", "muted"), ev, cell(e.source, "muted nowrap"));
    const det = document.createElement("tr");
    det.className = "detail"; det.hidden = !open.has(e.id);
    const td = document.createElement("td"); td.colSpan = 5;
    td.append(Object.assign(document.createElement("pre"), { textContent: `${e.action}  #${e.id}\n${detailText(e)}` }));
    det.append(td);
    const toggle = (evt) => {
      if (evt.target.closest("a")) return;
      det.hidden = !det.hidden;
      det.hidden ? open.delete(e.id) : open.add(e.id);
    };
    tr.onclick = toggle;
    tr.onkeydown = (k) => { if (k.key === "Enter" || k.key === " ") { k.preventDefault(); toggle(k); } };
    return [tr, det];
  }));
}
function row1(text, cls) {
  const tr = document.createElement("tr"), td = document.createElement("td");
  td.colSpan = 5; td.className = cls; td.textContent = text; tr.append(td); return tr;
}

const reload = () => { offset = 0; newestId = null; load(); };
$("filters").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(reload, 300); });
$("filters").addEventListener("reset", () => setTimeout(() => { cats.clear(); severity = ""; paintChips(); reload(); }, 0));
$("filters").addEventListener("submit", (e) => e.preventDefault());
document.querySelectorAll("#cats [data-cat]").forEach((b) => b.onclick = () => {
  cats.has(b.dataset.cat) ? cats.delete(b.dataset.cat) : cats.add(b.dataset.cat);
  paintChips(); reload();
});
document.querySelectorAll("#sevbar [data-sev]").forEach((b) => b.onclick = () => { severity = b.dataset.sev; paintChips(); reload(); });
$("prev").onclick = () => { offset = Math.max(0, offset - PAGE); load(); };
$("next").onclick = () => { offset += PAGE; load(); };

restoreFromUrl();
load();
setInterval(() => {
  if ($("live").checked && offset === 0 && document.visibilityState === "visible" && !$("filters").elements.to.value) load();
}, 5000);
