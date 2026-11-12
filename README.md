# Protocol 3 Monitor

Remote monitoring and control for a **Despatch Protocol 3** oven controller, hosted on a Raspberry Pi.
The Pi talks to the controller over **RS-485 (Modbus RTU)**, logs telemetry to **PostgreSQL**, serves a
web dashboard, and exposes an **API-key protected REST API** for remote control.

```
Protocol 3 ──RS-485 (term. 19/20/21)── USB-RS485 adapter ── Raspberry Pi
                                                              ├─ poller thread  → PostgreSQL (daily partitions)
                                                              ├─ purge job      → drops oldest days when disk > 80 %
                                                              └─ Flask (gunicorn) → dashboard  +  /api/v1 (X-API-Key)
```

## Features

- Live dashboard: PV, setpoint, high-limit PV, output power, mode/timer/profile status, alarms, relays, trend chart (30 min – 7 days). No CDN – works on an offline LAN.
- Control: mode select, setpoint, manual start/stop, timer start/stop, profile run/hold/release/next/abort, manual relays 1-4, optional high-limit value.
- **Adaptive logging**: every `LOG_INTERVAL_ACTIVE` s (default 5) while a process runs, every `LOG_INTERVAL_IDLE` s (default 60) when idle, plus an immediate row on any state change (mode, run status, segment, relays, alarms, high-limit trip, sensor break).
- **Disk auto-purge**: when the filesystem passes `PURGE_THRESHOLD_PCT` (80 %), the oldest days are dropped until usage is at `PURGE_TARGET_PCT` (75 %). Optional `RETENTION_DAYS`.
- Audit log of every control command, login, purge and comm loss/restore (`events` table, visible on the dashboard).
- **Run log**: every profile, manual and timer run is recorded with its start, end, result (completed / aborted / stopped …), profile number, lot label, notes and summary stats. It has its own chart, with profile segments and holds marked, plus a CSV export.
- **Settings page** in the dashboard: serial/Modbus connection with *Test connection* and *Auto-detect baud / parity*, database with *Test database*, sampling, purge and control limits. Saved to `.env` and applied live, with no restart.
- Starts even when the database is unreachable, so you can fix it from Settings; it reconnects every 30 s on its own.
- `SERIAL_PORT=SIMULATOR` runs a simulated oven for development without hardware.

## Hardware & controller setup

1. **Wiring** – Protocol 3 rear terminals: **19 = −, 20 = +, 21 = common** (RS485 Comms). Connect to a USB-RS485 adapter (2-wire, half duplex). For long runs add a 120 Ω terminator at the far end.
2. **Firmware** – Modbus needs Protocol 3 firmware **2.3 or later** (Main Menu → Product Information → Firmware Information).
3. **Comms settings** – Main Menu → Configuration Menu (unlock code 0010) → Comms: set **Modbus address**, **data rate** and **parity**. Defaults are address 1, 19200 baud, no parity. Match these in `.env`.

## Register map – verify before production

Addresses are in `app/registers.py`, taken from Despatch's *Protocol 3 Modbus Communications* manual (E-106, PN 320813), which the owner's manual (E-105) refers to but does not include. Analog values use the **integer + 1 decimal** format (base address + 0x4000). The source text has some duplicated / ambiguous entries (relay status, alarm block order, timer vs. segment remaining time), so:

- Run `python -m app.tools test-connection` and compare each value with the controller display.
- Try control writes with the oven empty and cold first.
- If an address differs on your firmware, change it in `app/registers.py` only.

## Quick setup (`setup.sh`)

`setup.sh` creates the virtual environment, installs `requirements.txt` and creates `.env` in one go:

