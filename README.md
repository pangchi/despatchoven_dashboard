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
- **Event log** page: alarms, high-limit trips, sensor breaks and front-panel changes detected from the controller, plus every remote command, run, settings change, sign-in and system event. Each has a category and severity, and the page has filters, search, live updates and CSV export.
- **Run log**: every profile, manual and timer run is recorded with its start, end, result (completed / aborted / stopped …), profile number, lot label, notes and summary stats. It has its own chart, with profile segments and holds marked, plus a CSV export.
- **Settings page** in the dashboard: serial/Modbus connection with *Test connection* and *Auto-detect baud / parity*, database with *Test database*, sampling, purge and control limits. Saved to `.env` and applied live, with no restart.
- Starts even when the database is unreachable, so you can fix it from Settings; it reconnects every 30 s on its own.
- `SERIAL_PORT=SIMULATOR` runs a simulated oven for development without hardware.

## Version

The version lives in the **`VERSION`** file in the project root, and everything reads it from there:

| Where | Shows |
|---|---|
| Every page header, login page, dashboard *System* panel, Settings | `v1.4.1` |
| `GET /api/v1/health`, `/status`, `/system` | `"version": "1.4.1"` |
| Every HTTP response | `X-App-Version: 1.4.1` header |
| `venv/bin/python -m app.tools version` | `1.4.1` |
| `setup.sh`, `install-service.sh`, service log on start, audit log `startup` event | the version being installed / started |

Static files are served as `app.js?v=1.4.1`, so browsers load fresh JS/CSS after an update instead of a cached old copy.
To release a new version: change `VERSION`, add a section to `CHANGELOG.md`, then restart the service.

```bash
curl -s http://<pi>:8000/api/v1/health      # {"ok":true,...,"version":"1.4.1"}
```

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

## Login security (lock after failed attempts)

Sign-in and API keys lock after **consecutive failed attempts**:

| What is counted | Locks | Why |
|---|---|---|
| The dashboard **account** (`WEB_USERNAME`) | sign-in to that account from anywhere | stops password guessing spread over many machines |
| The **address** signing in | sign-in from that address, any username | stops one machine trying names and passwords |
| The **address** using API keys | API-key requests from that address (HTTP `429` + `Retry-After`) | kept separate, so a script with an old key can't lock the operator out of the dashboard |

**How locks work:**
- **Trigger:** after `LOCKOUT_ATTEMPTS` (default **5**) failures in a row, the account or address is locked.
- **Duration:** the first lock lasts `LOCKOUT_MINUTES` (default **2**), and each further lock doubles (2 → 4 → 8 → 16 …) up to `LOCKOUT_MAX_MINUTES` (default **60**).
- **Reset:** a successful sign-in resets the count. After `LOCKOUT_RESET_HOURS` (default **24**) without failures, the count and lock level fall back to zero.
- **While locked:** the password isn't checked at all, and the login page shows *"Sign-in is locked for N minutes"*. With 2 or fewer attempts left it warns *"N attempts left before sign-in is locked"*.
- **Restarts:** locks are stored in PostgreSQL (`auth_lockouts`), so a restart doesn't clear them. If the database is down, they're kept in memory until it's back.
- **Wrong usernames:** the password check always runs, so response time doesn't reveal whether a username exists. Wrong usernames count against the address only.
- **Already signed in:** a dashboard session that is already signed in keeps working even if its address is locked for API keys.

**Real client address:** `X-Real-IP` / `X-Forwarded-For` headers are **ignored** unless `TRUST_PROXY=true`. Without that, anyone could send a fake address with every attempt and never be locked. Turn it on only when nginx, Caddy or similar sits in front of the app and sets those headers.

