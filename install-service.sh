#!/bin/bash
set -e

SCRIPT_DIR=$(readlink -f "$(dirname "$0")")
SERVICE_NAME="battery-tray"
SERVICE_DIR="${HOME}/.config/systemd/user"
SERVICE_FILE="${SERVICE_DIR}/${SERVICE_NAME}.service"

case "${1}" in
    uninstall)
        systemctl --user disable --now "${SERVICE_NAME}" 2>/dev/null || true
        rm -f "${SERVICE_FILE}"
        systemctl --user daemon-reload
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

echo "Installing ${SERVICE_NAME} service for user: ${USER}"
echo "App directory: ${SCRIPT_DIR}"

if ! python3 -c "import PyQt5" 2>/dev/null; then
    echo "Installing python3-pyqt5 via apt..."
    apt-get install -y python3-pyqt5
fi

echo "Setting up virtual environment..."
python3 -m venv --system-site-packages "${SCRIPT_DIR}/.venv"
"${SCRIPT_DIR}/.venv/bin/pip" install --quiet -r "${SCRIPT_DIR}/requirements.txt"
echo "Virtual environment ready."

mkdir -p "${SERVICE_DIR}"

sed "s|INSTALL_DIR|${SCRIPT_DIR}|g" "${SCRIPT_DIR}/battery-tray.service" > "${SERVICE_FILE}"

systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"

echo ""
echo "Done. Check status with:"
echo "  systemctl --user status ${SERVICE_NAME}"
echo "  journalctl --user -u ${SERVICE_NAME} -f"
