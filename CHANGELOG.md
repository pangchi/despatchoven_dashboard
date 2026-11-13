# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic versioning.

## [1.4.1] - 2026-11-13

### Added
- `VERSION` file as the single source of the app version.
- Version shown in every page header, on the login page, in the dashboard System panel and on the Settings page.
- `version` in `/api/v1/health`, `/status` and `/system`, plus an `X-App-Version` header on every response.
- `python -m app.tools version`; `setup.sh` and `install-service.sh` print the version they install.

### Changed
- Static files carry `?v=<version>`, so browsers pick up new JS/CSS right after an update.
- Header wraps on narrow screens, and the username is hidden below 480 px wide.

## [1.4.0] - 2026-11-12

### Added
- **Per-run logging.** Every profile, manual and timer run gets a record in the new `runs` table, and every telemetry
  row carries `run_id`. Each record holds start/end, result (completed / aborted / stopped / error / interrupted),
  profile number, who started it, label, notes and stats: PV min/max/avg, max SP, max PV − SP while running,
  high-limit PV max, average power, alarms, high-limit trip, thermocouple break, hold time and last segment.
- Runs survive a monitor restart: the same run resumes if the controller is still running it; otherwise it is closed
  as `interrupted` at its last sample with an approximate end time.
- Lot/batch labels: set the label for the next run from the dashboard or `POST /api/v1/runs/next-label` (persisted
  in the new `app_state` table), pass `"label"` with a start command, or edit it later.
- **Runs** page (search, filters, paging) and **run detail** page (stats, chart with segment bands and hold shading,
  label/notes editing, events during the run, CSV download, live while running).
- Current-run card on the dashboard (running run with elapsed time, or the next-run label).
- API: `/api/v1/runs`, `/runs/current`, `/runs/next-label`, `/runs/<id>` (GET/PATCH), `/runs/<id>/telemetry`,
  `/runs/<id>.csv`; `/events` accepts `from`/`to`.
- Unit tests for run start/end classification.

### Changed
- Run start/end always forces a telemetry row, so the first and last state of every run is captured.
- Chart drawing moved to a shared `static/chart.js` (dashboard and run pages).
- Simulator: `P3_SIM_SEGMENT_S` sets the demo profile segment length; holds keep the profile setpoint.
- Dashboard: the process-value tile no longer wraps its unit onto a second line.

### Upgrade notes
- The schema upgrades itself on start (`ALTER TABLE telemetry ADD COLUMN run_id`, new `runs` and `app_state`
  tables). Telemetry logged before this version has no run.

## [1.3.0] - 2026-11-12

### Added
- **Settings page** (`/settings`, linked from the dashboard header) for the controller connection, database,
  sampling, storage/purge and control limits. Saves to `.env` (comments and order kept, `.env.bak` backup,
  atomic write, `chmod 600`) and applies changes live, with no restart.
- Serial tools: adapter dropdown (`/dev/serial/by-id` first), **Test connection** and **Auto-detect baud / parity**
  (scans 1200–115200 × N/E/O at the configured address).
- **Test database** button. A new database is connected and its schema created before settings are saved;
  if that fails, nothing is written.
- Settings endpoints under `/api/v1/settings` (dashboard login only; API keys are refused).
- Audit log entries for settings tests and saves (DB password masked).
- Unit tests for settings validation and `.env` writing.

### Changed
- The app starts even when PostgreSQL is unreachable: the dashboard shows the DB error, data endpoints return `503`,
  and the connection is retried every 30 s, so database settings can be fixed from the UI.
- The serial port is opened with its own fully configured `serial.Serial` object and an exclusive lock,
  instead of minimalmodbus's shared per-device cache. Reconnecting with different settings no longer inherits
  a stale configuration, and a second program can no longer share the RS-485 port.
- Auto-detect skips baud/parity combinations the adapter cannot set, instead of aborting the scan.
- The poller can pause between polls (used by tests and live apply); `/status` reports `paused`.