```bash
./setup.sh                 # asks for the dashboard password
./setup.sh --yes           # no prompts: generates the password and prints it once
./setup.sh --simulator     # .env uses SERIAL_PORT=SIMULATOR (no hardware)
./setup.sh --dev           # also installs pytest
./setup.sh --recreate      # rebuild the venv from scratch
./setup.sh --pypi-only     # skip piwheels, download from pypi.org only (automatic on 64-bit Pi OS)
./setup.sh --piwheels      # keep piwheels on 64-bit Pi OS
./setup.sh --wheels DIR    # offline install from downloaded wheel files
./setup.sh --upgrade-pip   # also upgrade pip (skipped by default)
PYTHON=python3.11 VENV_DIR=/opt/p3venv ./setup.sh   # pick interpreter / venv location
```

What it does:

- Checks for Python 3.9+ and the `venv` module (on Raspberry Pi OS: `sudo apt install python3-venv`).
- Creates `./venv`, upgrades pip, installs `requirements.txt` and checks that every package imports. If `psycopg2` has to compile (32-bit Pi OS), it tells you to `sudo apt install libpq-dev python3-dev build-essential`.
- Copies `.env.example` to `.env` (`chmod 600`) and fills in a random `SECRET_KEY`, an API key (`client:...`, printed once), the hashed dashboard password and a random `DB_PASSWORD`.
- If exactly one USB serial adapter is plugged in, sets `SERIAL_PORT` to its stable `/dev/serial/by-id/...` path.
- Safe to re-run: an existing `.env` is never overwritten, only empty `SECRET_KEY`, `API_KEYS` and `WEB_PASSWORD_HASH` are filled in.

### Slow or unreliable network (piwheels)

Raspberry Pi OS adds **piwheels** as an extra package source in `/etc/pip.conf`, and it often drops connections. Those drops show up as `Retrying ... RemoteDisconnected` or `SSLEOFError` warnings. `setup.sh` handles this as follows:

- pip waits 60 s per request and retries 10 times (override with `PIP_DEFAULT_TIMEOUT` / `PIP_RETRIES`). The pip self-upgrade is skipped unless you pass `--upgrade-pip`.
- On **64-bit Pi OS (aarch64) piwheels is skipped automatically**: PyPI has ready-built wheels for every requirement, so piwheels only adds a second, flaky source. Use `--piwheels` to keep it. On 32-bit Pi OS piwheels stays on, because that is where the prebuilt `psycopg2-binary` comes from.
- If the install still fails with piwheels in use on a 64-bit Pi, it retries from pypi.org only (`--pypi-only` forces this anywhere).
- A proxy is picked up from `HTTPS_PROXY=http://proxy:port ./setup.sh`.
- On failure it prints checks (`curl` to PyPI/piwheels, `date` – a wrong clock breaks HTTPS) and the exact offline command for this Pi.

**Offline install (64-bit Pi OS).** On any PC with internet, set `--python-version` to the Pi's version (`python3 --version`: 311 for Bookworm, 313 for Trixie):

```bash
pip download -r requirements.txt -d wheels --only-binary=:all: \
    --platform manylinux2014_aarch64 --python-version 311
```

Copy the `wheels` folder to the Pi and run `./setup.sh --wheels wheels`.
On 32-bit Pi OS, PyPI has no `psycopg2-binary` wheel, so it must come from piwheels or be compiled (`sudo apt install libpq-dev python3-dev build-essential`).

It does not create the PostgreSQL role and database; it prints the two `psql` commands to run, using the generated `DB_PASSWORD`.

## Run as a service (`install-service.sh`)

After `setup.sh`, register the app with systemd right where it is installed (e.g. `/opt/oven-dashboard`):

```bash
cd /opt/oven-dashboard
sudo -u postgres psql -c "CREATE ROLE protocol3 LOGIN PASSWORD '<DB_PASSWORD from .env>';"
sudo -u postgres psql -c "CREATE DATABASE protocol3 OWNER protocol3;"
venv/bin/python -m app.tools test-connection     # optional: read the controller once
sudo ./install-service.sh                        # install, enable at boot, start, health-check
```

