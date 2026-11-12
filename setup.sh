#!/usr/bin/env bash
# Create the Python virtual environment, install requirements.txt and create .env.
#
#   ./setup.sh                 interactive: asks for the dashboard password
#   ./setup.sh --yes           non-interactive: generates every secret and prints them once
#   ./setup.sh --simulator     .env uses SERIAL_PORT=SIMULATOR (no hardware)
#   ./setup.sh --dev           also install pytest
#   ./setup.sh --recreate      delete and rebuild the venv
#   ./setup.sh --pypi-only     ignore piwheels / pip.conf, download from pypi.org only
#                              (automatic on 64-bit Pi OS, where piwheels is not needed)
#   ./setup.sh --piwheels      keep piwheels on 64-bit Pi OS (default on 32-bit)
#   ./setup.sh --wheels DIR    offline install from a folder of downloaded wheels
#   ./setup.sh --upgrade-pip   also upgrade pip itself (skipped by default)
#
# Environment overrides: PYTHON=python3.11  VENV_DIR=./venv  HTTPS_PROXY=http://proxy:3128
# Safe to re-run: an existing .env is never overwritten, only empty secrets are filled in.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
PROJECT_DIR=$(pwd)
PYTHON=${PYTHON:-python3}
VENV_DIR=${VENV_DIR:-$PROJECT_DIR/venv}
ARCH=${SETUP_ARCH:-$(uname -m)}   # SETUP_ARCH: testing override
ASSUME_YES=0 SIMULATOR=0 DEV=0 RECREATE=0 PYPI_ONLY=0 KEEP_PIWHEELS=0 UPGRADE_PIP=0 WHEELS_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    --simulator) SIMULATOR=1 ;;
    --dev) DEV=1 ;;
    --recreate) RECREATE=1 ;;
    --pypi-only) PYPI_ONLY=1 ;;
    --piwheels) KEEP_PIWHEELS=1 ;;
    --upgrade-pip) UPGRADE_PIP=1 ;;
    --wheels) shift; WHEELS_DIR=$(readlink -f "${1:?--wheels needs a folder}") ;;
    -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
[ -t 0 ] || ASSUME_YES=1   # no terminal (e.g. piped / CI): don't prompt

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- python check
command -v "$PYTHON" >/dev/null || die "$PYTHON not found. Install it: sudo apt install python3 python3-venv"
"$PYTHON" - <<'EOF' || die "Python 3.9 or newer is required"
import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)
EOF
"$PYTHON" -c "import venv, ensurepip" 2>/dev/null \
  || die "Python venv module missing. Install it: sudo apt install python3-venv"
say "Using $("$PYTHON" --version) at $(command -v "$PYTHON")"

# ---------------------------------------------------------------- virtualenv
if [ "$RECREATE" = 1 ] && [ -d "$VENV_DIR" ]; then
  say "Removing existing venv $VENV_DIR"
  rm -rf "$VENV_DIR"
fi
if [ ! -x "$VENV_DIR/bin/python" ]; then
  say "Creating virtual environment in $VENV_DIR"
  "$PYTHON" -m venv "$VENV_DIR"
else
  say "Virtual environment already exists ($VENV_DIR)"
fi
VPY="$VENV_DIR/bin/python"

# ---------------------------------------------------------------- pip
# Patient settings for flaky links (piwheels resets, 4G, SSL-inspecting firewalls)
export PIP_DEFAULT_TIMEOUT=${PIP_DEFAULT_TIMEOUT:-60} PIP_RETRIES=${PIP_RETRIES:-10}
export PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_PROGRESS_BAR=off

use_pypi_only() {
  # PIP_CONFIG_FILE=/dev/null makes pip ignore /etc/pip.conf (where Pi OS adds piwheels)
  export PIP_CONFIG_FILE=/dev/null PIP_INDEX_URL=https://pypi.org/simple
  unset PIP_EXTRA_INDEX_URL || true
}

pip_install() {
  if [ -n "$WHEELS_DIR" ]; then
    "$VPY" -m pip install --no-index --find-links "$WHEELS_DIR" "$@"
  else
    "$VPY" -m pip install "$@"
  fi
}