## [1.2.0] - 2026-11-12

### Added
- `install-service.sh`: installs the app as a systemd service from the folder it lives in (e.g.
  `/opt/oven-dashboard`) using the existing venv and `.env`. Options `--port`, `--user` (creates a system user
  if missing), `--name`, `--no-start`, `--dry-run`, `--uninstall`. Adds the user to `dialout`, fixes
  ownership and `.env` permissions, waits for PostgreSQL when it is local, checks `/api/v1/health` after
  start and prints the journal if the service fails. Safe to re-run.

### Changed
- README: the service section now uses `install-service.sh`; `deploy/install.sh` is documented as the older
  all-in-one route.

## [1.1.2] - 2026-11-11

### Changed
- `setup.sh` skips piwheels by default on 64-bit Pi OS (aarch64). pip was still fetching some files from
  piwheels alongside PyPI and stalling on dropped connections. New `--piwheels` flag keeps it.
  32-bit Pi OS is unchanged.

## [1.1.1] - 2026-11-11

### Added
- `setup.sh --pypi-only`: ignore `/etc/pip.conf` (piwheels) and install from pypi.org only.
- `setup.sh --wheels DIR`: offline install from a folder of wheels; on failure the script prints the
  matching `pip download` command for the Pi's architecture and Python version.
- `setup.sh --upgrade-pip`: opt-in pip self-upgrade.

### Changed
- pip runs with a 60 s timeout and 10 retries (overridable via `PIP_DEFAULT_TIMEOUT` / `PIP_RETRIES`).
- When the install fails with piwheels configured on 64-bit Pi OS, it retries automatically from pypi.org.
- The pip self-upgrade is no longer done by default; one less download over a flaky link.
- Network failures print diagnostics (PyPI/piwheels reachability, clock, proxy) instead of a generic error.

## [1.1.0] - 2026-11-11

### Added
- `setup.sh`: creates the virtual environment, installs `requirements.txt`, verifies imports and creates `.env`
  from `.env.example` with generated `SECRET_KEY`, API key, dashboard password hash and `DB_PASSWORD`.
  Options `--yes`, `--simulator`, `--dev`, `--recreate`; `PYTHON` / `VENV_DIR` overrides; safe to re-run.
- Auto-detects a single USB serial adapter and sets `SERIAL_PORT` to its `/dev/serial/by-id/` path.

### Changed
- `/api/v1/health` reports `db_ok: true` right after startup instead of waiting for the first logged row.

## [1.0.0] - 2026-11-06

### Added
- Modbus RTU client for the Despatch Protocol 3 over RS-485 (minimalmodbus), with retries, write read-back
  verification and automatic port reopen after comm loss.
- Register map (`app/registers.py`) from the Protocol 3 Modbus Communications manual (E-106), 1-decimal format.
- Background poller with live snapshot and adaptive DB logging: active / idle rates, immediate logging on
  state changes, idle hold-off and optional PV threshold.
- PostgreSQL storage: `telemetry` partitioned by day, `events` audit table; schema and partitions created automatically.
- Disk-pressure purge (drop oldest daily partitions above 80 % until 75 %), optional retention in days,
  manual trigger via API.
- REST API v1 with API-key auth (`X-API-Key` / Bearer), named keys recorded in the audit log, failed-attempt throttling.
- Control endpoints: mode, setpoint (clamped to controller and `.env` limits), manual, timer, profile, relays,
  high limit (disabled by default).
- Web dashboard with session login, live tiles, alarm flags, trend chart with server-side downsampling,
  controls, system/disk panel and recent events; light/dark and mobile layouts, no external CDN.
- Oven simulator (`SERIAL_PORT=SIMULATOR`) for development without hardware.
- Helper CLI: `gen-secret`, `gen-key`, `hash-password`, `test-connection`.
- Deployment: systemd unit (single gunicorn worker), `install.sh`, `setup_db.sql`, `.env.example`.
- Unit tests for the purge policy.