| Option | Effect |
|---|---|
| `--port 8080` | Listen port (default 8000) |
| `--user NAME` | Run as this user; created as a system user if missing. Default: the user who ran `sudo` |
| `--name NAME` | Service name (default: folder name, e.g. `oven-dashboard`) |
| `--no-start` | Install and enable only |
| `--dry-run` | Print the unit file, change nothing (no sudo needed) |
| `--uninstall` | Stop, disable and remove the service; app files are kept |

What it does:

- Checks that `venv/bin/gunicorn`, `wsgi.py` and `.env` exist, and refuses to run the service as root.
- Creates the user if needed and adds it to `dialout` for the RS-485 adapter. If the app folder belongs to someone else, it hands ownership to that user and keeps `.env` at `chmod 600`.
- Writes `/etc/systemd/system/<name>.service`: a single gunicorn worker, restart on failure, start after the network, and after PostgreSQL when `DB_HOST` is local.
- Warns if `SERIAL_PORT` is missing, if the port is already taken, or if the older `protocol3-monitor` service is also enabled (two copies would fight over the serial port).
- Restarts the service and waits up to 30 s for `/api/v1/health`, then prints the dashboard URL. If the service doesn't come up, it shows the last 30 journal lines and exits with an error.

Re-run it to change the port or user. After editing `.env`, `sudo systemctl restart oven-dashboard` is enough.

```bash
journalctl -u oven-dashboard -f          # live logs
systemctl status oven-dashboard
```

`deploy/install.sh` is the older all-in-one route: it copies the app to `/opt/protocol3-monitor` and uses the `p3mon` user. Use one route or the other, not both.

Open `http://<pi>:8000/`. Use a stable port name such as `/dev/serial/by-id/usb-FTDI_...` instead of `/dev/ttyUSB0`.

> Run with **one** gunicorn worker (`-w 1 --threads 4`, as in the unit file). Multiple workers would each start a poller and fight over the serial port.

### Development (no hardware)

```bash
./setup.sh --simulator --dev
venv/bin/python wsgi.py
venv/bin/pytest -q
```

## Runs (per-run logging)

A **run** is one process from start to stop:

| Type | Starts when | Ends when → result |
|---|---|---|
| Profile | controller is in profile mode and the profile is running, held or delayed | status *ended* → `completed`, *aborted* → `aborted`, *error* → `error`, otherwise `stopped` |
| Manual | manual mode and manual status *running* | stopped → `stopped` |
| Timer | timer mode and timer *running* | time reached 0 → `completed`, otherwise `stopped` |

Holds and delays stay inside the same run; hold time is measured and shaded on the chart. A different profile number starts a new run.

**What is recorded:**
- **Telemetry:** every sample logged during a run carries its `run_id`, including the final sample that shows how it ended. Runs are logged at the active rate (`LOG_INTERVAL_ACTIVE`), with an extra sample at every segment change.
- **Run record** (`runs` table): start and end time, type, profile number, label, notes, who started it (`api:<key>`, `web:<user>`, or `front panel` if no remote command came first), end reason, and stats computed when the run ends. The stats are samples, PV min/max/avg, max SP, **max PV − SP while running**, high-limit PV max, average power, alarms seen, high-limit trip, thermocouple break, hold time and last segment.
- **Monitor restarts:** if the monitor restarts during a run and the controller is still running the same program, the run continues under the same ID and the restart is counted (`resumed`). If the run ended while the monitor was down, it is closed as `interrupted` at its last recorded sample, and the end time is flagged as approximate.
- **Purge:** old telemetry is purged by disk space or retention as before. Run records are tiny and are kept, so you still see the summary after the samples are gone.

**Lot / batch labels:**
- **Dashboard:** the run card lets you set a label for the **next** run; it is stored in the database and survives a restart. While a run is in progress, the same field renames the current run.
- **API:** add `"label"` to the start command, e.g. `POST /control/profile {"action":"run","profile":3,"label":"LOT-1234"}`. This also works on manual and timer starts.
- **Afterwards:** edit the label and notes on the run page.

