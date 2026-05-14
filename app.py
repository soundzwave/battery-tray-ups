#!/usr/bin/env python3
"""
Battery Monitor — Waveshare UPS HAT D on Orange Pi Zero 3
==========================================================
Entry point.  Wires together all subsystems and runs the Qt event loop.

Usage
-----
  python app.py [--config PATH] [--verbose] [--no-tray] [--no-window]

Signal/slot wiring diagram
--------------------------
  PollerThread ──reading_ready──► BatteryManager ──state_updated──► TrayIcon
                                                  ──state_updated──► MainWindow
                                                  ──state_updated──► Notifier
                                                  ──state_updated──► MetricsCollector
                                                  ──state_updated──► PowerEventLogger
  PowerEventLogger ──event_added──► MainWindow
  Notifier ──notification_ready──► TrayIcon
"""

import argparse
import logging
import os
import re
import signal
import sys
from pathlib import Path
from typing import Any

import yaml
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from core.battery import BatteryManager
from core.ina219 import INA219
from core.logger import setup_logging
from core.metrics import MetricsCollector
from core.notifier import Notifier
from core.poller import PollerThread
from core.power_events import PowerEventLogger
from api.prometheus import PrometheusServer
from ui.tray import TrayIcon
from ui.window import MainWindow


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Battery monitor for Waveshare UPS HAT D (INA219 auto-detected on I2C bus)"
    )
    p.add_argument("--config",    default="config/config.yaml", help="YAML config path")
    p.add_argument("--verbose",   action="store_true",           help="Enable DEBUG logging")
    p.add_argument("--no-tray",   action="store_true",           help="Disable system tray icon")
    p.add_argument("--no-window", action="store_true",           help="Start without showing the window")
    return p.parse_args()


def _load_config(path: str) -> dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    with cfg_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _save_bus_to_config(config_path: str, bus_number: int) -> None:
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"^(\s*bus:\s*)\d+", lambda m: f"{m.group(1)}{bus_number}", text, flags=re.MULTILINE)
        path.write_text(text, encoding="utf-8")
        logging.getLogger(__name__).info("Saved i2c.bus=%d to %s", bus_number, config_path)
    except OSError as e:
        logging.getLogger(__name__).warning("Could not save bus to config: %s", e)


