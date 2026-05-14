#!/usr/bin/env bash
# Battery Monitor install script
# Tested on: Debian 12 (Bookworm) ARM64, Orange Pi Zero 3
#
# Run as: bash install.sh
# Do NOT run as root — the service is a user unit.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="battery-monitor"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
PYTHON_MIN_MAJOR=3
PYTHON_MIN_MINOR=10   # slots=True in dataclass + X|Y unions require 3.10+

# ── Colours ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# ── Helpers ────────────────────────────────────────────────────────────────────
_py_ver() {
    "$1" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')" 2>/dev/null || echo "0.0.0"
}

_py_ok() {
    # Returns 0 if the given binary meets the minimum version requirement
    "$1" -c "
import sys
ok = sys.version_info >= ($PYTHON_MIN_MAJOR, $PYTHON_MIN_MINOR)
sys.exit(0 if ok else 1)
" 2>/dev/null
}

_debian_codename() {
    # shellcheck source=/dev/null
    . /etc/os-release 2>/dev/null && echo "${VERSION_CODENAME:-unknown}" || echo "unknown"
}

# ── Step 0: Python version check and installation ──────────────────────────────
info "Checking Python version (need ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+)…"

PYTHON=""
PYQT_USE_APT=1   # prefer apt PyQt6; set to 0 when using pyenv/custom Python

# Probe candidates in preference order (most specific first)
for candidate in python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null && _py_ok "$candidate"; then
        PYTHON="$candidate"
        break
    fi
done

if [ -n "$PYTHON" ]; then
    info "  Found $PYTHON — $(_py_ver "$PYTHON") ✓"
else
    warn "  No Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+ found (system has: $(_py_ver python3))"
    CODENAME=$(_debian_codename)
    info "  Detected Debian: $CODENAME"

    case "$CODENAME" in
        bookworm|trixie|sid)
            info "  Installing python3.11 from apt…"
            sudo apt-get update -qq
            sudo apt-get install -y python3.11 python3.11-venv python3.11-dev 2>/dev/null
            ;;
        bullseye)
            info "  Adding bullseye-backports and installing python3.11…"
            BACKPORTS_LIST='/etc/apt/sources.list.d/bullseye-backports.list'
            if [ ! -f "$BACKPORTS_LIST" ]; then
                echo "deb http://deb.debian.org/debian bullseye-backports main" \
                    | sudo tee "$BACKPORTS_LIST" > /dev/null
            fi
            sudo apt-get update -qq
            sudo apt-get install -y -t bullseye-backports python3.11 python3.11-venv python3.11-dev 2>/dev/null
            ;;
        *)
            warn "  Unknown Debian version '$CODENAME' — cannot install via apt."
            ;;
    esac

    # Re-probe after apt install
    for candidate in python3.11 python3.10 python3; do
        if command -v "$candidate" &>/dev/null && _py_ok "$candidate"; then
            PYTHON="$candidate"
            break
        fi
    done

    # ── Fallback: pyenv ────────────────────────────────────────────────────────
    if [ -z "$PYTHON" ]; then
        warn "  apt install did not produce a working Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+."
        info "  Falling back to pyenv…"

        if ! command -v pyenv &>/dev/null; then
            info "  Installing pyenv…"
            # Install build dependencies for CPython
            sudo apt-get install -y \
                build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
                libsqlite3-dev libncursesw5-dev xz-utils tk-dev libxml2-dev \
                libxmlsec1-dev libffi-dev liblzma-dev curl git 2>/dev/null

            curl -fsSL https://pyenv.run | bash

            # Activate pyenv for this session
            export PYENV_ROOT="$HOME/.pyenv"
            export PATH="$PYENV_ROOT/bin:$PATH"
            eval "$(pyenv init -)"

            # Add to shell profile for future sessions
            PROFILE="$HOME/.bashrc"
            if ! grep -q 'PYENV_ROOT' "$PROFILE" 2>/dev/null; then
                cat >> "$PROFILE" <<'EOF'

