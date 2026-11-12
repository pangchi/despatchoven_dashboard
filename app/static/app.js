"use strict";
const $ = (id) => document.getElementById(id);
let units = "", rangeMin = 60, last = null;

async function api(path, body) {
  const opt = { headers: { "X-Requested-With": "p3-dashboard" }, credentials: "same-origin" };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch("/api/v1" + path, opt);
  if (r.status === 401) { location.href = "/login?next=/"; throw new Error("unauthorized"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toFixed(d);
const dur = (s) => { s = Math.max(0, s | 0); return `${String(s / 3600 | 0).padStart(2, "0")}:${String((s % 3600) / 60 | 0).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
function tag(el, text, cls) { el.textContent = text; el.className = "tag " + (cls || ""); }

// ---------------- live status ----------------
async function refreshStatus() {
  let st;
  try { st = await api("/status"); } catch (e) { tag($("conn"), "server unreachable", "bad"); return; }
  units = (st.info && st.info.units_label) || "";
  tag($("conn"), st.comm_ok ? "controller online" : "controller offline", st.comm_ok ? "ok" : "bad");
  if (!st.comm_ok && st.comm_error) $("conn").title = st.comm_error;
  tag($("dbtag"), st.db_ok ? "DB ok" : "DB error", st.db_ok ? "ok" : "bad");
  if (st.db_error) $("dbtag").title = st.db_error;
  const d = st.data; if (!d) return;
  last = d;
  $("pv").textContent = d.sensor_break ? "OPEN" : fmt(d.pv) + " " + units;
  $("sp").textContent = fmt(d.sp) + " " + units;
  $("hlpv").textContent = fmt(d.hl_pv) + " " + units;
  $("hlsp").textContent = "limit " + fmt(d.hl_alarm_value) + " " + units;
  $("power").textContent = fmt(d.power, 0) + " %";
  $("powerbar").style.width = Math.min(100, Math.max(0, d.power || 0)) + "%";
  $("mode").textContent = d.mode_label;
  let sub = "";
  if (d.current_mode === 2) sub = d.manual_status ? "running" : "stopped";
  else if (d.current_mode === 3) sub = (d.timer_status ? "running · " : "stopped · ") + dur(d.timer_remaining);
  else if (d.current_mode === 4) sub = `#${d.selected_profile} ${d.profile_status_label} · seg ${d.active_segment} ${d.segment_type_label} · ${dur(d.profile_remaining)} left`;
  $("modesub").textContent = sub;
  $("state").textContent = d.state;
  $("logint").textContent = "every " + st.log_interval + " s · " + st.samples_logged + " rows this session";
  if (st.info && st.info.sp_max !== undefined) $("splim").textContent = `range ${fmt(st.info.sp_min)}–${fmt(st.info.sp_max)}`;
  if (document.activeElement !== $("spin") && $("spin").value === "") $("spin").placeholder = fmt(d.manual_sp);

  document.querySelectorAll("[data-mode]").forEach((b) =>
    b.classList.toggle("on", { manual: 2, timer: 3, profile: 4, select: 0 }[b.dataset.mode] === d.current_mode));

  const flags = [];
  if (d.hl_tripped) flags.push(["HIGH LIMIT TRIPPED – reset at controller", "bad"]);
  if (d.sensor_break) flags.push(["Control thermocouple open", "bad"]);
  if (d.hl_status & 1) flags.push(["High-limit thermocouple open", "bad"]);
  for (let i = 0; i < 5; i++) if (d.alarm_bits & (1 << i)) flags.push([`Alarm ${i + 1} active`, "warn"]);
  if (d.profile_status === 2) flags.push(["Profile held", "warn"]);
  $("flags").replaceChildren(...flags.map(([t, c]) => { const s = document.createElement("span"); tag(s, t, c); return s; }));

  renderRun(st.run);

  const rel = $("relays"); rel.replaceChildren();
  for (let i = 1; i <= 4; i++) {
    const on = !!(d.relay_bits & (1 << (i - 1)));
    const b = document.createElement("button");
    b.className = "btn small" + (on ? " on" : ""); b.textContent = `R${i} ${on ? "ON" : "off"}`;
    b.onclick = () => act(`/control/relay`, { relay: i, on: !on }, `Relay ${i} → ${on ? "off" : "on"}`);
    rel.appendChild(b);
  }
}

// ---------------- current run ----------------
let runState = null;
function renderRun(r) {
  runState = r || {};
  const line = $("runline"), input = $("runlabel"), link = $("runlink");
  const editing = document.activeElement === input;
  if (runState.id) {
    const started = new Date(runState.started_at);
    const elapsed = (Date.now() - started) / 1000;
    line.replaceChildren(
      Object.assign(document.createElement("span"), { className: "tag warn", textContent: "● running" }),
      ` #${runState.id} · ${runTitle(runState)} · started ${started.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} · ${fmtDur(elapsed)}`);
    $("runlabel-lbl").textContent = "Label (this run)";
    if (!editing) input.value = runState.label || "";
    link.textContent = "View run →"; link.href = "/runs/" + runState.id;
  } else {
    line.textContent = "No run in progress";
    $("runlabel-lbl").textContent = "Label for next run";
    if (!editing) input.value = runState.next_label || "";
    link.textContent = "All runs →"; link.href = "/runs";
  }
}
$("labelform").onsubmit = async (e) => {
  e.preventDefault();
  const label = $("runlabel").value.trim();
  try {
    if (runState && runState.id) {
      await fetch(`/api/v1/runs/${runState.id}`, { method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Requested-With": "p3-dashboard" }, body: JSON.stringify({ label }) })
        .then((r) => r.ok ? r : r.json().then((j) => { throw new Error(j.error); }));
    } else {
      await api("/runs/next-label", { label });
    }
    $("runlabel").blur();
    $("msg").className = "msg ok"; $("msg").textContent = label ? `Label “${label}” set` : "Label cleared";
    refreshStatus();
  } catch (err) { $("msg").className = "msg err"; $("msg").textContent = "Label: " + err.message; }
};

// ---------------- chart ----------------
async function refreshChart() {
  let res;
  try { res = await api(`/telemetry?minutes=${rangeMin}&max_points=1200`); } catch (e) { return; }
  draw(res.rows.map((r) => ({ t: Date.parse(r.ts), pv: r.pv, sp: r.sp, hl: r.hl_pv, pw: r.power })),
    Date.parse(res.start), Date.parse(res.end));
}

function draw(rows, t0, t1) { drawChart($("chart"), rows, t0, t1); }

// ---------------- system / events ----------------
async function refreshSystem() {
  try {
    const s = await api("/system");
    const mb = (b) => (b / 1048576).toFixed(1) + " MB";
    const kv = [
      ["Disk used", `${s.disk.pct}% of ${(s.disk.total / 1e9).toFixed(1)} GB (purge at ${s.purge_threshold_pct}%)`],
      ["Database", mb(s.db.db_bytes) + ` · ${s.db.partitions} day partitions`],
      ["Data from", s.db.oldest ? new Date(s.db.oldest).toLocaleString() : "–"],
      ["Log rate", `${s.log_interval_active} s active / ${s.log_interval_idle} s idle`],
      ["Retention", s.retention_days ? s.retention_days + " days" : "until disk limit"],
      ["Last purge", s.last_purge ? `${new Date(s.last_purge.ts).toLocaleTimeString()} · dropped ${s.last_purge.dropped.length}` : "–"],
    ];
    $("sys").replaceChildren(...kv.flatMap(([k, v]) => {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k; dd.textContent = v; return [dt, dd];
    }));
    const ev = await api("/events?limit=40");
    $("events").replaceChildren(...ev.events.map((e) => {
      const d = document.createElement("div");
      if (!e.ok) d.className = "bad";
      const det = e.detail ? " " + JSON.stringify(e.detail) : "";
      d.textContent = `${new Date(e.ts).toLocaleString()} · ${e.source} · ${e.action}${det}`;
      return d;
    }));
  } catch (e) { /* shown via status tags */ }
}

// ---------------- controls ----------------
async function act(path, body, label) {
  const m = $("msg");
  m.className = "msg"; m.textContent = label + "…";
  try {
    await api(path, body);
    m.className = "msg ok"; m.textContent = label + " ✓";
    setTimeout(refreshStatus, 300); setTimeout(refreshSystem, 600);
  } catch (e) { m.className = "msg err"; m.textContent = label + " failed: " + e.message; }
}

document.querySelectorAll("[data-mode]").forEach((b) => b.onclick = () => act("/control/mode", { mode: b.dataset.mode }, "Mode " + b.dataset.mode));
document.querySelectorAll("[data-manual]").forEach((b) => b.onclick = () => {
  if (b.dataset.manual === "start" && !confirm("Start the oven in manual mode?")) return;
  act("/control/manual", { action: b.dataset.manual }, "Manual " + b.dataset.manual);
});
document.querySelectorAll("[data-timer]").forEach((b) => b.onclick = () => {
  const body = { action: b.dataset.timer }, s = parseInt($("tsec").value, 10);
  if (b.dataset.timer === "start") { if (s > 0) body.seconds = s; if (!confirm("Start timer?")) return; }
  act("/control/timer", body, "Timer " + b.dataset.timer);
});
document.querySelectorAll("[data-profile]").forEach((b) => b.onclick = () => {
  const body = { action: b.dataset.profile }, p = $("pnum").value;
  if (b.dataset.profile === "run") { if (p !== "") body.profile = parseInt(p, 10); if (!confirm("Run profile " + (p || "(selected)") + "?")) return; }
  if (b.dataset.profile === "abort" && !confirm("Abort the running profile?")) return;
  act("/control/profile", body, "Profile " + b.dataset.profile);
});
$("spset").onclick = () => {
  const v = parseFloat($("spin").value);
  if (Number.isNaN(v)) return;
  act("/control/setpoint", { value: v }, `Setpoint ${v}`).then(() => { $("spin").value = ""; });
};
if ($("hlset")) $("hlset").onclick = () => {
  const v = parseFloat($("hlin").value);
  if (Number.isNaN(v) || !confirm(`Change HIGH LIMIT to ${v}? This is a safety setting.`)) return;
  act("/control/high-limit", { value: v }, `High limit ${v}`);
};
document.querySelectorAll("#range button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("#range button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on"); rangeMin = parseInt(b.dataset.m, 10); refreshChart();
});
window.addEventListener("resize", refreshChart);

refreshStatus(); refreshChart(); refreshSystem();
setInterval(refreshStatus, 2000);
setInterval(refreshChart, 15000);
setInterval(refreshSystem, 30000);