def main() -> None:
    args   = _parse_args()
    config = _load_config(args.config)

    log_cfg = config["logging"]
    setup_logging(
        level       = "DEBUG" if args.verbose else log_cfg["level"],
        log_file    = log_cfg["file"],
        max_bytes   = log_cfg["max_bytes"],
        backup_count= log_cfg["backup_count"],
    )
    logger = logging.getLogger(__name__)
    logger.info("Battery Monitor starting — pid=%d", os.getpid())

    # ── Qt application ────────────────────────────────────────────────────────
    app = QApplication(sys.argv)
    app.setApplicationName("Battery Monitor")
    app.setApplicationDisplayName("Battery Monitor")
    # Keep running when all windows are closed (tray-only mode)
    app.setQuitOnLastWindowClosed(False)

    # ── INA219 driver ─────────────────────────────────────────────────────────
    i2c_cfg = config["i2c"]

    def _make_ina219(bus_number: int) -> INA219:
        return INA219(
            bus_number   = bus_number,
            address      = i2c_cfg["address"],
            r_shunt_ohm  = i2c_cfg["r_shunt_ohm"],
            max_current_a= i2c_cfg["max_current_a"],
            retry_count  = i2c_cfg["retry_count"],
            retry_delay_s= i2c_cfg["retry_delay_s"],
        )

    ina219 = _make_ina219(i2c_cfg["bus"])
    if not ina219.connect():
        logger.warning("INA219 not found on configured bus %d — scanning all I2C buses", i2c_cfg["bus"])
        found = False
        for dev in sorted(Path("/dev").glob("i2c-*")):
            try:
                bus_num = int(dev.name.split("-")[1])
            except ValueError:
                continue
            if bus_num == i2c_cfg["bus"]:
                continue
            candidate = _make_ina219(bus_num)
            if candidate.connect():
                logger.info("INA219 found on bus %d — saving to config", bus_num)
                ina219 = candidate
                found = True
                _save_bus_to_config(args.config, bus_num)
                break
        if not found:
            logger.warning("INA219 not found on any I2C bus — will retry in poller")

    # ── Core subsystems ───────────────────────────────────────────────────────
    batt_cfg = config["battery"]
    battery  = BatteryManager(
        nominal_capacity_mah = batt_cfg["nominal_capacity_mah"],
        full_voltage_v       = batt_cfg["full_voltage_v"],
        empty_voltage_v      = batt_cfg["empty_voltage_v"],
        cells                = batt_cfg.get("cells", 1),
        invert_current       = batt_cfg.get("invert_current", False),
    )

    notif_cfg = config["notifications"]
    notifier  = Notifier(
        low_battery_pct      = notif_cfg["low_battery_percent"],
        critical_battery_pct = notif_cfg["critical_battery_percent"],
        cooldowns            = notif_cfg["cooldown_seconds"],
    )

    power_log = PowerEventLogger(
        low_battery_pct      = notif_cfg["low_battery_percent"],
        critical_battery_pct = notif_cfg["critical_battery_percent"],
    )

    metrics = MetricsCollector()

    # ── Prometheus server ─────────────────────────────────────────────────────
    prom_server: PrometheusServer | None = None
    prom_cfg = config["prometheus"]
    if prom_cfg["enabled"]:
        prom_server = PrometheusServer(host=prom_cfg["host"], port=prom_cfg["port"])
        prom_server.start()

    # ── Polling thread ────────────────────────────────────────────────────────
    poll_cfg = config["polling"]
    poller   = PollerThread(ina219=ina219, interval_ms=poll_cfg["interval_ms"])

    # ── Wiring: poller → battery manager ──────────────────────────────────────
    poller.reading_ready.connect(battery.process_reading)

    # ── Wiring: battery state → consumers ────────────────────────────────────
    battery.state_updated.connect(metrics.update)
    battery.state_updated.connect(power_log.on_state_update)
    battery.state_updated.connect(notifier.check_notifications)

    # ── UI ────────────────────────────────────────────────────────────────────
    tray:   TrayIcon | None   = None
    window: MainWindow | None = None

    use_tray = not args.no_tray and QSystemTrayIcon.isSystemTrayAvailable()
    if not use_tray and not args.no_tray:
        logger.warning(
            "System tray not available — running without tray icon. "
            "On GNOME, install gnome-shell-extension-appindicator."
        )

    ui_cfg = config.get("ui", {})
    window = MainWindow(
        history_size = config["history"]["size"],
        chart_points = ui_cfg.get("chart_points", 300),
        no_tray      = not use_tray,
    )
    window.resize(
        ui_cfg.get("window_width",  860),
        ui_cfg.get("window_height", 640),
    )

    battery.state_updated.connect(window.update_state)
    power_log.event_added.connect(window.add_event)

    if use_tray:
        tray = TrayIcon(parent=app)
        battery.state_updated.connect(tray.update_state)
        notifier.notification_ready.connect(tray.show_notification)

        def _toggle_window() -> None:
            if window.isVisible():
                window.hide()
            else:
                window.show()
                window.raise_()
                window.activateWindow()

        tray.show_window_requested.connect(_toggle_window)
        tray.quit_requested.connect(app.quit)

    if not args.no_window:
        window.show()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    def _shutdown() -> None:
        logger.info("Shutting down…")
        power_log.log_app_stop()
        poller.stop()
        poller.wait(3000)
        ina219.disconnect()
        if prom_server:
            prom_server.stop()
        logger.info("Shutdown complete")

    app.aboutToQuit.connect(_shutdown)

    # Allow Python signal handlers to fire inside the Qt event loop.
    # Qt normally blocks between events; this timer gives CPython a window
    # to run signal handlers every 500 ms.
    sigterm_timer = QTimer()
    sigterm_timer.start(500)
    sigterm_timer.timeout.connect(lambda: None)

    def _handle_unix_signal(*_) -> None:
        app.quit()

    signal.signal(signal.SIGTERM, _handle_unix_signal)
    signal.signal(signal.SIGINT,  _handle_unix_signal)

    # ── Start ─────────────────────────────────────────────────────────────────
    poller.start()
    logger.info(
        "Battery Monitor running — poll=%d ms prometheus=%s",
        poll_cfg["interval_ms"],
        f"http://{prom_cfg['host']}:{prom_cfg['port']}/metrics" if prom_cfg["enabled"] else "disabled",
    )
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
