"use strict";
const $ = (id) => document.getElementById(id);
let schema = null;         // last settings from the server
const original = {};       // key -> string value as loaded
const inputs = {};         // key -> element holding the value

async function api(path, body) {
  const opt = { headers: { "X-Requested-With": "p3-dashboard" }, credentials: "same-origin" };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  if (r.status === 401) { location.href = "/login?next=/settings"; throw new Error("not logged in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const el = (tag, props = {}, ...kids) => {
  const e = Object.assign(document.createElement(tag), props);
  kids.flat().forEach((k) => k != null && e.append(k));
  return e;
};
const str = (v) => (v === null || v === undefined) ? "" : (typeof v === "boolean" ? String(v) : String(v));

// ---------------------------------------------------------------- render
function fieldInput(f) {
  let input;
  if (f.type === "choice") {
    input = el("select", { id: "f_" + f.key });
    f.options.forEach((o) => input.append(el("option", { value: String(o), textContent: String(o) })));
    input.value = str(f.value);
  } else if (f.type === "bool") {
    input = el("input", { type: "checkbox", id: "f_" + f.key, checked: !!f.value });
  } else if (f.type === "secret") {
    input = el("input", { type: "password", id: "f_" + f.key, autocomplete: "new-password",
      placeholder: f.is_set ? "•••••••• (unchanged)" : "not set" });
  } else if (["int", "float", "optfloat"].includes(f.type)) {
    input = el("input", { type: "number", id: "f_" + f.key, value: str(f.value),
      step: f.step || (f.type === "int" ? 1 : "any"), placeholder: f.type === "optfloat" ? "off" : "" });
    if (f.min !== undefined) input.min = f.min;
    if (f.max !== undefined) input.max = f.max;
  } else if (f.type === "port") {
    return portInput(f);
  } else {
    input = el("input", { type: "text", id: "f_" + f.key, value: str(f.value), spellcheck: false });
  }
  inputs[f.key] = input;
  input.addEventListener("input", updateDirty);
  input.addEventListener("change", updateDirty);
  return input;
}

function portInput(f) {
  const sel = el("select", { id: "f_SERIAL_PORT_sel" });
  const custom = el("input", { type: "text", id: "f_SERIAL_PORT", value: str(f.value), spellcheck: false,
    placeholder: "/dev/serial/by-id/..." });
  const refresh = el("button", { className: "btn small", type: "button", textContent: "↻", title: "Rescan adapters" });
  const wrap = el("div", { className: "portpick" }, sel, refresh, custom);
  inputs.SERIAL_PORT = custom;
  const load = async () => {
    const { ports } = await api("/api/v1/settings/serial-ports");
    sel.replaceChildren(...ports.map((p) => el("option", { value: p.path, textContent: (p.stable ? "" : "⚠ ") + p.label })),
      el("option", { value: "__custom", textContent: "Other (type a path)…" }));
    const cur = custom.value;
    sel.value = ports.some((p) => p.path === cur) ? cur : "__custom";
    custom.hidden = sel.value !== "__custom";
  };
  sel.onchange = () => {
    if (sel.value !== "__custom") custom.value = sel.value;
    custom.hidden = sel.value !== "__custom";
    if (!custom.hidden) custom.focus();
    updateDirty();
  };
  custom.addEventListener("input", updateDirty);
  refresh.onclick = () => load().catch((e) => alert(e.message));
  load().catch(() => {});
  return wrap;
}

function render(data) {
  schema = data;
  $("envfile").textContent = data.env_file;
  for (const k of Object.keys(inputs)) delete inputs[k];
  const out = data.sections.map((sec) => {
    const rows = sec.fields.map((f) => {
      original[f.key] = f.type === "secret" ? "" : str(f.value);
      return el("div", { className: "srow", id: "row_" + f.key },
        el("label", { htmlFor: "f_" + f.key, textContent: f.label }),
        el("div", { className: "sval" }, fieldInput(f), f.help ? el("div", { className: "help", textContent: f.help }) : null));
    });
    const card = el("section", { className: "card", id: "sec_" + sec.id }, el("h2", { textContent: sec.title }), ...rows);
    if (sec.id === "serial") card.append(serialTools());
    if (sec.id === "db") card.append(dbTools());
    return card;
  });
  $("sections").replaceChildren(...out);
  $("readonly").replaceChildren(...Object.entries(data.readonly).flatMap(([k, v]) =>
    [el("dt", { textContent: k }), el("dd", { textContent: typeof v === "boolean" ? (v ? "enabled" : "disabled") : String(v) })]));
  updateDirty();
}

// ---------------------------------------------------------------- values
function current(key) {
  const i = inputs[key];
  return i.type === "checkbox" ? String(i.checked) : i.value.trim();
}
function changed() {
  return Object.keys(inputs).filter((k) => current(k) !== original[k]);
}
function updateDirty() {
  const c = changed();
  Object.keys(inputs).forEach((k) => $("row_" + k)?.classList.toggle("changed", c.includes(k)));
  $("savebar").hidden = c.length === 0;
  $("dirty").textContent = `${c.length} unsaved change${c.length === 1 ? "" : "s"}`;
}
function payload(keys) {
  const body = {};
  keys.forEach((k) => {
    const i = inputs[k];
    body[k] = i.type === "checkbox" ? i.checked : i.value.trim();
  });
  return body;
}
const SERIAL = ["SERIAL_PORT", "SERIAL_BAUD", "SERIAL_PARITY", "SERIAL_STOPBITS", "MODBUS_ADDRESS", "SERIAL_TIMEOUT", "MODBUS_RETRIES"];
const DB = ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"];

// ---------------------------------------------------------------- tools
function result(box, cls, text) { box.className = "result " + cls; box.textContent = text; }

function serialTools() {
  const box = el("div", { className: "result" });
  const test = el("button", { className: "btn", type: "button", textContent: "Test connection" });
  const detect = el("button", { className: "btn", type: "button", textContent: "Auto-detect baud / parity" });
  test.onclick = async () => {
    result(box, "busy", "Testing… (live polling pauses for a moment)");
    test.disabled = detect.disabled = true;
    try {
      const r = await api("/api/v1/settings/test-serial", payload(SERIAL));
      if (r.ok) result(box, "ok", `✓ Controller answered in ${r.ms} ms · PV ${r.pv} ${r.units} · mode: ${r.mode} · SP range ${r.sp_min}–${r.sp_max}`);
      else result(box, "err", "✗ No valid answer: " + r.error);
    } catch (e) { result(box, "err", "✗ " + e.message); }
    test.disabled = detect.disabled = false;
  };
  detect.onclick = async () => {
    result(box, "busy", "Scanning baud rates and parity at this address… up to 40 s");
    test.disabled = detect.disabled = true;
    try {
      const r = await api("/api/v1/settings/detect-serial", payload(SERIAL));
      if (r.found) {
        inputs.SERIAL_BAUD.value = String(r.SERIAL_BAUD);
        inputs.SERIAL_PARITY.value = r.SERIAL_PARITY;
        updateDirty();
        result(box, "ok", `✓ Found: ${r.SERIAL_BAUD} baud, parity ${r.SERIAL_PARITY}` +
          (r.pv !== undefined ? ` (PV ${r.pv})` : "") + ` after ${r.tried} tries. Click “Save & apply” to keep it.`);
      } else result(box, "err", `✗ ${r.error} (${r.tried} combinations tried)`);
    } catch (e) { result(box, "err", "✗ " + e.message); }
    test.disabled = detect.disabled = false;
  };
  return el("div", { className: "tools" }, el("div", { className: "btns" }, test, detect), box);
}

function dbTools() {
  const box = el("div", { className: "result" });
  const test = el("button", { className: "btn", type: "button", textContent: "Test database" });
  test.onclick = async () => {
    result(box, "busy", "Connecting…");
    try {
      const r = await api("/api/v1/settings/test-db", payload(DB));
      result(box, r.can_create ? "ok" : "err",
        `✓ PostgreSQL ${r.server_version} in ${r.ms} ms · telemetry table ${r.telemetry_table ? "present" : "will be created"}` +
        (r.can_create ? "" : " · ✗ user cannot create tables in schema public"));
    } catch (e) { result(box, "err", "✗ " + e.message); }
  };
  return el("div", { className: "tools" }, el("div", { className: "btns" }, test), box);
}

// ---------------------------------------------------------------- save
$("discard").onclick = () => render(schema);
$("save").onclick = async () => {
  const keys = changed();
  if (!keys.length) return;
  const serialChange = keys.some((k) => SERIAL.includes(k));
  if (keys.includes("CONTROL_ENABLED") && inputs.CONTROL_ENABLED.checked &&
      !confirm("Enable remote control? Logged-in users and API keys will be able to start the oven.")) return;
  if (serialChange && !confirm("Apply new connection settings now? Monitoring reconnects using the new settings.")) return;
  $("save").disabled = true;
  try {
    const r = await api("/api/v1/settings", payload(keys));
    render(r.settings);
    toast(`Saved & applied: ${r.changed.join(", ")}`, "ok");
  } catch (e) {
    toast("Not saved: " + e.message, "err");
  }
  $("save").disabled = false;
};
function toast(text, cls) {
  const t = el("div", { className: "toast " + cls, textContent: text });
  document.body.append(t);
  setTimeout(() => t.remove(), cls === "err" ? 9000 : 4000);
}
window.addEventListener("beforeunload", (e) => { if (changed().length) e.preventDefault(); });

// ---------------------------------------------------------------- live status
async function status() {
  try {
    const s = await api("/api/v1/status");
    const t = $("conn");
    if (s.paused) { t.textContent = "paused: " + s.paused; t.className = "tag warn"; }
    else if (s.comm_ok) { t.textContent = "controller online"; t.className = "tag ok"; }
    else { t.textContent = "controller offline"; t.className = "tag bad"; t.title = s.comm_error || ""; }
  } catch (e) { /* ignore */ }
}

api("/api/v1/settings").then(render).catch((e) => { $("sections").textContent = "Could not load settings: " + e.message; });
status(); setInterval(status, 3000);
