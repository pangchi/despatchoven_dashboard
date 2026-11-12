"use strict";
const $ = (id) => document.getElementById(id);
const PAGE = 50;
let offset = 0, total = 0, timer = null;

async function get(path) {
  const r = await fetch(path, { credentials: "same-origin", headers: { "X-Requested-With": "p3-dashboard" } });
  if (r.status === 401) { location.href = "/login?next=/runs"; throw new Error("not logged in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const td = (text, cls) => Object.assign(document.createElement("td"), { textContent: text ?? "–", className: cls || "" });
const n1 = (v) => (v == null ? "–" : Number(v).toFixed(1));

function query() {
  const f = new FormData($("filters")), p = new URLSearchParams();
  for (const [k, v] of f.entries()) if (String(v).trim() !== "") p.set(k, String(v).trim());
  if (p.get("to")) {   // inclusive end date
    const d = new Date(p.get("to") + "T00:00:00"); d.setDate(d.getDate() + 1);
    p.set("to", d.toISOString());
  }
  if (p.get("from")) p.set("from", new Date(p.get("from") + "T00:00:00").toISOString());
  p.set("limit", PAGE); p.set("offset", offset);
  return p;
}

async function load() {
  let d;
  try { d = await get("/api/v1/runs?" + query()); }
  catch (e) { $("rows").replaceChildren(Object.assign(document.createElement("tr"), { innerHTML: `<td colspan="10" class="err"></td>` })); $("rows").querySelector("td").textContent = e.message; return; }
  total = d.total;
  $("count").textContent = `${total} run${total === 1 ? "" : "s"}`;
  $("page").textContent = total ? `${offset + 1}–${Math.min(offset + PAGE, total)} of ${total}` : "";
  $("prev").disabled = offset === 0; $("next").disabled = offset + PAGE >= total;
  if (!d.runs.length) {
    const tr = document.createElement("tr"); tr.append(td("No runs match these filters.", "muted")); tr.firstChild.colSpan = 10;
    $("rows").replaceChildren(tr); return;
  }
  $("rows").replaceChildren(...d.runs.map((r) => {
    const tr = document.createElement("tr");
    tr.tabIndex = 0; tr.className = "click";
    tr.onclick = () => { location.href = "/runs/" + r.id; };
    tr.onkeydown = (e) => { if (e.key === "Enter") tr.onclick(); };
    const s = r.stats || {};
    const started = new Date(r.started_at);
    const dur = r.duration_s ?? (Date.now() - started) / 1000;
    const res = document.createElement("td");
    res.append(Object.assign(document.createElement("span"), { className: "tag " + resultClass(r), textContent: resultText(r) }));
    const flags = [];
    if (s.hl_tripped) flags.push("HL trip");
    if (s.alarm_bits) flags.push("alarm");
    if (s.sensor_break) flags.push("TC open");
    if (r.resumed) flags.push(`gap×${r.resumed}`);
    if (r.end_approx) flags.push("end≈");
    tr.append(td(r.id, "num"), td(started.toLocaleString([], { dateStyle: "short", timeStyle: "short" })),
      td(fmtDur(dur) + (r.ended_at ? "" : "…")), td(runTitle(r)), td(r.label || "", "label"), res,
      td(n1(s.pv_max), "num"), td(n1(s.max_over_sp), "num"), td(flags.join(" · "), flags.some((f) => /HL|TC/.test(f)) ? "err" : "muted"),
      td(r.started_by || "", "muted"));
    return tr;
  }));
}

$("filters").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => { offset = 0; load(); }, 300); });
$("filters").addEventListener("reset", () => setTimeout(() => { offset = 0; load(); }, 0));
$("filters").addEventListener("submit", (e) => e.preventDefault());
$("prev").onclick = () => { offset = Math.max(0, offset - PAGE); load(); };
$("next").onclick = () => { offset += PAGE; load(); };
load();
setInterval(() => { if (offset === 0 && document.visibilityState === "visible") load(); }, 15000);
