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
