"""
Desktop notification dispatcher with per-event cooldown (anti-spam).

Notification backend
--------------------
Primary  : QSystemTrayIcon.showMessage() — works wherever the tray icon is
           visible.  Zero extra dependencies.
Fallback : notify-send subprocess — catches cases where the tray icon exists
           but showMessage() is suppressed by the desktop environment (some
           GNOME setups).  Available on all Ubuntu installations.

Cooldown
--------
Each event kind has an independent timer.  A notification is only sent if
the last notification of the same kind was sent more than cooldown_seconds
ago.  This prevents alert storms on a flaky power supply or voltage near a
threshold.
"""

import logging
import subprocess
import time
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QSystemTrayIcon

from core.battery import BatteryState, ChargeState

logger = logging.getLogger(__name__)

# Notification urgency levels for notify-send
_URGENCY_LOW      = "low"
_URGENCY_NORMAL   = "normal"
_URGENCY_CRITICAL = "critical"


class Notifier(QObject):
    """
    Watches BatteryState changes and emits notification_ready when an
    alert should be shown.

    Signals
    -------
    notification_ready(title, body, urgency)
        Carry a notification payload.  Connect to TrayIcon.show_notification.
    """

    notification_ready = pyqtSignal(str, str, str)   # title, body, urgency

    def __init__(
        self,
        low_battery_pct:      float = 20.0,
        critical_battery_pct: float = 10.0,
        cooldowns: Optional[dict] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._low_pct  = low_battery_pct
        self._crit_pct = critical_battery_pct

        default_cooldowns = {
            "low_battery":       300,
            "critical_battery":  120,
            "power_disconnected": 60,
            "power_restored":     30,
        }
        cd = {**default_cooldowns, **(cooldowns or {})}
        self._cooldown: dict[str, float] = {k: float(v) for k, v in cd.items()}
        self._last_sent: dict[str, float] = {}

        # Previous state for transition detection
        self._prev_external: Optional[bool] = None

    # ── Slot ─────────────────────────────────────────────────────────────────

    @pyqtSlot(object)
    def check_notifications(self, state: BatteryState) -> None:
        if not state.is_valid:
            return

        self._check_power_transitions(state)
        if not state.is_external_power:
            self._check_battery_thresholds(state)

    # ── Detection ─────────────────────────────────────────────────────────────

    def _check_power_transitions(self, state: BatteryState) -> None:
        ext = state.is_external_power
        if self._prev_external is None:
            self._prev_external = ext
            return

        if not self._prev_external and ext:
            self._maybe_send(
                key="power_restored",
                title="Power Restored",
                body=f"External power connected — {state.voltage_v:.2f} V ({state.percentage:.0f} %)",
                urgency=_URGENCY_NORMAL,
            )
        elif self._prev_external and not ext:
            pct = state.percentage
            runtime = state.estimated_runtime_min
            body = f"Running on battery — {pct:.0f} % ({state.voltage_v:.2f} V)"
            if runtime is not None:
                h, m = divmod(int(runtime), 60)
                body += f"\nEstimated runtime: {h}h {m:02d}m" if h else f"\nEstimated runtime: {m} min"
            self._maybe_send(
                key="power_disconnected",
                title="Power Disconnected",
                body=body,
                urgency=_URGENCY_NORMAL,
            )
        self._prev_external = ext

    def _check_battery_thresholds(self, state: BatteryState) -> None:
        pct = state.percentage
        v   = state.voltage_v

        if pct <= self._crit_pct:
            runtime = state.estimated_runtime_min
            body = f"Battery critical: {pct:.0f} % ({v:.2f} V)"
            if runtime is not None:
                body += f"\nLess than {int(runtime) + 1} minutes remaining!"
            self._maybe_send(
                key="critical_battery",
                title="CRITICAL: Battery Almost Dead",
                body=body,
                urgency=_URGENCY_CRITICAL,
            )
        elif pct <= self._low_pct:
            self._maybe_send(
                key="low_battery",
                title="Low Battery",
                body=f"Battery low: {pct:.0f} % ({v:.2f} V) — please connect power",
                urgency=_URGENCY_NORMAL,
            )

    # ── Cooldown gate ─────────────────────────────────────────────────────────

    def _maybe_send(self, key: str, title: str, body: str, urgency: str) -> None:
        now      = time.monotonic()
        cooldown = self._cooldown.get(key, 60.0)
        last     = self._last_sent.get(key, 0.0)

        if now - last < cooldown:
            logger.debug(
                "Notification '%s' suppressed (cooldown %.0f s remaining)",
                key, cooldown - (now - last),
            )
            return

        self._last_sent[key] = now
        self.notification_ready.emit(title, body, urgency)
        logger.info("Notification dispatched [%s]: %s", key, title)

    # ── Direct notify-send fallback ────────────────────────────────────────────

    @staticmethod
    def send_via_notify_send(title: str, body: str, urgency: str = _URGENCY_NORMAL) -> None:
        """
        Fire a notification via notify-send.  Used as a fallback when
        QSystemTrayIcon.showMessage() is silent (some GNOME configurations).
        Call this from the slot connected to notification_ready if the tray
        method proves unreliable.
        """
        try:
            subprocess.Popen(
                ["notify-send", f"--urgency={urgency}", "--app-name=Battery Monitor",
                 "--icon=battery", title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("notify-send not available; install libnotify-bin")
        except OSError as exc:
            logger.error("notify-send failed: %s", exc)