# pyenv
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$PATH"
eval "$(pyenv init -)"
EOF
            fi
        else
            export PYENV_ROOT="${PYENV_ROOT:-$HOME/.pyenv}"
            export PATH="$PYENV_ROOT/bin:$PATH"
            eval "$(pyenv init -)"
        fi

        PYENV_PYTHON="3.11.9"
        info "  Installing Python $PYENV_PYTHON via pyenv (this takes a few minutes on ARM64)…"
        pyenv install -s "$PYENV_PYTHON"
        pyenv local "$PYENV_PYTHON"

        PYTHON="$(pyenv which python3)"

        # When using pyenv Python, apt python3-pyqt6 is incompatible —
        # use pip instead. PyQt6 6.6+ has manylinux_2_28_aarch64 wheels on PyPI.
        PYQT_USE_APT=0
        warn "  Using pyenv Python — PyQt6 will be installed via pip (not apt)."
    fi
fi

if [ -z "$PYTHON" ]; then
    error "Could not find or install Python ${PYTHON_MIN_MAJOR}.${PYTHON_MIN_MINOR}+. Aborting."
fi

info "Using: $PYTHON ($(_py_ver "$PYTHON"))"

# ── Step 1: System packages ────────────────────────────────────────────────────
info "Installing system dependencies…"
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    i2c-tools \
    libxcb-xinerama0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxkbcommon-x11-0 \
    libgl1 \
    libdbus-1-3 \
    libnotify-bin \
    2>/dev/null

# ── Step 2: I2C permissions ────────────────────────────────────────────────────
info "Configuring I2C access…"

if ! groups | grep -q '\bi2c\b'; then
    sudo usermod -aG i2c "$USER"
    warn "Added $USER to i2c group — log out and back in, or run: newgrp i2c"
fi

UDEV_RULE='/etc/udev/rules.d/99-i2c.rules'
if [ ! -f "$UDEV_RULE" ]; then
    echo 'SUBSYSTEM=="i2c-dev", GROUP="i2c", MODE="0660"' | sudo tee "$UDEV_RULE" > /dev/null
    sudo udevadm control --reload-rules
    sudo udevadm trigger
    info "Created udev rule: $UDEV_RULE"
fi

# ── Step 3: Enable I2C on Orange Pi Zero 3 ────────────────────────────────────
info "Checking I2C device availability…"
if [ -e /dev/i2c-3 ]; then
    info "  /dev/i2c-3 found — I2C bus 3 is available"
else
    warn "  /dev/i2c-3 NOT found."
    warn "  To enable I2C bus 3, add 'i2c3' to OVERLAYS in:"
    warn "    /boot/armbianEnv.txt   (Armbian-based Debian)"
    warn "    /boot/orangepiEnv.txt  (official OrangePi Debian)"
    warn "  Example line:  overlays=i2c3"
    warn "  Then reboot."
fi

# ── Step 4: Verify INA219 presence ────────────────────────────────────────────
info "Probing I2C buses for INA219 at 0x43…"
if command -v i2cdetect &>/dev/null; then
    _found_bus=""
    for _bus_dev in /dev/i2c-*; do
        [ -e "$_bus_dev" ] || continue
        _bus_num="${_bus_dev##*-}"
        # $5 in the "40:" row is 0x43; driver-claimed devices show "UU" instead of "43"
        _i2c_cell=$(i2cdetect -y "$_bus_num" 2>/dev/null | awk '/^40:/{print $5}')
        if [[ "$_i2c_cell" == "43" || "$_i2c_cell" == "UU" ]]; then
            _found_bus="$_bus_num"
            break
        fi
    done
    if [ -n "$_found_bus" ]; then
        info "  INA219 detected at 0x43 on i2c-${_found_bus} ✓"
    else
        warn "  INA219 not found at 0x43 on any I2C bus. Check wiring and HAT seating."
        warn "  Available buses: $(ls /dev/i2c-* 2>/dev/null | tr '\n' ' ')"
        warn "  Run manually: i2cdetect -y <bus_number>"
    fi
else
    warn "  Cannot probe I2C (i2cdetect not installed — run: sudo apt-get install i2c-tools)"
fi

# ── Step 5: Qt and heavy deps ─────────────────────────────────────────────────
PYQTGRAPH_FROM_APT=0