**Seeing and clearing locks:**
- **Settings → Login security** shows current locks and addresses with failed attempts, with **Unlock** / **Reset** buttons and **Unlock all**. The limits can be changed there too.
- **Locked yourself out?** On the Pi:
  ```bash
  cd /opt/oven-dashboard
  sudo -u <service-user> venv/bin/python -m app.tools lockouts            # list
  sudo -u <service-user> venv/bin/python -m app.tools unlock user admin   # or: unlock ip 192.168.1.20 / unlock all
  ```
- **Event log:** every failure, lock (as an **alarm**) and unlock goes into the Event log under *Access*.

Note: someone who knows your username can keep the **account** locked by failing on purpose. That's the price of stopping guessing spread over many machines. If that happens, check the Event log for the address and unlock from the Pi.

## Changing the password

**From the dashboard:** **Settings → Change password** (at the top). Enter the current password and the new one twice, then click **Change password**.
- **New password rules:** at least 8 characters, different from the current one, not the user name, and not a repeated character. The page shows live hints for length and whether the two entries match.
- **Applies at once:** the new hash is written to `.env` (quoted; the previous file is kept as `.env.bak`) and used immediately, with no restart.
- **Other browsers are signed out; this one stays signed in.** Each sign-in stores a fingerprint of the current password hash, and a session whose fingerprint no longer matches is dropped. Browsers signed in before v1.8.0 have no fingerprint and must sign in once more after the update.
- **Lockout applies:** a wrong current password counts toward the sign-in lockout, so a browser left signed in can't be used to guess the password.
- **Audit:** the change (`password_changed`) and refusals (`password_change_failed`) go into the Event log under *Access*.
- **Who can change it:** only a password session. Passwordless local users and API keys are refused.

**From the Pi's command line** (e.g. if you forgot the password):
```bash
cd /opt/oven-dashboard
sudo -u $(stat -c %U .env) venv/bin/python -m app.tools hash-password   # prints WEB_PASSWORD_HASH='…'
sudo nano .env                                                           # replace that line, keep the quotes
sudo systemctl restart oven-dashboard
```
To change the user name, edit `WEB_USERNAME` in `.env` and restart.

## No password on the local network (optional)

The dashboard can open **without a password** from addresses you trust, such as a screen on the Pi itself or PCs on the workshop LAN. It is **off by default**. Turn it on in **Settings → Login security**, or in `.env`:

| Setting | Example | Meaning |
|---|---|---|
| `LOCAL_NETWORKS` (*No password from*) | `localhost` | Only a browser on the Pi itself (kiosk / touchscreen) |
| | `localhost, 192.168.1.0/24` | The Pi and the whole workshop LAN |
| | `192.168.1.20, 192.168.1.21` | Specific PCs |
| `LOCAL_ACCESS` (*Without password, allow*) | `control` (default) / `view` | `view` = watch only: control panel and label editing hidden, commands refused |
| `ALLOWED_HOSTS` (*Extra host names*) | `oven.lan` | Names besides IP / `localhost` / the Pi's own name that you type in the browser |

**What passwordless users see:**
- **Header:** a green **Local · 192.168.1.20** badge (plus *view only* if set) and a **Sign in** button. Signing in with the password gives full control as usual.
- **Settings:** always need the password, so a passwordless user cannot widen access, switch off the lockout, or change the database.
- **Audit trail:** commands are logged as `local:<address>`, and each address's first visit per hour is logged (*Access* category).
- **API clients:** still need an API key. Requests without a key are only accepted when they come from a browser on a trusted address, because of the browser-only header check below.

**Built-in safeguards:**
- **Reverse-proxy guard:** a request that carries `X-Forwarded-For` / `X-Real-IP` is **never** treated as local unless *Behind a reverse proxy* (`TRUST_PROXY`) is on. Otherwise a proxy on the Pi would make every outside visitor look like `localhost`. With `TRUST_PROXY=true`, the address the proxy reports is used.
- **Host check (DNS rebinding):** passwordless access only works when the page is opened by IP address, `localhost`, the Pi's hostname (also `.local`) or a name in `ALLOWED_HOSTS`. This stops a malicious website from pointing its own domain at the Pi and controlling it through a workshop browser.
- **Cross-site requests:** commands still need the dashboard's `X-Requested-With` header, which another website's page cannot send to the Pi.
- **Whole-internet ranges refused:** `0.0.0.0/0` and `::/0` are rejected.

