#!/usr/bin/env bash
# Provision an Oracle Cloud Always Free Ampere A1 instance to run the bot.
# Tested target: Ubuntu 22.04/24.04 aarch64, 2 OCPU / 12 GB.
#
#   scp -r trading-bot ubuntu@<ip>:~/
#   ssh ubuntu@<ip> 'bash ~/trading-bot/deploy/setup.sh'
#
# Pass --venv to install into /opt/trading-bot/.venv instead of system-wide.
# You almost certainly do not need it: the bot has two dependencies and both
# ship as Ubuntu packages. If you do use it, it MUST live under /opt -- the
# service runs with ProtectHome=true, so a venv anywhere under /home is
# invisible to it and the unit fails to start after testing fine by hand.

set -euo pipefail

APP_DIR=/opt/trading-bot
SERVICE_USER=trader
USE_VENV=0
SKIP_FIREWALL=0
for arg in "$@"; do
  case "$arg" in
    --venv)        USE_VENV=1 ;;
    --no-firewall) SKIP_FIREWALL=1 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Architecture and OS"
uname -m; . /etc/os-release && echo "$PRETTY_NAME"

say "Base packages"
sudo apt-get update -qq
# python3 and PyYAML come from apt so there is no pip build step on ARM.
sudo apt-get install -y -qq python3 python3-yaml python3-pip chrony ufw

say "Time synchronisation (signed requests fail on clock drift)"
sudo systemctl enable --now chrony
sleep 2
chronyc tracking | grep -E 'System time|Leap status' || true

if [ "$USE_VENV" = "1" ]; then
  say "Virtual environment at $APP_DIR/.venv (opt-in)"
  sudo apt-get install -y -qq python3-venv
  sudo mkdir -p "$APP_DIR"
  sudo /usr/bin/python3 -m venv "$APP_DIR/.venv"
  sudo "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
  sudo "$APP_DIR/.venv/bin/pip" install --quiet -r "$(cd "$(dirname "$0")/.." && pwd)/requirements.txt"
  PYTHON="$APP_DIR/.venv/bin/python"
  echo "using $PYTHON"
else
  PYTHON=/usr/bin/python3
fi

say "Python websockets (required -- realtime is on by default)"
# Must be readable by the service, which runs with ProtectHome=true: a
# `pip install --user` as $SERVICE_USER lands in /home/trader/.local, which is
# invisible inside the unit's namespace. The install would succeed, netcheck
# would pass, and the service would then fail to import it. So: system-wide
# only, and verified afterwards from inside the same context. (QA F14)
if [ "$USE_VENV" = "0" ]; then
  sudo apt-get install -y -qq python3-websockets 2>/dev/null || \
    sudo /usr/bin/python3 -m pip install --break-system-packages websockets
fi

if ! sudo "$PYTHON" -c 'import websockets' 2>/dev/null; then
  echo "FATAL: websockets is not importable system-wide. The bot cannot start."
  echo "Install it and re-run:  sudo /usr/bin/python3 -m pip install --break-system-packages websockets"
  exit 1
fi
echo "websockets $(sudo "$PYTHON" -c 'import websockets;print(websockets.__version__)') OK"

say "Service user and directories"
id -u "$SERVICE_USER" &>/dev/null || sudo useradd --system --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
sudo mkdir -p "$APP_DIR"
sudo cp -r "$(cd "$(dirname "$0")/.." && pwd)"/. "$APP_DIR"/
sudo mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

# .env holds API keys. Seed it from the template if absent so it always exists
# with the right owner and mode -- a world-readable .env on a public-IP VPS is
# the single worst mistake available here.
if [ ! -f "$APP_DIR/.env" ]; then
  sudo -u "$SERVICE_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "created $APP_DIR/.env from the template -- fill it in before starting"
fi
sudo chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
sudo chmod 600 "$APP_DIR/.env"
echo ".env: $(sudo stat -c '%U:%G %a' "$APP_DIR/.env")"

if [ "$SKIP_FIREWALL" = "1" ]; then
  say "Firewall: skipped (--no-firewall)"
else
  say "Firewall"
  # NEVER reset. This box may already be serving websites, and `ufw reset`
  # silently drops every rule that keeps them reachable. Only ever add.
  if sudo ufw status | head -1 | grep -q "Status: active"; then
    echo "ufw is already active -- leaving your existing rules alone."
    echo "Ensuring SSH stays allowed, and nothing else is touched:"
    sudo ufw allow OpenSSH >/dev/null 2>&1 || true
  else
    echo "ufw is inactive. Setting a deny-inbound default and allowing SSH."
    echo "If you later run websites here, you must open their ports yourself:"
    echo "    sudo ufw allow 80/tcp && sudo ufw allow 443/tcp"
    sudo ufw default deny incoming >/dev/null
    sudo ufw default allow outgoing >/dev/null
    sudo ufw allow OpenSSH >/dev/null
    sudo ufw --force enable >/dev/null
  fi
  sudo ufw status verbose | head -12
  echo
  echo "The bot itself needs NO inbound ports -- every connection it makes is"
  echo "outbound, and the dashboard binds to 127.0.0.1 only."
fi

say "Verifying imports as the service user"
sudo -u "$SERVICE_USER" "$PYTHON" -c \
  "import yaml, websockets; print('yaml + websockets importable as $SERVICE_USER')"

say "Endpoint check from THIS host"
sudo -u "$SERVICE_USER" "$PYTHON" "$APP_DIR/run.py" netcheck || true

say "Installing the service"
sudo cp "$APP_DIR/deploy/trading-bot.service" /etc/systemd/system/
if [ "$USE_VENV" = "1" ]; then
  # The unit hardcodes /usr/bin/python3; point it at the venv instead. Missing
  # this step is how a venv deployment fails only once systemd starts it.
  sudo sed -i "s|^ExecStart=.*|ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/run.py trade|" \
    /etc/systemd/system/trading-bot.service
  echo "ExecStart -> $(grep ^ExecStart= /etc/systemd/system/trading-bot.service)"
fi
sudo systemctl daemon-reload
sudo systemctl enable trading-bot

cat <<'DONE'

Setup complete. Before starting:

  1. sudo -u trader nano /opt/trading-bot/.env      # API keys, Telegram, SMTP
  2. sudo -u trader /usr/bin/python3 /opt/trading-bot/run.py alerts
                                                    # confirm phone + email work
  3. sudo -u trader /usr/bin/python3 /opt/trading-bot/run.py doctor
  4. sudo systemctl start trading-bot
     journalctl -u trading-bot -f

To stop it right now, from anywhere:
     sudo -u trader touch /opt/trading-bot/KILL

DONE