if [ "$PYQT_USE_APT" -eq 1 ]; then
    # System Python → apt PyQt6 (no ARM64 wheel pain)
    info "Installing PyQt6 and pyqtgraph via apt…"
    sudo apt-get install -y \
        python3-pyqt6 \
        python3-pyqt6.sip \
        python3-yaml \
        2>/dev/null

    if apt-cache show python3-pyqtgraph &>/dev/null 2>&1; then
        sudo apt-get install -y python3-pyqtgraph 2>/dev/null
        PYQTGRAPH_FROM_APT=1
    fi
else
    # pyenv Python → pip PyQt6 (wheels exist for aarch64 in PyQt6 6.6+)
    info "Will install PyQt6 via pip (pyenv Python — apt package incompatible)…"
    sudo apt-get install -y python3-yaml 2>/dev/null || true   # best-effort
fi

# ── Step 6: Python virtual environment ────────────────────────────────────────
info "Creating Python virtual environment…"
VENV_DIR="$SCRIPT_DIR/venv"

if [ "$PYQT_USE_APT" -eq 1 ]; then
    # --system-site-packages: inherit apt-installed PyQt6/pyqtgraph/PyYAML
    "$PYTHON" -m venv "$VENV_DIR" --system-site-packages
else
    # Clean venv — everything goes through pip
    "$PYTHON" -m venv "$VENV_DIR"
fi

info "Installing Python dependencies via pip…"
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install smbus2 prometheus-client --quiet

if [ "$PYQT_USE_APT" -eq 0 ]; then
    info "Installing PyQt6 via pip (this may take a moment on ARM64)…"
    # Minimum 6.6.0 — first version with manylinux_2_28_aarch64 wheels
    "$VENV_DIR/bin/pip" install "PyQt6>=6.6.0" PyYAML --quiet
fi

if [ "$PYQTGRAPH_FROM_APT" -eq 0 ]; then
    info "Installing pyqtgraph via pip…"
    "$VENV_DIR/bin/pip" install pyqtgraph --quiet
fi

# ── Step 7: Systemd user service ──────────────────────────────────────────────
info "Installing systemd user service…"
mkdir -p "$SYSTEMD_USER_DIR"

# Patch the service file: replace placeholder Python with venv Python
VENV_PYTHON="$VENV_DIR/bin/python3"
sed "s|/usr/bin/python3|$VENV_PYTHON|g; s|%h|$HOME|g" \
    "$SCRIPT_DIR/systemd/$SERVICE_NAME.service" \
    > "$SYSTEMD_USER_DIR/$SERVICE_NAME.service"

systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME.service"

loginctl enable-linger "$USER" 2>/dev/null \
    || warn "loginctl enable-linger failed — service may not auto-start at boot"

# ── Step 8: Desktop tray hint ─────────────────────────────────────────────────
if command -v gnome-extensions &>/dev/null; then
    if ! gnome-extensions list --enabled 2>/dev/null | grep -qi appindicator; then
        warn "GNOME AppIndicator extension not enabled — tray icon may be hidden."
        warn "Fix: sudo apt install gnome-shell-extension-appindicator"
        warn "     gnome-extensions enable appindicatorsupport@rgcjonas.gmail.com"
    else
        info "GNOME AppIndicator extension active ✓"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Installation complete."
echo ""
echo "  Python:       $PYTHON ($(_py_ver "$PYTHON"))"
echo "  Venv:         $VENV_DIR"
echo "  PyQt6 source: $([ "$PYQT_USE_APT" -eq 1 ] && echo apt || echo pip)"
echo ""
echo "  Start now:    systemctl --user start $SERVICE_NAME"
echo "  View logs:    journalctl --user -u $SERVICE_NAME -f"
echo "  Run manually: $VENV_DIR/bin/python3 $SCRIPT_DIR/app.py"
echo "  Debug:        $VENV_DIR/bin/python3 $SCRIPT_DIR/app.py --verbose"
echo "  Metrics:      curl http://127.0.0.1:9105/metrics"
echo ""
if ! groups | grep -q '\bi2c\b'; then
    warn "IMPORTANT: Log out and back in to apply I2C group membership."
fi