Recommendation: use `localhost` for a touchscreen on the Pi. For the LAN, prefer `LOCAL_ACCESS=view`, so anyone on the workshop Wi-Fi can watch but starting the oven still needs the password.

## Event log

**Event log** (`/events`, linked from the dashboard header and its *Recent events* panel) is the complete history of what happened to the oven and the monitor.

| Category | Events | Source |
|---|---|---|
| **Process** | Alarm 1–5 active / cleared · **high limit tripped** / reset · control or high-limit thermocouple open / OK · mode, setpoint and profile hold/resume **changed at the controller** · profile segment changes | `controller` |
| **Control** | Every remote command: setpoint, mode, manual/timer/profile, relays, high limit (failures included, with the reason) | `api:<key>` / `web:<user>` |
| **Run** | Run started / ended (with result), label and notes edits, next-run label | `system`, user |
| **Settings** | Settings saved (DB password masked), connection tests, auto-detect | `web:<user>` |
| **Access** | Sign-in, sign-out, **failed sign-in** (username tried, IP, attempts left), **sign-in / API locked** (what, how long, lock number), unlocks, **rejected API key** (IP and path; the key itself is never stored) | `web`, `api`, `cli` |
| **System** | Monitor started (with version), controller communication lost / restored, database connected, purges | `system` |

**Severity:** `alarm` (red) for high-limit trips, thermocouple breaks and blocked sign-ins; `warning` (amber) for alarms, comm loss, failed commands, failed sign-ins, and aborted, error or interrupted runs; `info` for everything else.

How process events are detected:
- They come from comparing each controller reading with the one before. Anything already abnormal when the monitor starts (an active alarm, a tripped high limit) is logged once, marked *already active when monitoring started*.
- Changes made through this app's API or dashboard are logged once, as the command, and are not repeated as "changed at the controller".
- If communication drops, the next reading is compared with the last one before the gap, so a change made in between is still logged.
- Note: a change that happens and reverses entirely within one poll interval (default 2 s) cannot be seen.

The page:
- **Filtering:** severity chips with counts (All / Alarms / Warnings / Info), category chips, text search (event, source and details, e.g. an IP or alarm number), source, a date/time range, and *Failures only*.
- **Rows:** click a row to see the raw event detail; run events link to the run.
- **Live:** new events appear every 5 s and are briefly highlighted.
- **Shareable views:** filters are kept in the URL, so a filtered view (e.g. `/events?severity=alarm`) can be bookmarked.
- **CSV:** **Download CSV** exports the current filter (up to 200 000 rows).
- **Phone:** each event becomes a stacked card.

Events are stored in the `events` table and purged together with telemetry when the disk fills. The upgrade from v1.4 adds `category` and `severity` columns and fills them in for existing events.

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
| Login security | Lock on/off, attempts before lock, first and longest lock, forget-after hours, **no password from** (networks), **without password allow** (control/view), extra host names, behind a reverse proxy | List of current locks with **Unlock** / **Reset**, **Unlock all** |
| Control & safety | Remote control on/off, setpoint min/max clamp, log level | |

How saving works:

- Only changed fields are sent; changed rows get an orange edge and a save bar appears. Leaving the page with unsaved changes asks first.
- Values are checked on the server: ranges, purge stop level below start level, idle rate not faster than active rate, setpoint min below max, disk path exists, and serial port is `/dev/...` or `SIMULATOR`.
- A new database is connected and its tables are created **before** anything is saved. If that fails, nothing is written.
- `.env` is updated in place: comments, order and other keys are kept, the previous file is saved as `.env.bak`, and the file stays `chmod 600`. The write is atomic.
- Changes apply immediately. Serial changes reopen the port with the new settings, database changes switch to the new connection, and everything else takes effect on the next poll.
- During a test or auto-detect, live polling pauses for a moment so the port is free; the header shows `paused: …`.
- Every test and save is recorded in the audit log, with the DB password masked.

