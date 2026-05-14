"""
Prometheus metric definitions and update logic.

All gauges are module-level singletons registered at import time.
Call MetricsCollector.update(state) from the BatteryManager signal
to keep them current.

Exposed on http://127.0.0.1:9105/metrics (configured in api/prometheus.py).
"""

import logging

from prometheus_client import Gauge, Info

from core.battery import BatteryState, ChargeState

logger = logging.getLogger(__name__)

# ── Gauge declarations ────────────────────────────────────────────────────────
_NAMESPACE = "battery"

BATT_VOLTAGE = Gauge(
    f"{_NAMESPACE}_voltage_volts",
    "Bus voltage measured by INA219 (battery terminal voltage)",
)
BATT_CURRENT = Gauge(
    f"{_NAMESPACE}_current_milliamps",
    "Current in mA — positive = charging, negative = discharging",
)
BATT_POWER = Gauge(
    f"{_NAMESPACE}_power_milliwatts",
    "Power in mW",
)
BATT_SHUNT = Gauge(
    f"{_NAMESPACE}_shunt_voltage_millivolts",
    "Shunt voltage in mV (10 μV × raw ADC count)",
)
BATT_PERCENT = Gauge(
    f"{_NAMESPACE}_percentage",
    "State of charge in % (piecewise-linear LiPo discharge curve)",
)
BATT_EXTERNAL = Gauge(
    f"{_NAMESPACE}_external_power",
    "1 if external power is connected, 0 otherwise",
)
BATT_CHARGING = Gauge(
    f"{_NAMESPACE}_charging",
    "1 if actively charging, 0 otherwise",
)
BATT_RUNTIME = Gauge(
    f"{_NAMESPACE}_estimated_runtime_minutes",
    "Estimated remaining runtime in minutes (-1 = unknown / not discharging)",
)
BATT_HEALTH = Gauge(
    f"{_NAMESPACE}_health_percent",
    "Estimated battery health in % (cycle-count model, approximate)",
)
BATT_CYCLES = Gauge(
    f"{_NAMESPACE}_cycle_count_estimate",
    "Estimated charge cycle count (resets on restart)",
)
BATT_VALID = Gauge(
    f"{_NAMESPACE}_sensor_valid",
    "1 if the last INA219 reading was successful, 0 on I2C error",
)
BATT_SMOOTHED_CURRENT = Gauge(
    f"{_NAMESPACE}_smoothed_current_milliamps",
    "EMA-smoothed current used for runtime estimation",
)


class MetricsCollector:
    """Stateless adapter that updates Prometheus gauges from a BatteryState."""

    def update(self, state: BatteryState) -> None:
        BATT_VALID.set(1 if state.is_valid else 0)

        if not state.is_valid:
            return

        BATT_VOLTAGE.set(state.voltage_v)
        BATT_CURRENT.set(state.current_ma)
        BATT_POWER.set(state.power_mw)
        BATT_SHUNT.set(state.shunt_voltage_mv)
        BATT_PERCENT.set(state.percentage)
        BATT_EXTERNAL.set(1 if state.is_external_power else 0)
        BATT_CHARGING.set(1 if state.charge_state == ChargeState.CHARGING else 0)
        BATT_RUNTIME.set(state.estimated_runtime_min if state.estimated_runtime_min is not None else -1)
        BATT_HEALTH.set(state.health_percent)
        BATT_CYCLES.set(state.cycle_count_estimate)
        BATT_SMOOTHED_CURRENT.set(state.smoothed_current_ma)