uses_piwheels() {
  [ "${PIP_CONFIG_FILE:-}" = /dev/null ] && return 1
  local cfg   # capture first: "pip | grep -q" fails under pipefail when grep exits early
  cfg=$("$VPY" -m pip config list 2>/dev/null || true)
  [[ "$cfg ${PIP_INDEX_URL:-} ${PIP_EXTRA_INDEX_URL:-}" == *piwheels* ]]
}

network_help() {
  local pyver arch plat
  pyver=$("$VPY" -c 'import sys; print(f"{sys.version_info[0]}{sys.version_info[1]}")')
  arch=$ARCH
  warn "Could not download packages. Things to check on this Pi:"
  warn "  curl -sI https://pypi.org/simple/flask/ | head -1          (expect HTTP/2 200)"
  warn "  curl -sI https://www.piwheels.org/simple/flask/ | head -1"
  warn "  date                       (a wrong clock breaks HTTPS - check NTP)"
  warn "  behind a proxy?            HTTPS_PROXY=http://proxy:port ./setup.sh"
  warn "  retry PyPI only:           ./setup.sh --pypi-only"
  if [ "$arch" = aarch64 ]; then
    plat=manylinux2014_aarch64
    warn "Offline route - on any PC with internet run:"
    warn "  pip download -r requirements.txt -d wheels --only-binary=:all: --platform $plat --python-version $pyver"
    warn "  copy the 'wheels' folder to this Pi, then:  ./setup.sh --wheels wheels"
  else
    warn "32-bit OS ($arch): PyPI has no psycopg2-binary wheel; it must come from piwheels or be compiled:"
    warn "  sudo apt install libpq-dev python3-dev build-essential"
  fi
}

if [ "$PYPI_ONLY" = 1 ]; then
  use_pypi_only
elif [ -z "$WHEELS_DIR" ] && [ "$KEEP_PIWHEELS" = 0 ] && [ "$ARCH" = aarch64 ] && uses_piwheels; then
  # Every requirement has an aarch64 wheel on PyPI; piwheels only adds a slow, flaky second source.
  say "64-bit Pi OS: skipping piwheels, using pypi.org only (--piwheels to keep it)"
  use_pypi_only
fi
[ -n "$WHEELS_DIR" ] && { [ -d "$WHEELS_DIR" ] || die "wheels folder not found: $WHEELS_DIR"; say "Offline install from $WHEELS_DIR"; }

if [ "$UPGRADE_PIP" = 1 ]; then
  say "Upgrading pip"
  pip_install -q --upgrade pip || warn "pip upgrade failed - continuing with the bundled pip"
fi

REQS=(-r requirements.txt)
[ "$DEV" = 1 ] && REQS+=(pytest)
EXTRA=""; [ "$DEV" = 1 ] && EXTRA=" + pytest"
say "Installing requirements.txt$EXTRA (timeout ${PIP_DEFAULT_TIMEOUT}s, ${PIP_RETRIES} retries)"
if ! pip_install "${REQS[@]}"; then
  if [ -z "$WHEELS_DIR" ] && uses_piwheels && [ "$ARCH" = aarch64 ]; then
    warn "Install failed with piwheels enabled - retrying from pypi.org only"
    use_pypi_only
    pip_install "${REQS[@]}" || { network_help; exit 1; }
  else
    network_help
    exit 1
  fi
fi
"$VPY" -c "import flask, minimalmodbus, psycopg2, serial, dotenv" \
  || die "Packages installed but an import failed - see the error above"

# ---------------------------------------------------------------- .env
[ -f .env.example ] || die ".env.example is missing from $PROJECT_DIR"
NEW_ENV=0
if [ ! -f .env ]; then
  say "Creating .env from .env.example"
  cp .env.example .env
  NEW_ENV=1
else
  say ".env already exists - keeping it, filling in only empty secrets"
fi
chmod 600 .env

