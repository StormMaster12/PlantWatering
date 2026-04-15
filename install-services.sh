#!/bin/bash
# install-services.sh
# Clones the plant watering repo from GitHub and installs both systemd services.
# Usage: sudo bash install-services.sh

set -e

# ════════════════════════════════════════════════════════════════════
#  CONFIGURATION — edit before running
# ════════════════════════════════════════════════════════════════════

GITHUB_REPO="https://github.com/StormMaster12/PlantWatering.git"  # ← change this
GITHUB_BRANCH="main"

APP_DIR="/home/pi/plant-watering"
APP_USER="pi"
SERVICE_DIR="/etc/systemd/system"

# Email credentials — written into the service file at install time.
# Leave blank to disable email alerts.
EMAIL_FROM=""
EMAIL_TO=""
EMAIL_PASSWORD=""
EMAIL_HOST="smtp.gmail.com"
EMAIL_PORT="587"

# Anthropic API key — required for plant identification stretch goal
# Get yours at console.anthropic.com
ANTHROPIC_API_KEY=""

# ════════════════════════════════════════════════════════════════════
#  PREFLIGHT CHECKS
# ════════════════════════════════════════════════════════════════════

if [[ $EUID -ne 0 ]]; then
  echo "Error: run this script with sudo" >&2
  exit 1
fi

if [[ "$GITHUB_REPO" == *"YOUR_USERNAME"* ]]; then
  echo "Error: set GITHUB_REPO at the top of this script before running" >&2
  exit 1
fi

# ════════════════════════════════════════════════════════════════════
#  GIT CLONE / PULL
# ════════════════════════════════════════════════════════════════════

if command -v git &>/dev/null; then
  echo "── git found: $(git --version)"
else
  echo "── Installing git"
  apt-get install -y git
fi

if [ -d "$APP_DIR/.git" ]; then
  echo "── Repo already cloned — pulling latest from $GITHUB_BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR" pull origin "$GITHUB_BRANCH"
else
  echo "── Cloning $GITHUB_REPO into $APP_DIR"
  sudo -u "$APP_USER" git clone --branch "$GITHUB_BRANCH" "$GITHUB_REPO" "$APP_DIR"
fi

# ════════════════════════════════════════════════════════════════════
#  PYTHON DEPENDENCIES
# ════════════════════════════════════════════════════════════════════

echo "── Installing Python dependencies"
pip3 install flask gpiozero lgpio anthropic picamera2 --break-system-packages

# Allow the 'pi' user to restart the watering service without a password
# (needed for config_manager.reload_watering_service())
SUDOERS_LINE="pi ALL=(ALL) NOPASSWD: /bin/systemctl restart plant-watering.service"
SUDOERS_FILE="/etc/sudoers.d/plant-watering"
if ! grep -qF "$SUDOERS_LINE" "$SUDOERS_FILE" 2>/dev/null; then
  echo "$SUDOERS_LINE" > "$SUDOERS_FILE"
  chmod 440 "$SUDOERS_FILE"
  echo "── sudoers rule added for service restart"
fi

# ════════════════════════════════════════════════════════════════════
#  WRITE SERVICE FILES  (generated from variables above)
# ════════════════════════════════════════════════════════════════════

echo "── Writing plant-log-server.service"
cat > "$SERVICE_DIR/plant-log-server.service" <<EOF
[Unit]
Description=Plant Watering — Log Server & Dashboard
After=network.target
Before=plant-watering.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}

Environment="EMAIL_FROM=${EMAIL_FROM}"
Environment="EMAIL_TO=${EMAIL_TO}"
Environment="EMAIL_PASSWORD=${EMAIL_PASSWORD}"
Environment="EMAIL_HOST=${EMAIL_HOST}"
Environment="EMAIL_PORT=${EMAIL_PORT}"
Environment="ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"

ExecStart=/usr/bin/python3 ${APP_DIR}/log_server.py
Restart=on-failure
RestartSec=10
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

echo "── Writing plant-watering.service"
cat > "$SERVICE_DIR/plant-watering.service" <<EOF
[Unit]
Description=Plant Watering — Sensor & Pump Controller
After=network.target plant-log-server.service
Requires=plant-log-server.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}

ExecStart=/usr/bin/python3 ${APP_DIR}/plant_watering.py
Restart=on-failure
RestartSec=15
TimeoutStartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# ════════════════════════════════════════════════════════════════════
#  ENABLE & START
# ════════════════════════════════════════════════════════════════════

echo "── Reloading systemd"
systemctl daemon-reload

echo "── Enabling services (start on boot)"
systemctl enable plant-log-server.service
systemctl enable plant-watering.service

echo "── Starting services"
systemctl restart plant-log-server.service
sleep 3
systemctl restart plant-watering.service

# ════════════════════════════════════════════════════════════════════
#  SUMMARY
# ════════════════════════════════════════════════════════════════════

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║           Install complete                           ║"
echo "╠══════════════════════════════════════════════════════╣"
printf  "║  Repo      : %-39s║\n" "$GITHUB_REPO"
printf  "║  Branch    : %-39s║\n" "$GITHUB_BRANCH"
printf  "║  App dir   : %-39s║\n" "$APP_DIR"
printf  "║  Dashboard : http://%-33s║\n" "$(hostname -I | awk '{print $1}'):5000"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Useful commands:                                    ║"
echo "║    journalctl -fu plant-watering                     ║"
echo "║    journalctl -fu plant-log-server                   ║"
echo "║    sudo systemctl restart plant-watering             ║"
echo "║    sudo bash install-services.sh   <- to update      ║"
echo "╚══════════════════════════════════════════════════════╝"
