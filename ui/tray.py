"""
System tray icon with dynamic battery state display.

Ubuntu / GNOME note
-------------------
GNOME 40+ hides legacy tray icons by default.  Install the extension:
  gnome-shell-extension-appindicator  (available in Ubuntu repos)
or
  gnome-extensions install ubuntu-appindicators@ubuntu.com

The icon itself is rendered via QSystemTrayIcon which maps to the
StatusNotifierItem DBus protocol on modern desktops.
"""

import logging
from typing import Optional

from PyQt6.QtCore import pyqtSignal, pyqtSlot, QObject
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from core.battery import BatteryState, ChargeState
from ui.icons import battery_icon

logger = logging.getLogger(__name__)


class TrayIcon(QObject):
    """
    Manages the system tray icon lifecycle.

    Signals
    -------
    show_window_requested() — user clicked "Show" in the menu
    quit_requested()        — user clicked "Quit" in the menu
    """

    show_window_requested = pyqtSignal()
    quit_requested        = pyqtSignal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)

        self._tray = QSystemTrayIcon(parent)
        self._tray.setIcon(battery_icon(0.0, False, error=True))
        self._tray.setToolTip("Battery Monitor — initialising…")

        menu = QMenu()
        self._show_action = menu.addAction("Show Window")
        menu.addSeparator()
        self._quit_action = menu.addAction("Quit")

        self._show_action.triggered.connect(self.show_window_requested)
        self._quit_action.triggered.connect(self.quit_requested)
        self._tray.activated.connect(self._on_activated)
        self._tray.setContextMenu(menu)
        self._tray.show()

        self._last_pct:      float = 0.0
        self._last_charging: bool  = False
        self._last_error:    bool  = True

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(object)
    def update_state(self, state: BatteryState) -> None:
        if not state.is_valid:
            self._set_icon(0.0, False, error=True)
            self._tray.setToolTip("Battery Monitor — sensor error")
            return

        pct      = state.percentage
        charging = state.charge_state == ChargeState.CHARGING
        self._set_icon(pct, charging, error=False)
        self._tray.setToolTip(self._build_tooltip(state))

    @pyqtSlot(str, str, str)
    def show_notification(self, title: str, body: str, urgency: str) -> None:
        icon_map = {
            "low":      QSystemTrayIcon.MessageIcon.Warning,
            "normal":   QSystemTrayIcon.MessageIcon.Information,
            "critical": QSystemTrayIcon.MessageIcon.Critical,
        }
        qt_icon = icon_map.get(urgency, QSystemTrayIcon.MessageIcon.Information)
        duration_ms = 8000 if urgency == "critical" else 5000
        self._tray.showMessage(title, body, qt_icon, duration_ms)

    # ── Private ───────────────────────────────────────────────────────────────

    def _set_icon(self, pct: float, charging: bool, error: bool) -> None:
        # Avoid re-rendering if state hasn't changed meaningfully
        pct_bucket = round(pct / 5) * 5   # quantise to 5 % steps
        if (
            pct_bucket == round(self._last_pct / 5) * 5
            and charging == self._last_charging
            and error == self._last_error
        ):
            return
        self._last_pct      = pct
        self._last_charging = charging
        self._last_error    = error
        self._tray.setIcon(battery_icon(float(pct_bucket), charging, error))

    @staticmethod
    def _build_tooltip(state: BatteryState) -> str:
        lines = [
            f"Battery: {state.percentage:.0f} %",
            f"Voltage: {state.voltage_v:.3f} V",
            f"Current: {state.current_ma:+.1f} mA",
            f"Power:   {state.power_mw:.1f} mW",
            f"State:   {state.charge_state.value}",
        ]
        if state.estimated_runtime_min is not None:
            h, m = divmod(int(state.estimated_runtime_min), 60)
            runtime_str = f"{h}h {m:02d}m" if h else f"{m} min"
            lines.append(f"Runtime: {runtime_str}")
        return "\n".join(lines)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_window_requested.emit()