The password is changed in its own **Change password** card on this page. Not editable here, only in `.env` with a service restart: `SECRET_KEY`, `API_KEYS`, `WEB_USERNAME` and `ALLOW_HIGH_LIMIT_WRITE`. The high-limit switch stays file-only on purpose, so a stolen dashboard password cannot enable writes to the over-temperature limit.

The service user must be able to write `.env` and its folder. `install-service.sh` already sets this up; otherwise run `sudo chown -R <service-user> /opt/oven-dashboard`.

## Configuration (`.env`)

All credentials live in `.env` (`chmod 600`, excluded by `.gitignore`); `.env.example` documents every setting. Set `P3_ENV_FILE` to load a different file.

| Setting | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | – | Flask session key (required) |
| `WEB_USERNAME` / `WEB_PASSWORD_HASH` | admin / – | Dashboard login (werkzeug hash; change it in Settings → Change password or with `app.tools hash-password`) |
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

Authenticate with `X-API-Key: <key>` or `Authorization: Bearer <key>`. Responses are JSON. After `LOCKOUT_ATTEMPTS` consecutive bad keys an address gets `429` with `Retry-After` (see [Login security](#login-security-lock-after-failed-attempts)). Expose the Pi beyond the LAN only behind HTTPS (reverse proxy or VPN, e.g. Tailscale/WireGuard) – keys are sent in a header.

| Method & path | Body | Notes |
|---|---|---|
| `GET /health` | – | No auth. `{ok, comm_ok, db_ok}` for uptime checks |
| `GET /status` | – | Live snapshot, comm/DB state, logging state |
| `GET /telemetry?minutes=60` or `?from=ISO&to=ISO` | – | Optional `max_points` (default 1500, max 20000) |
| `GET /events?limit=100&offset=0` | – | Event log, newest first; returns `total` and `counts` per severity. Filters: `category` and `severity` (comma lists), `source`, `action`, `q` (text search incl. details), `from`, `to`, `failed=1` |
| `GET /events.csv` | – | Same filters, as CSV |
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

`POST /control/manual`, `/control/timer` and `/control/profile` accept an optional `"label"` on start / run. 

Settings endpoints (**dashboard login only**, API keys get `401`; writes need `X-Requested-With: p3-dashboard`):

| Method & path | Body | Notes |
|---|---|---|
| `GET /settings` | – | Field definitions + current values (DB password never returned) |
| `POST /settings` | `{"SERIAL_BAUD":9600, ...}` | Validate, write `.env`, apply live; returns changed keys |
| `GET /settings/serial-ports` | – | Detected adapters |
| `GET /settings/lockouts` | – | Current locks and failed-attempt counts |
| `POST /settings/password` | `{"current":"…","new":"…","confirm":"…"}` | Change the dashboard password; other sessions are signed out |
| `POST /settings/lockouts/unlock` | `{"scope":"ip","subject":"1.2.3.4"}` or `{"all":true}` | Clear a lock (scope `user`, `ip` or `api`) |
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
  events.py       event categories/severity, queries, CSV, process-event detection
  lockout.py      lock after failed sign-ins / API keys, client address, local-network matching
  templates/_userbox.html  header user / local badge
  web.py          login + dashboard routes
  tools.py        gen-secret, gen-key, hash-password, test-connection
  templates/, static/
deploy/           systemd unit, install.sh, setup_db.sql
setup.sh          venv + requirements + .env bootstrap
install-service.sh  systemd service for the folder it lives in
tests/            pytest (purge policy)
wsgi.py           entry point
VERSION           app version (single source)
```
