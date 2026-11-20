"use strict";
const $ = (id) => document.getElementById(id);
const RUN = Number(document.body.dataset.run);
let run = null, dirty = false;

async function call(path, method = "GET", body) {
  const opt = { method, credentials: "same-origin", headers: { "X-Requested-With": "p3-dashboard" } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login?next=/runs/" + RUN; throw new Error("not logged in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const n1 = (v, u = "") => (v == null ? "–" : Number(v).toFixed(1) + u);
const tile = (lbl, val, sub) => {
  const d = document.createElement("div"); d.className = "tile";
  d.innerHTML = '<div class="lbl"></div><div class="val sm"></div>' + (sub ? '<div class="sub"></div>' : "");
  d.querySelector(".lbl").textContent = lbl; d.querySelector(".val").textContent = val;
  if (sub) d.querySelector(".sub").textContent = sub;
  return d;
};
const flag = (text, cls) => Object.assign(document.createElement("span"), { className: "tag " + cls, textContent: text });

async function load() {
  let d;
  try { d = await call(`/api/v1/runs/${RUN}/telemetry?max_points=1500`); }
  catch (e) { $("title").textContent = `Run #${RUN}`; $("subtitle").textContent = e.message; return; }
  run = d.run;
  const s = run.stats || {};
  const started = new Date(run.started_at);
  const ended = run.ended_at ? new Date(run.ended_at) : null;
  const dur = ended ? (ended - started) / 1000 : (Date.now() - started) / 1000;

  $("title").textContent = `Run #${run.id} · ${runTitle(run)}` + (run.label ? ` · ${run.label}` : "");
  $("subtitle").textContent = `${started.toLocaleString()}` + (ended ? ` → ${ended.toLocaleString()}` : " → running") +
    ` · started by ${run.started_by || "?"}`;
  document.title = `Run ${run.id}` + (run.label ? ` · ${run.label}` : "");

  const flags = [flag(resultText(run), resultClass(run))];
  if (s.hl_tripped) flags.push(flag("High limit tripped during run", "bad"));
  if (s.sensor_break) flags.push(flag("Thermocouple open during run", "bad"));
  for (let i = 0; i < 5; i++) if ((s.alarm_bits || 0) & (1 << i)) flags.push(flag(`Alarm ${i + 1} occurred`, "warn"));
  if (run.end_approx) flags.push(flag("End time approximate (monitor was offline)", "warn"));
  if (run.resumed) flags.push(flag(`Monitoring gap ×${run.resumed} (monitor restarted)`, "warn"));
  $("flags").replaceChildren(...flags);

  const u = " °";
  $("tiles").replaceChildren(
    tile("Duration", fmtDur(dur) + (ended ? "" : " …")),
    tile("Max PV", n1(s.pv_max, u), `min ${n1(s.pv_min)} · avg ${n1(s.pv_avg)}`),
    tile("Max SP", n1(s.sp_max, u)),
    tile("Max over SP", n1(s.max_over_sp, u), "PV − SP while running"),
    tile("High-limit PV max", n1(s.hl_pv_max, u)),
    tile("Avg power", n1(s.power_avg, " %")),
    ...(run.mode === "profile" ? [tile("Held", fmtDur(s.held_s), `last segment S${s.last_segment ?? "–"}`)] : []),
    tile("Samples", s.samples ?? 0, d.count < (s.samples || 0) ? `chart shows ${d.count} averaged points` : ""));

  const rows = d.rows.map((r) => ({ t: Date.parse(r.ts), pv: r.pv, sp: r.sp, hl: r.hl_pv, pw: r.power,
    seg: run.mode === "profile" ? r.active_segment : null, held: r.profile_status === 2 }));
  const t1 = ended ? +ended : Date.now();
  drawChart($("chart"), rows, +started, Math.max(t1, rows.length ? rows[rows.length - 1].t : t1),
    { segments: run.mode === "profile", elapsed: true, height: 320,
      empty: run.ended_at ? "No samples left for this run - telemetry was purged (summary kept)." : "Waiting for samples…" });
  $("chartnote").textContent = `x axis = time since start`;

  if (!dirty) { $("label").value = run.label || ""; $("notes").value = run.notes || ""; }
  loadEvents(started, ended);
  if (!ended) setTimeout(load, 5000);   // live while running
}

async function loadEvents(start, end) {
  try {
    const p = new URLSearchParams({ limit: 200, from: new Date(+start - 60000).toISOString() });
    if (end) p.set("to", new Date(+end + 60000).toISOString());
    const { events } = await call("/api/v1/events?" + p);
    $("events").replaceChildren(...(events.length ? events.reverse().map((e) => eventRow(e, { timeOnly: true })) : [Object.assign(document.createElement("span"), { className: "muted", textContent: "No events." })]));
  } catch (e) { $("events").textContent = e.message; }
}

["label", "notes"].forEach((id) => $(id).addEventListener("input", () => { dirty = true; }));
$("editform").onsubmit = async (e) => {
  e.preventDefault();
  const m = $("savemsg");
  try {
    await call(`/api/v1/runs/${RUN}`, "PATCH", { label: $("label").value, notes: $("notes").value });
    dirty = false; m.className = "msg ok"; m.textContent = "Saved";
    load();
  } catch (err) { m.className = "msg err"; m.textContent = err.message; }
};
window.addEventListener("resize", () => run && load());
load();
