"""
Battery state calculation layer.

Converts raw INA219 readings into BatteryState objects containing:
  - State-of-charge percentage (piecewise-linear discharge curve)
  - Charge/discharge/full state detection with hysteresis
  - External power detection
  - Runtime estimation via Coulomb counting + EMA current smoothing
  - Battery health and cycle-count estimation

Percentage calculation
----------------------
We use piecewise linear interpolation on an empirical LiPo discharge
curve (no-load, 0.5 C, 25 °C).  Under load the curve shifts left
(voltage sag) so the reported percentage will be slightly optimistic
while under heavy discharge — this is expected and well-known behaviour.

Multi-cell packs
----------------
Set `cells` to the number of series cells.  Voltage is divided by
`cells` before lookup so the same curve applies to 1S, 2S, etc.

Cycle counting
--------------
A cycle is counted on each DISCHARGING → CHARGING transition.
This is an approximation; the true cycle count depends on depth-of-
discharge, which we cannot measure without a full BMS.  The counter
is not persisted across restarts.

Health estimation
-----------------
Typical LiPo loses ~20 % capacity over ~500 full cycles.
We model this as: health = clamp(100 − cycles × 0.04, 60, 100).
This is an order-of-magnitude estimate — treat it accordingly.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from core.ina219 import INA219Reading

logger = logging.getLogger(__name__)

# ── LiPo single-cell discharge curve  ────────────────────────────────────────
# (voltage in V, SoC in %)
# Sorted highest→lowest voltage for binary-search-style interpolation.
_DISCHARGE_CURVE: list[tuple[float, float]] = [
    (4.20, 100.0),
    (4.15,  95.0),
    (4.11,  90.0),
    (4.08,  85.0),
    (4.02,  80.0),
    (3.98,  75.0),
    (3.95,  70.0),
    (3.91,  65.0),
    (3.87,  60.0),
    (3.83,  55.0),
    (3.79,  50.0),
    (3.75,  45.0),
    (3.71,  40.0),
    (3.67,  35.0),
    (3.63,  30.0),
    (3.59,  25.0),
    (3.53,  20.0),
    (3.45,  15.0),
    (3.38,  10.0),
    (3.30,   5.0),
    (3.00,   0.0),
]

# Current thresholds for state detection
_CHARGE_THRESHOLD_MA    =  50.0   # mA; above this = definitely charging
_DISCHARGE_THRESHOLD_MA = -50.0   # mA; below this = definitely discharging

# Voltage within this margin of full_voltage still counts as "full"
_FULL_V_HYSTERESIS = 0.05

# EMA smoothing factor for current used in runtime estimation.
# α=0.05 → ~20-sample time constant ≈ 40 s at 2 s polling.
_EMA_ALPHA = 0.05

# Minimum discharge current for runtime to be meaningful
_MIN_RUNTIME_CURRENT_MA = 10.0

# Reasonable health floor — batteries don't drop below ~60 % capacity
_HEALTH_FLOOR_PCT = 60.0
# Approximate capacity loss per full cycle (linear model)
_HEALTH_LOSS_PER_CYCLE = 0.04


class ChargeState(str, Enum):
    CHARGING    = "charging"
    DISCHARGING = "discharging"
    FULL        = "full"
    UNKNOWN     = "unknown"


@dataclass(frozen=True, slots=True)
class BatteryState:
    voltage_v:           float
    current_ma:          float
    power_mw:            float
    shunt_voltage_mv:    float
    percentage:          float
    charge_state:        ChargeState
    is_external_power:   bool
    estimated_runtime_min: Optional[float]
    health_percent:      float
    cycle_count_estimate: int
    smoothed_current_ma: float
    timestamp:           float
    is_valid:            bool
    error:               Optional[str]


def _voltage_to_percentage(voltage_v: float, cells: int) -> float:
    """Piecewise-linear interpolation on the discharge curve."""
    vpc = voltage_v / cells          # per-cell voltage
    curve = _DISCHARGE_CURVE

    if vpc >= curve[0][0]:
        return 100.0
    if vpc <= curve[-1][0]:
        return 0.0

    for i in range(len(curve) - 1):
        v_hi, pct_hi = curve[i]
        v_lo, pct_lo = curve[i + 1]
        if v_lo <= vpc <= v_hi:
            t = (vpc - v_lo) / (v_hi - v_lo)
            return pct_lo + t * (pct_hi - pct_lo)

    return 0.0


class BatteryManager(QObject):
    """
    Stateful processor that converts INA219Reading → BatteryState.

    Emits state_updated(BatteryState) after every processed reading.
    Instance state (EMA, Coulomb counter, cycle count) is maintained
    across calls, so the object should live for the application lifetime.
    """

    state_updated = pyqtSignal(object)   # BatteryState

    def __init__(
        self,
        nominal_capacity_mah: float = 2000.0,
        full_voltage_v:       float = 4.2,
        empty_voltage_v:      float = 3.0,
        cells:                int   = 1,
        invert_current:       bool  = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._nominal_mah   = nominal_capacity_mah
        self._full_v        = full_voltage_v
        self._empty_v       = empty_voltage_v
        self._cells         = cells
        self._invert        = invert_current

        self._smoothed_ma:  float = 0.0
        self._remaining_mah: Optional[float] = None
        self._last_ts:       Optional[float] = None
        self._last_state     = ChargeState.UNKNOWN

        self._cycle_count    = 0
        self._was_discharging = False
        self._health_pct:    float = 100.0

    @pyqtSlot(object)
    def process_reading(self, reading: INA219Reading) -> None:
        now = time.monotonic()

        if not reading.is_valid:
            self.state_updated.emit(BatteryState(
                voltage_v=0.0, current_ma=0.0, power_mw=0.0,
                shunt_voltage_mv=0.0, percentage=0.0,
                charge_state=ChargeState.UNKNOWN,
                is_external_power=False,
                estimated_runtime_min=None,
                health_percent=self._health_pct,
                cycle_count_estimate=self._cycle_count,
                smoothed_current_ma=self._smoothed_ma,
                timestamp=now,
                is_valid=False,
                error=reading.error,
            ))
            return

        current_ma = -reading.current_ma if self._invert else reading.current_ma

        # ── Smoothed current (EMA) ────────────────────────────────────────────
        if self._last_ts is None:
            self._smoothed_ma = current_ma
        else:
            self._smoothed_ma = _EMA_ALPHA * current_ma + (1.0 - _EMA_ALPHA) * self._smoothed_ma

        # ── Charge state (with hysteresis via last state) ──────────────────────
        full_v_threshold = self._full_v * self._cells - _FULL_V_HYSTERESIS
        if current_ma > _CHARGE_THRESHOLD_MA:
            state = ChargeState.CHARGING
        elif current_ma < _DISCHARGE_THRESHOLD_MA:
            state = ChargeState.DISCHARGING
        elif reading.bus_voltage_v >= full_v_threshold:
            state = ChargeState.FULL
        else:
            state = self._last_state   # keep previous to prevent 50 mA flapping

        is_external = state in (ChargeState.CHARGING, ChargeState.FULL)

        # ── Cycle counting ────────────────────────────────────────────────────
        if state == ChargeState.DISCHARGING:
            self._was_discharging = True
        elif state == ChargeState.CHARGING and self._was_discharging:
            self._cycle_count += 1
            self._was_discharging = False
            self._health_pct = max(
                _HEALTH_FLOOR_PCT, 100.0 - self._cycle_count * _HEALTH_LOSS_PER_CYCLE
            )
            logger.info(
                "Charge cycle %d detected — estimated health %.1f %%",
                self._cycle_count, self._health_pct,
            )

        # ── Percentage (voltage curve) ─────────────────────────────────────────
        percentage = _voltage_to_percentage(reading.bus_voltage_v, self._cells)

        # ── Coulomb counting ──────────────────────────────────────────────────
        # Bootstrap remaining capacity from voltage on first valid reading.
        if self._remaining_mah is None:
            self._remaining_mah = (percentage / 100.0) * self._nominal_mah
        elif self._last_ts is not None:
            dt_h = (now - self._last_ts) / 3600.0
            if state == ChargeState.DISCHARGING:
                self._remaining_mah -= abs(current_ma) * dt_h
            elif state == ChargeState.CHARGING:
                self._remaining_mah += abs(current_ma) * dt_h
            self._remaining_mah = max(0.0, min(self._nominal_mah, self._remaining_mah))

        # ── Runtime estimation ─────────────────────────────────────────────────
        runtime_min: Optional[float] = None
        if (
            state == ChargeState.DISCHARGING
            and self._smoothed_ma < -_MIN_RUNTIME_CURRENT_MA
            and self._remaining_mah is not None
        ):
            runtime_min = (self._remaining_mah / abs(self._smoothed_ma)) * 60.0

        self._last_ts    = now
        self._last_state = state

        self.state_updated.emit(BatteryState(
            voltage_v=reading.bus_voltage_v,
            current_ma=current_ma,
            power_mw=reading.power_mw,
            shunt_voltage_mv=reading.shunt_voltage_mv,
            percentage=percentage,
            charge_state=state,
            is_external_power=is_external,
            estimated_runtime_min=runtime_min,
            health_percent=self._health_pct,
            cycle_count_estimate=self._cycle_count,
            smoothed_current_ma=self._smoothed_ma,
            timestamp=now,
            is_valid=True,
            error=None,
        ))