**Pages:**
- **Runs** (`/runs`): list with search over label and notes, filters by type, profile number, result and date range, and paging.
- **Run detail** (`/runs/<id>`): result and flags, stat tiles, a chart with time since start on the x-axis (segments S1, S2 … shaded alternately, holds highlighted), label and notes, audit events during the run, and **Download CSV** (every sample, file named `run<id>_<date>_<label>.csv`). It updates live while the run is in progress.

## Settings page

Open **Settings** (top right of the dashboard, or `http://<pi>:8000/settings`). You need to be logged in to the dashboard; API keys are refused here, so a key can operate the oven but cannot reconfigure the system.

| Section | What you can set | Tools |
|---|---|---|
| Controller connection | Serial port (dropdown of detected adapters, `/dev/serial/by-id/...` first, ⚠ marks names that can change on reboot), baud, parity, stop bits, Modbus address, timeout, retries | **Test connection** reads PV, mode and SP range with the values on screen. **Auto-detect** tries every baud rate (1200–115200) × parity N/E/O at that address (≤ 40 s) and fills in the first combination that answers |
| Database | Host, port, name, user, password (blank = keep) | **Test database** shows server version, whether tables exist and whether the user may create them |
| Sampling & logging | Live poll, active/idle log rates, active-above-PV threshold, hold-off | |
| Storage & purge | Purge start/stop %, retention days, check interval, disk to watch | |
| Control & safety | Remote control on/off, setpoint min/max clamp, log level | |

How saving works:

- Only changed fields are sent; changed rows get an orange edge and a save bar appears. Leaving the page with unsaved changes asks first.
- Values are checked on the server: ranges, purge stop level below start level, idle rate not faster than active rate, setpoint min below max, disk path exists, and serial port is `/dev/...` or `SIMULATOR`.
- A new database is connected and its tables are created **before** anything is saved. If that fails, nothing is written.
- `.env` is updated in place: comments, order and other keys are kept, the previous file is saved as `.env.bak`, and the file stays `chmod 600`. The write is atomic.
- Changes apply immediately. Serial changes reopen the port with the new settings, database changes switch to the new connection, and everything else takes effect on the next poll.
- During a test or auto-detect, live polling pauses for a moment so the port is free; the header shows `paused: …`.
- Every test and save is recorded in the audit log, with the DB password masked.

Not editable here, only in `.env` with a service restart: `SECRET_KEY`, `API_KEYS`, `WEB_USERNAME`/`WEB_PASSWORD_HASH` and `ALLOW_HIGH_LIMIT_WRITE`. The high-limit switch stays file-only on purpose, so a stolen dashboard password cannot enable writes to the over-temperature limit.

The service user must be able to write `.env` and its folder. `install-service.sh` already sets this up; otherwise run `sudo chown -R <service-user> /opt/oven-dashboard`.

## Configuration (`.env`)

All credentials live in `.env` (`chmod 600`, excluded by `.gitignore`); `.env.example` documents every setting. Set `P3_ENV_FILE` to load a different file.

