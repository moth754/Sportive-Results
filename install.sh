#!/usr/bin/env bash
# Race Results installer for Raspberry Pi OS, Debian and Ubuntu.
# Safe to re-run at any time: it updates the Python packages and the service.
#
#   ./install.sh               install, and start automatically at boot on port 8080
#   ./install.sh --port 8090   use a different port
#   ./install.sh --no-service  only set up Python; start it yourself with ./run.sh
#   ./install.sh --uninstall   remove the service and power-off permission (keeps data/)
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="raceresults"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SUDOERS_FILE="/etc/sudoers.d/${SERVICE_NAME}"
PORT=8080
WITH_SERVICE=1
UNINSTALL=0

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; }
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

while [ $# -gt 0 ]; do
    case "$1" in
        --port) PORT="${2:-}"; shift 2 ;;
        --port=*) PORT="${1#*=}"; shift ;;
        --no-service) WITH_SERVICE=0; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1"; usage; exit 1 ;;
    esac
done

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
    echo "The port must be a number from 1024 to 65535."
    exit 1
fi
if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal user, not with sudo. It asks for your password when it needs it."
    exit 1
fi
RUN_USER="$(id -un)"
RUN_GROUP="$(id -gn)"

if [ "$UNINSTALL" -eq 1 ]; then
    say "Removing the $SERVICE_NAME service and the power-off permission"
    sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
    sudo rm -f "$SERVICE_FILE" "$SUDOERS_FILE"
    sudo systemctl daemon-reload
    echo "Done. Your settings and results are still in $APP_DIR/data."
    exit 0
fi

say "Checking Python"
if ! command -v python3 >/dev/null; then
    echo "python3 isn't installed. Install it with:  sudo apt install python3 python3-venv"
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    echo "Python 3.9 or newer is needed (this computer has $(python3 --version))."
    exit 1
fi
if ! python3 -c 'import venv, ensurepip' 2>/dev/null; then
    echo "Python's venv module is missing, so installing python3-venv (asks for your password)."
    sudo apt-get update
    sudo apt-get install -y python3-venv
fi

say "Installing Python packages into $APP_DIR/.venv (a few minutes on a Raspberry Pi)"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$APP_DIR/.venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

say "Preparing the data folder"
mkdir -p "$APP_DIR/data"
chmod 700 "$APP_DIR/data"
if [ -f "$APP_DIR/data/raceresults.db" ]; then
    say "Backing up the database (updates can change its layout)"
    mkdir -p "$APP_DIR/data/backups"
    "$APP_DIR/.venv/bin/python" - "$APP_DIR/data/raceresults.db" "$APP_DIR/data/backups" <<'PYEOF'
import pathlib, sqlite3, sys, time
source, folder = sys.argv[1], pathlib.Path(sys.argv[2])
target = folder / time.strftime("raceresults-%Y%m%d-%H%M%S.db")
src, dst = sqlite3.connect(source), sqlite3.connect(target)
src.backup(dst)  # safe while the app is running
src.close()
dst.close()
target.chmod(0o600)
for old in sorted(folder.glob("raceresults-*.db"))[:-10]:  # keep the last ten
    old.unlink()
print(f"Saved {target}")
PYEOF
fi
echo "Settings, passwords and results are kept in $APP_DIR/data, which is never added to git."

if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" config core.hooksPath .githooks
    chmod +x "$APP_DIR/.githooks/"*
    echo "Git safety check on: commits that include secrets or data/ are refused."
fi
chmod +x "$APP_DIR/run.sh"

if [ "$WITH_SERVICE" -eq 0 ]; then
    say "Done"
    echo "Start the app with:  $APP_DIR/run.sh --port $PORT"
    exit 0
fi

if ! command -v systemctl >/dev/null; then
    echo "This computer doesn't use systemd. Start the app with $APP_DIR/run.sh instead."
    exit 1
fi
SYSTEMCTL="$(command -v systemctl)"

say "Installing the $SERVICE_NAME service so it starts at boot (asks for your password)"
tmp="$(mktemp)"
sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@USER@|$RUN_USER|g" -e "s|@GROUP@|$RUN_GROUP|g" \
    -e "s|@PORT@|$PORT|g" -e "s|@SYSTEMCTL@|$SYSTEMCTL|g" \
    "$APP_DIR/deploy/raceresults.service" > "$tmp"
sudo install -m 0644 "$tmp" "$SERVICE_FILE"
rm -f "$tmp"

say "Letting the Shutdown button power this computer off"
tmp="$(mktemp)"
echo "$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL poweroff" > "$tmp"
if sudo visudo -cf "$tmp" >/dev/null; then
    sudo install -m 0440 -o root -g root "$tmp" "$SUDOERS_FILE"
    echo "Added $SUDOERS_FILE (allows only: $SYSTEMCTL poweroff)."
else
    echo "Couldn't add the sudo rule; the Shutdown button won't be able to power off."
fi
rm -f "$tmp"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null
sudo systemctl restart "$SERVICE_NAME"

say "Starting"
started=0
for _ in $(seq 1 60); do
    if "$APP_DIR/.venv/bin/python" -c "import sys, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/static/app.css' % sys.argv[1], timeout=2)" "$PORT" 2>/dev/null; then
        started=1
        break
    fi
    sleep 1
done
if [ "$started" -eq 0 ]; then
    echo "The app hasn't answered yet. Check it with:"
    echo "  systemctl status $SERVICE_NAME"
    echo "  journalctl -u $SERVICE_NAME -n 50"
    exit 1
fi

echo "Race Results is running and will start automatically after a reboot."
echo "Open it in a browser on the same network:"
echo "  http://$(hostname).local:$PORT"
for ip in $(hostname -I 2>/dev/null); do
    case "$ip" in *:*) ;; *) echo "  http://$ip:$PORT" ;; esac
done
echo
echo "Logs:    journalctl -u $SERVICE_NAME -f"
echo "Update:  git pull && ./install.sh"
