#!/bin/bash
set -e

SCRIPT_DIR=$(readlink -f "$(dirname "$0")")
SERVICE_NAME="battery-tray"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

case "${1}" in
    uninstall)
        systemctl disable --now "${SERVICE_NAME}" 2>/dev/null || true
        rm -f "${SERVICE_FILE}"
        systemctl daemon-reload
        echo "Service removed."
        exit 0
        ;;
    ""|install)
        ;;
    *)
        echo "Usage: $0 [install|uninstall]"
        exit 1
        ;;
esac

# Remove legacy user service if present
if systemctl --user is-active --quiet battery-tray 2>/dev/null; then
    systemctl --user disable --now battery-tray 2>/dev/null || true
fi
rm -f "${HOME}/.config/systemd/user/battery-tray.service"
systemctl --user daemon-reload 2>/dev/null || true

if ! python3 -c "import PyQt5" 2>/dev/null; then
    echo "Installing python3-pyqt5 via apt..."
    apt-get install -y python3-pyqt5
fi

echo "Setting up virtual environment..."
python3 -m venv --system-site-packages "${SCRIPT_DIR}/.venv"
"${SCRIPT_DIR}/.venv/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
echo "Virtual environment ready."

echo "Installing ${SERVICE_NAME} as system service"
echo "App directory: ${SCRIPT_DIR}"

sed "s|INSTALL_DIR|${SCRIPT_DIR}|g" "${SCRIPT_DIR}/battery-tray.service" > "${SERVICE_FILE}"

systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo ""
echo "Done. Check status with:"
echo "  systemctl status ${SERVICE_NAME}"
echo "  journalctl -u ${SERVICE_NAME} -f"