# Dashboard password (only needed when WEB_PASSWORD_HASH is still empty)
WEB_PW="" GENERATED_PW=0
if ! grep -Eq "^WEB_PASSWORD_HASH=['\"]?[^'\" ]+" .env; then
  if [ "$ASSUME_YES" = 1 ]; then
    WEB_PW=$("$VPY" -c "import secrets; print(secrets.token_urlsafe(12))")
    GENERATED_PW=1
  else
    while :; do
      read -rsp "Dashboard password for user 'admin' (min 8 chars): " WEB_PW; echo
      read -rsp "Repeat: " pw2; echo
      [ "$WEB_PW" = "$pw2" ] || { warn "Passwords do not match"; continue; }
      [ ${#WEB_PW} -ge 8 ] || { warn "Use at least 8 characters"; continue; }
      break
    done
  fi
fi

# Single serial adapter attached? Use its stable by-id path.
SERIAL_GUESS=""
if [ "$SIMULATOR" = 1 ]; then
  SERIAL_GUESS="SIMULATOR"
elif [ "$NEW_ENV" = 1 ] && [ -d /dev/serial/by-id ]; then
  mapfile -t ports < <(ls -1 /dev/serial/by-id/ 2>/dev/null)
  [ "${#ports[@]}" -eq 1 ] && SERIAL_GUESS="/dev/serial/by-id/${ports[0]}"
fi

# Edit .env in Python: values with $ (password hashes) are unsafe in sed.
WEB_PW="$WEB_PW" SERIAL_GUESS="$SERIAL_GUESS" NEW_ENV="$NEW_ENV" "$VPY" - <<'EOF'
import os, re, secrets
from werkzeug.security import generate_password_hash

path = ".env"
lines = open(path).read().splitlines()
report = []

def get(key):
    for l in lines:
        if l.startswith(key + "="):
            return l.split("=", 1)[1].strip().strip("'\"")
    return None

def put(key, value, quote=False):
    v = f"'{value}'" if quote else value
    for i, l in enumerate(lines):
        if l.startswith(key + "="):
            lines[i] = f"{key}={v}"
            return
    lines.append(f"{key}={v}")

if not get("SECRET_KEY"):
    put("SECRET_KEY", secrets.token_hex(32)); report.append("SECRET_KEY generated")
if not get("API_KEYS"):
    key = secrets.token_urlsafe(32)
    put("API_KEYS", f"client:{key}"); report.append(f"API key generated (name 'client'): {key}")
if os.environ.get("WEB_PW"):
    put("WEB_PASSWORD_HASH", generate_password_hash(os.environ["WEB_PW"]), quote=True)
    report.append("WEB_PASSWORD_HASH set")
if os.environ.get("NEW_ENV") == "1" and not get("DB_PASSWORD"):
    put("DB_PASSWORD", secrets.token_urlsafe(18)); report.append("DB_PASSWORD generated")
if os.environ.get("SERIAL_GUESS"):
    put("SERIAL_PORT", os.environ["SERIAL_GUESS"]); report.append(f"SERIAL_PORT={os.environ['SERIAL_GUESS']}")

open(path, "w").write("\n".join(lines) + "\n")
for r in report:
    print("    " + r)
EOF

if [ "$GENERATED_PW" = 1 ]; then
  echo "    Dashboard login: admin / $WEB_PW   (shown once - change with: venv/bin/python -m app.tools hash-password)"
fi

# ---------------------------------------------------------------- next steps
DB_NAME=$(grep -E '^DB_NAME=' .env | cut -d= -f2-)
DB_USER=$(grep -E '^DB_USER=' .env | cut -d= -f2-)
cat <<EOF

Setup complete.
Next steps:
  1. Create the database user (if not done) - password must match DB_PASSWORD in .env:
       sudo -u postgres psql -c "CREATE ROLE ${DB_USER:-protocol3} LOGIN PASSWORD '<DB_PASSWORD from .env>';"
       sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME:-protocol3} OWNER ${DB_USER:-protocol3};"
  2. Review .env (SERIAL_PORT, SERIAL_BAUD, MODBUS_ADDRESS must match the controller Comms menu)
  3. Check the controller link:   venv/bin/python -m app.tools test-connection
  4. Run:                         venv/bin/python wsgi.py        (http://<pi>:8000)
     or as a service:             sudo ./deploy/install.sh
EOF
