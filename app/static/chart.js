"use strict";
/* Shared trend chart (no library). rows: [{t, pv, sp, hl, pw, seg?, held?}], t in ms.
   opts.segments: shade alternating profile segments and label them. */
function drawChart(cv, rows, t0, t1, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = opts.height || 300;
  cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px";
  const c = cv.getContext("2d"); c.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement), col = (n) => css.getPropertyValue(n).trim();
  const L = 48, R = 40, T = opts.segments ? 22 : 10, B = 24, pw = W - L - R, ph = H - T - B;
  c.clearRect(0, 0, W, H);
  c.font = "11px system-ui"; c.fillStyle = col("--muted"); c.strokeStyle = col("--line"); c.lineWidth = 1;
  if (t1 <= t0) t1 = t0 + 1000;

  let lo = Infinity, hi = -Infinity;
  rows.forEach((r) => [r.pv, r.sp, r.hl].forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
  if (!isFinite(lo)) { c.fillText(opts.empty || "No data in this range yet", L + 10, T + 20); return; }
  const pad = Math.max(2, (hi - lo) * 0.08); lo -= pad; hi += pad;
  const x = (t) => L + (t - t0) / (t1 - t0) * pw, y = (v) => T + (1 - (v - lo) / (hi - lo)) * ph;
  const yp = (v) => T + (1 - v / 100) * ph;

  if (opts.segments) {                        // segment bands + numbers, hold periods hatched
    let i = 0;
    while (i < rows.length) {
      let j = i; while (j + 1 < rows.length && rows[j + 1].seg === rows[i].seg) j++;
      const xa = x(rows[i].t), xb = x(j + 1 < rows.length ? rows[j + 1].t : rows[j].t);
      if (rows[i].seg != null && rows[i].seg > 0) {
        if (rows[i].seg % 2 === 0) { c.fillStyle = col("--line"); c.globalAlpha = 0.45; c.fillRect(xa, T, xb - xa, ph); c.globalAlpha = 1; }
        c.fillStyle = col("--muted"); c.textAlign = "left";
        if (xb - xa > 18) c.fillText("S" + rows[i].seg, xa + 3, T - 8);
      }
      i = j + 1;
    }
    c.fillStyle = col("--warn");
    rows.forEach((r, k) => { if (r.held && k + 1 < rows.length) { c.globalAlpha = 0.25; c.fillRect(x(r.t), T, x(rows[k + 1].t) - x(r.t), ph); c.globalAlpha = 1; } });
    c.fillStyle = col("--muted");
  }

  for (let i = 0; i <= 5; i++) {
    const v = lo + (hi - lo) * i / 5, yy = y(v);
    c.beginPath(); c.moveTo(L, yy); c.lineTo(L + pw, yy); c.stroke();
    c.textAlign = "right"; c.fillText(v.toFixed(0), L - 6, yy + 4);
    c.textAlign = "left"; c.fillText((i * 20) + "%", L + pw + 6, yp(i * 20) + 4);
  }
  c.textAlign = "center";
  const span = t1 - t0;
  for (let i = 0; i <= 6; i++) {
    const t = t0 + span * i / 6, d = new Date(t);
    let s;
    if (opts.elapsed) {                       // run pages: time since start
      const m = Math.round((t - t0) / 60000);
      s = span < 3 * 60000 ? `${Math.round((t - t0) / 1000)}s` : (m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}` : `${m}m`);
    } else {
      s = span > 864e5 ? `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}h`
        : `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
    }
    c.fillText(s, x(t), H - 6);
  }
  // gaps longer than 3.5x the median spacing are drawn as breaks (comm loss, monitor offline)
  const gaps = rows.slice(1).map((r, i) => r.t - rows[i].t).sort((a, b) => a - b);
  const brk = Math.max(15000, (gaps[gaps.length >> 1] || 0) * 3.5);
  const line = (key, color, w, yf) => {
    c.strokeStyle = color; c.lineWidth = w; c.beginPath();
    let prev = null;
    rows.forEach((r) => {
      const v = r[key]; if (v == null) { prev = null; return; }
      if (prev === null || r.t - prev > brk) c.moveTo(x(r.t), yf(v)); else c.lineTo(x(r.t), yf(v));
      prev = r.t;
    });
    c.stroke();
  };
  line("pw", col("--pw"), 1, yp);
  line("hl", col("--hl"), 1.2, y);
  line("sp", col("--sp"), 1.5, y);
  line("pv", col("--pv"), 2, y);
}

