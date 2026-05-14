"""
Power event detection and logging.

Detects state transitions (external power connected/disconnected,
low battery crossings) and logs them to a JSON-lines file and an
in-memory deque.  Also emits Qt signals so the UI can update its
event list in real time.
"""

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.battery import BatteryState, ChargeState

logger = logging.getLogger(__name__)


class EventKind(str, Enum):
    POWER_CONNECTED    = "power_connected"
    POWER_DISCONNECTED = "power_disconnected"
    LOW_BATTERY        = "low_battery"
    CRITICAL_BATTERY   = "critical_battery"
    SENSOR_ERROR       = "sensor_error"
    SENSOR_RECOVERED   = "sensor_recovered"
    APP_START          = "app_start"
    APP_STOP           = "app_stop"


@dataclass
class PowerEvent:
    kind:       str
    timestamp:  float
    message:    str
    voltage_v:  Optional[float] = None
    percentage: Optional[float] = None

    def iso_time(self) -> str:
        import datetime
        return datetime.datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")


class PowerEventLogger(QObject):
    """
    Watches BatteryState transitions and records power events.

    Signals
    -------
    event_added(PowerEvent) — emitted whenever a new event is recorded
    """

    event_added = pyqtSignal(object)   # PowerEvent

    def __init__(
        self,
        log_file:           str  = "logs/power-events.jsonl",
        memory_limit:       int  = 500,
        low_battery_pct:    float = 20.0,
        critical_battery_pct: float = 10.0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._log_path   = Path(log_file)
        self._history: deque[PowerEvent] = deque(maxlen=memory_limit)
        self._low_pct    = low_battery_pct
        self._crit_pct   = critical_battery_pct

        # Previous-state tracking for change detection
        self._prev_external_power: Optional[bool]        = None
        self._prev_charge_state:   Optional[ChargeState] = None
        self._prev_valid:          Optional[bool]        = None
        self._was_low              = False
        self._was_critical         = False

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._record(EventKind.APP_START, "Battery monitor started")

    # ── Public interface ──────────────────────────────────────────────────────

    @pyqtSlot(object)
    def on_state_update(self, state: BatteryState) -> None:
        """Called from BatteryManager.state_updated signal."""
        self._detect_sensor_transitions(state)
        if not state.is_valid:
            return
        self._detect_power_transitions(state)
        self._detect_threshold_crossings(state)

    @property
    def history(self) -> list[PowerEvent]:
        return list(self._history)

    def log_app_stop(self) -> None:
        self._record(EventKind.APP_STOP, "Battery monitor stopping")

    # ── Detection helpers ─────────────────────────────────────────────────────

    def _detect_sensor_transitions(self, state: BatteryState) -> None:
        if self._prev_valid is None:
            self._prev_valid = state.is_valid
            return
        if not self._prev_valid and state.is_valid:
            self._record(EventKind.SENSOR_RECOVERED, "I2C sensor recovered")
        elif self._prev_valid and not state.is_valid:
            self._record(
                EventKind.SENSOR_ERROR,
                f"I2C sensor error: {state.error}",
            )
        self._prev_valid = state.is_valid

    def _detect_power_transitions(self, state: BatteryState) -> None:
        if self._prev_external_power is None:
            self._prev_external_power = state.is_external_power
            return

        if not self._prev_external_power and state.is_external_power:
            self._record(
                EventKind.POWER_CONNECTED,
                f"External power connected — {state.voltage_v:.3f} V "
                f"({state.percentage:.0f} %)",
                state,
            )
        elif self._prev_external_power and not state.is_external_power:
            self._record(
                EventKind.POWER_DISCONNECTED,
                f"External power disconnected — {state.voltage_v:.3f} V "
                f"({state.percentage:.0f} %)",
                state,
            )
        self._prev_external_power = state.is_external_power

    def _detect_threshold_crossings(self, state: BatteryState) -> None:
        pct = state.percentage

        is_critical = pct <= self._crit_pct
        is_low      = pct <= self._low_pct and not is_critical

        if is_critical and not self._was_critical:
            self._record(
                EventKind.CRITICAL_BATTERY,
                f"Critical battery: {pct:.0f} % ({state.voltage_v:.3f} V)",
                state,
            )
        elif is_low and not self._was_low:
            self._record(
                EventKind.LOW_BATTERY,
                f"Low battery: {pct:.0f} % ({state.voltage_v:.3f} V)",
                state,
            )

        # Reset hysteresis when charging back above thresholds
        if not is_critical and pct > self._crit_pct + 5.0:
            self._was_critical = False
        if not is_low and pct > self._low_pct + 5.0:
            self._was_low = False

        self._was_critical = self._was_critical or is_critical
        self._was_low      = self._was_low or is_low

    # ── Persistence ───────────────────────────────────────────────────────────

    def _record(
        self,
        kind: EventKind,
        message: str,
        state: Optional[BatteryState] = None,
    ) -> None:
        event = PowerEvent(
            kind=kind.value,
            timestamp=time.time(),
            message=message,
            voltage_v=state.voltage_v    if state else None,
            percentage=state.percentage  if state else None,
        )
        self._history.append(event)
        self._persist(event)
        self.event_added.emit(event)
        logger.info("Power event [%s]: %s", kind.value, message)

    def _persist(self, event: PowerEvent) -> None:
        try:
            with self._log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts":         event.timestamp,
                    "iso":        event.iso_time(),
                    "kind":       event.kind,
                    "message":    event.message,
                    "voltage_v":  event.voltage_v,
                    "percentage": event.percentage,
                }) + "\n")
        except OSError as exc:
            logger.error("Failed to persist power event: %s", exc)
