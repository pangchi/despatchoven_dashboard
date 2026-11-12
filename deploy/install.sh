#!/usr/bin/env bash
# Install on Raspberry Pi OS (Bookworm). Run from the project folder: sudo ./deploy/install.sh
set -euo pipefail
APP=/opt/protocol3-monitor
apt-get update
apt-get install -y python3-venv postgresql
id p3mon >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin p3mon
usermod -aG dialout p3mon
mkdir -p "$APP"
rsync -a --exclude venv --exclude .env --exclude __pycache__ ./ "$APP"/
python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
if [ ! -f "$APP/.env" ]; then
  cp "$APP/.env.example" "$APP/.env"
  echo "Created $APP/.env - edit it before starting the service."
fi
chown -R p3mon:p3mon "$APP"
chmod 600 "$APP/.env"
cp "$APP/deploy/protocol3-monitor.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable protocol3-monitor
echo "Next: create the DB (sudo -u postgres psql -f $APP/deploy/setup_db.sql), edit $APP/.env, then: sudo systemctl start protocol3-monitor"