| Setting | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | – | Flask session key (required) |
| `WEB_USERNAME` / `WEB_PASSWORD_HASH` | admin / – | Dashboard login (werkzeug hash) |
| `API_KEYS` | – | Comma-separated `name:key`; name is recorded in the audit log |
| `DB_HOST/PORT/NAME/USER/PASSWORD` | localhost… | PostgreSQL connection |
| `SERIAL_PORT` | /dev/ttyUSB0 | RS-485 device or `SIMULATOR` |
| `SERIAL_BAUD`, `SERIAL_PARITY`, `SERIAL_STOPBITS`, `MODBUS_ADDRESS` | 19200, N, 1, 1 | Must match controller Comms menu |
| `POLL_INTERVAL` | 2 | Live poll (s) for the dashboard |
| `LOG_INTERVAL_ACTIVE` / `LOG_INTERVAL_IDLE` | 5 / 60 | DB logging rate (s) |
| `IDLE_PV_THRESHOLD` | blank | PV above this counts as active (logs cool-down at full rate) |
| `IDLE_HOLDOFF` | 300 | Seconds to stay at the active rate after a run ends |
| `DISK_CHECK_PATH` | / | Filesystem holding the PostgreSQL data dir |
| `PURGE_THRESHOLD_PCT` / `PURGE_TARGET_PCT` | 80 / 75 | Purge start / stop levels |
| `PURGE_CHECK_INTERVAL` | 600 | Seconds between purge checks |
| `RETENTION_DAYS` | 0 | Also drop days older than this (0 = off) |
| `CONTROL_ENABLED` | true | `false` = monitor-only, all control endpoints return 403 |
| `ALLOW_HIGH_LIMIT_WRITE` | false | Enables `POST /control/high-limit` |
| `SETPOINT_MIN` / `SETPOINT_MAX` | blank | Extra clamp on top of the controller's own SP limits |

**"Active"** means manual or timer running, profile running/held/delayed, output power > 0, or PV above `IDLE_PV_THRESHOLD`.

## Data storage & purge

- `telemetry` is partitioned **by day (UTC)**; partitions for yesterday→+2 days are created automatically and hourly.
- Purging **drops whole partitions**, which frees disk space immediately (a `DELETE` would not shrink files without `VACUUM FULL`). Today's partition is never dropped. Events older than the oldest remaining telemetry day go with it.
- If usage stays above target after all past days are gone, an error is logged – something else is filling the disk.
- If PostgreSQL runs on another host, point `DISK_CHECK_PATH` at a local mount of that disk or rely on `RETENTION_DAYS`; the check is of the Pi's filesystem.
- Long ranges are averaged into time buckets server-side (`max_points`), so the 7-day chart stays fast.

## REST API (`/api/v1`)

Authenticate with `X-API-Key: <key>` or `Authorization: Bearer <key>`. Responses are JSON. After 10 failed key/login attempts in 5 minutes an IP gets `429`. Expose the Pi beyond the LAN only behind HTTPS (reverse proxy or VPN, e.g. Tailscale/WireGuard) – keys are sent in a header.

| Method & path | Body | Notes |
|---|---|---|
| `GET /health` | – | No auth. `{ok, comm_ok, db_ok}` for uptime checks |
| `GET /status` | – | Live snapshot, comm/DB state, logging state |
| `GET /telemetry?minutes=60` or `?from=ISO&to=ISO` | – | Optional `max_points` (default 1500, max 20000) |
| `GET /events?limit=100` | – | Audit log |
| `GET /system` | – | Disk, DB size, retention, last purge |
| `POST /system/purge` | – | Run the purge check now |
| `POST /control/mode` | `{"mode":"manual"\|"timer"\|"profile"\|"select"}` | Refused while a process is running |
| `POST /control/setpoint` | `{"value":150.0}` | Manual/timer SP, clamped to controller SP min/max and `.env` limits |
| `POST /control/manual` | `{"action":"start"\|"stop"}` | Requires manual mode |
| `POST /control/timer` | `{"action":"start","seconds":3600}` | Requires timer mode; seconds 1–65535 |
| `POST /control/profile` | `{"action":"run","profile":3}` | Actions: run, hold, release, next, abort, exit_hold_sp. `profile` (0–63) only with run |
| `POST /control/relay` | `{"relay":1,"on":true}` | Manual-mode relays 1–4 |
| `POST /control/high-limit` | `{"value":250}` | Only if `ALLOW_HIGH_LIMIT_WRITE=true` |

Runs endpoints (API key or dashboard login):