const fmtDur = (s) => {
  if (s == null) return "–";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}h ${String(m).padStart(2, "0")}m` : m ? `${m}m ${String(sec).padStart(2, "0")}s` : `${sec}s`;
};
const runTitle = (r) => r.mode === "profile" ? `Profile ${r.profile}` : r.mode === "timer" ? "Timer" : "Manual";
const resultClass = (r) => !r.ended_at ? "warn" : (r.end_reason === "completed" ? "ok" : (/abort|error|interrupt/.test(r.end_reason || "") ? "bad" : ""));
const resultText = (r) => !r.ended_at ? "running" : (r.end_reason || "ended");

/* ---------- events: plain-language text, shared by dashboard, run page and event log ---------- */
const SEV_CLASS = { info: "", warning: "warn", alarm: "bad" };
const CAT_LABEL = { control: "Control", process: "Process", run: "Run", settings: "Settings", access: "Access", system: "System" };
function describeEvent(e) {
  const d = e.detail || {}, n = (v) => (v == null ? "?" : Number(v).toFixed(1));
  const fail = !e.ok && d.error ? ` – failed: ${d.error}` : (!e.ok && e.category === "control" ? " – failed" : "");
  const T = {
    alarm_on: () => `Alarm ${d.alarm} active (PV ${n(d.pv)})${d.at_startup ? " – already active when monitoring started" : ""}`,
    alarm_off: () => `Alarm ${d.alarm} cleared (PV ${n(d.pv)})`,
    high_limit_tripped: () => `HIGH LIMIT TRIPPED – high-limit PV ${n(d.hl_pv)}, limit ${n(d.limit)}${d.at_startup ? " (already tripped at monitor start)" : ""}`,
    high_limit_reset: () => `High limit reset (high-limit PV ${n(d.hl_pv)})`,
    sensor_break: () => "Control thermocouple open (sensor break)",
    sensor_ok: () => `Control thermocouple OK again (PV ${n(d.pv)})`,
    hl_sensor_open: () => "High-limit thermocouple open",
    hl_sensor_ok: () => "High-limit thermocouple OK again",
    mode_changed: () => `Mode changed at controller: ${d.from} → ${d.to}`,
    setpoint_changed: () => `Setpoint changed at controller: ${n(d.from)} → ${n(d.to)}`,
    profile_held: () => `Profile ${d.profile} held at controller (segment ${d.segment}, PV ${n(d.pv)})`,
    profile_resumed: () => `Profile ${d.profile} resumed at controller (segment ${d.segment})`,
    segment_changed: () => `Profile ${d.profile}: segment ${d.segment} ${d.type} (SP ${n(d.sp)}, PV ${n(d.pv)})`,
    run_started: () => `Run #${d.run} started – ${d.mode === "profile" ? "profile " + d.profile : d.mode}${d.label ? " · " + d.label : ""} (by ${d.by || "?"})`,
    run_ended: () => `Run #${d.run} ended – ${d.reason}`,
    run_edited: () => `Run #${d.run} edited${d.label != null ? ` – label “${d.label}”` : ""}${d.notes_changed ? ", notes" : ""}`,
    run_next_label: () => d.label ? `Next run label set: ${d.label}` : "Next run label cleared",
    set_setpoint: () => `Setpoint → ${d.value}${fail}`,
    set_mode: () => `Mode → ${d.mode}${fail}`,
    manual: () => `Manual ${d.action}${d.label ? " · " + d.label : ""}${fail}`,
    timer: () => `Timer ${d.action}${d.seconds ? " (" + d.seconds + " s)" : ""}${d.label ? " · " + d.label : ""}${fail}`,
    profile: () => `Profile ${d.action}${d.profile != null ? " #" + d.profile : ""}${d.label ? " · " + d.label : ""}${fail}`,
    relay: () => `Relay ${d.relay} ${d.on ? "on" : "off"}${fail}`,
    set_high_limit: () => `High limit → ${d.value}${fail}`,
    settings_saved: () => `Settings changed: ${Object.keys(d).join(", ")}`,
    settings_test_serial: () => `Connection test ${d.port} @ ${d.baud}: ${d.ok ? "OK" : "no answer"}`,
    settings_detect_serial: () => d.found ? `Auto-detect: found ${d.SERIAL_BAUD} baud, parity ${d.SERIAL_PARITY}` : `Auto-detect: no answer (${d.tried} tried)`,
    login: () => `Signed in from ${d.ip}`,
    logout: () => `Signed out from ${d.ip}`,
    login_failed: () => `Failed sign-in as “${d.username}” from ${d.ip}` + (d.attempts_left != null ? ` – ${d.attempts_left} attempt(s) left` : ""),
    login_locked: () => d.scope
      ? `SIGN-IN LOCKED for ${d.minutes} min – ${d.scope === "user" ? "account " + d.subject : "address " + d.subject} (lock #${d.level})`
      : `Sign-in blocked (too many failures) from ${d.ip}`,
    login_unlocked: () => d.all || (d.args && d.args[0] === "all") ? `All sign-in locks cleared (${d.cleared})` : `Sign-in lock cleared: ${d.scope || (d.args || []).join(" ")} ${d.subject || ""}`,
    api_key_rejected: () => `Rejected API key from ${d.ip} (${d.path})` + (d.attempts_left != null ? ` – ${d.attempts_left} attempt(s) left` : ""),
    password_changed: () => `Dashboard password changed from ${d.ip} – other browsers signed out`,
    password_change_failed: () => `Password change refused from ${d.ip}: ${d.reason}` + (d.attempts_left != null ? ` – ${d.attempts_left} attempt(s) left` : ""),
    local_access: () => `Dashboard used without password from ${d.ip} (trusted network, ${d.level === "view" ? "view only" : "full control"})`,
    api_locked: () => `API KEYS LOCKED for ${d.minutes} min from ${d.ip} (lock #${d.level})`,
    startup: () => `Monitor started – v${d.version}, port ${d.port}`,
    comm_lost: () => `Controller communication lost${d.error ? ": " + d.error : ""}`,
    comm_restored: () => "Controller communication restored",
    db_connected: () => "Database connected",
    purge: () => `Purged ${(d.dropped || []).length} day(s) of old telemetry (disk ${n(d.disk_pct_after)} %)`,
    purge_check: () => `Purge check run – ${d.dropped} day(s) dropped`,
  };
  try { if (T[e.action]) return T[e.action](); } catch (err) { /* fall through */ }
  return e.action + (e.detail ? " " + JSON.stringify(e.detail) : "");
}
function eventRow(e, opts = {}) {
  /* compact one-line event for side panels */
  const div = document.createElement("div");
  div.className = "ev " + (SEV_CLASS[e.severity] || "") + (e.ok ? "" : " failed");
  const t = new Date(e.ts);
  const time = opts.timeOnly ? t.toLocaleTimeString() : t.toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  const dot = Object.assign(document.createElement("span"), { className: "sevdot " + (e.severity || "info"), title: e.severity || "" });
  const txt = document.createElement("span");
  txt.textContent = `${time} · ${describeEvent(e)}`;
  const src = Object.assign(document.createElement("span"), { className: "src", textContent: e.source });
  div.append(dot, txt, src);
  return div;
}