| Method & path | Body | Notes |
|---|---|---|
| `GET /runs?limit=50&offset=0` | – | Filters: `mode`, `profile`, `result` (running/completed/aborted/stopped/error/interrupted), `q` (label/notes), `from`, `to` |
| `GET /runs/current` | – | Run in progress (with live stats) and the pending next-run label |
| `POST /runs/next-label` | `{"label":"LOT-1234"}` | Label for the next run to start; `""` clears it |
| `GET /runs/<id>` | – | Run record + stats |
| `PATCH /runs/<id>` | `{"label":"…","notes":"…"}` | Edit label (≤ 100 chars) / notes (≤ 2000) |
| `GET /runs/<id>/telemetry?max_points=2000` | – | Samples of the run, averaged into buckets if more |
| `GET /runs/<id>.csv` | – | All samples as CSV (streamed) |

`POST /control/manual`, `/control/timer` and `/control/profile` accept an optional `"label"` on start / run. `GET /events` also takes `from` / `to`.

Settings endpoints (**dashboard login only**, API keys get `401`; writes need `X-Requested-With: p3-dashboard`):

| Method & path | Body | Notes |
|---|---|---|
| `GET /settings` | – | Field definitions + current values (DB password never returned) |
| `POST /settings` | `{"SERIAL_BAUD":9600, ...}` | Validate, write `.env`, apply live; returns changed keys |
| `GET /settings/serial-ports` | – | Detected adapters |
| `POST /settings/test-serial` | serial fields (optional) | Test with these values, current ones for anything omitted |
| `POST /settings/detect-serial` | `SERIAL_PORT`, `MODBUS_ADDRESS` (optional) | Baud/parity scan |
| `POST /settings/test-db` | DB fields (optional) | Connection test |

Errors: `400` bad input / wrong mode, `401` no or bad key, `403` control disabled, `502` controller did not respond or write-verify failed, `503` database unavailable.

```bash
KEY='scada:...'; KEY=${KEY#*:}
curl -H "X-API-Key: $KEY" http://pi:8000/api/v1/status
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"mode":"manual"}'   http://pi:8000/api/v1/control/mode
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"value":120}'       http://pi:8000/api/v1/control/setpoint
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{"action":"start"}'  http://pi:8000/api/v1/control/manual
```

The dashboard uses the same API with its login session; cookie-authenticated writes must carry `X-Requested-With: p3-dashboard` (CSRF guard).

## Safety notes

- The serial port is opened **exclusively**. While the service runs, `python -m app.tools test-connection` reports *Could not exclusively lock port*. Use **Settings → Test connection** instead, or stop the service first. This stops two programs from talking on the RS-485 bus at once.

- The Protocol 3 high-limit relay is the oven's hardware safety. This app **cannot reset a tripped high limit** (that needs the Reset key on the controller after the cause is fixed) and high-limit writes are off by default. Per the owner's manual, set the high limit 5–15° above the highest programmed setpoint, or below the product's damage temperature.
- Remote start means the oven can heat with nobody present – keep `CONTROL_ENABLED=false` where that is not acceptable.
- Settings written over Modbus are stored in the controller's non-volatile memory; avoid scripts that write the same register in a tight loop.

## Project layout

```
app/
  __init__.py     app factory, starts poller
  config.py       .env loading and validation
  registers.py    Modbus register map (edit here if addresses differ)
  controller.py   Modbus RTU client (minimalmodbus) + simulator
  poller.py       polling, active/idle detection, logging, housekeeping
  db.py           PostgreSQL schema, partitions, queries
  purge.py        disk / retention purge
  auth.py         API key + session auth, throttling
  api.py          REST API
  settings.py     editable settings: schema, validation, .env writer, serial/DB tests
  settings_api.py settings endpoints + live apply
  runs.py         run tracking (start/end detection, stats, queries)
  runs_api.py     runs endpoints, CSV export
  web.py          login + dashboard routes
  tools.py        gen-secret, gen-key, hash-password, test-connection
  templates/, static/
deploy/           systemd unit, install.sh, setup_db.sql
setup.sh          venv + requirements + .env bootstrap
install-service.sh  systemd service for the folder it lives in
tests/            pytest (purge policy)
wsgi.py           entry point
```
