#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import socket
import logging
import logging.handlers
import signal
import subprocess
import configparser
import statistics
import smbus2
from collections import deque
import INA219
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QAction, QMessageBox
)
from PyQt5.QtCore import QObject, QThread, pyqtSignal, pyqtSlot, QTimer, QFileSystemWatcher

signal.signal(signal.SIGINT, signal.SIG_DFL)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_cfg = configparser.ConfigParser()
if not _cfg.read(os.path.join(BASE_DIR, "config.ini")):
    logging.warning("config.ini not found, using defaults")

def _get(section, key, fallback):
    return _cfg.get(section, key, fallback=str(fallback))

BATTERY_CAPACITY_MAH   = int(_get("battery", "capacity_mah",               4000))
MIN_VOLTAGE_V          = float(_get("battery", "min_voltage_v",             3.00))
MAX_VOLTAGE_V          = float(_get("battery", "max_voltage_v",             4.20))
WARN_30_PCT            = int(_get("battery", "warn_30_pct",                   30))
WARN_20_PCT            = int(_get("battery", "warn_20_pct",                   20))
WARN_10_PCT            = int(_get("battery", "warn_10_pct",                   10))
WARN_5_PCT             = int(_get("battery", "warn_5_pct",                     5))
SHUTDOWN_COUNTDOWN_SEC = int(_get("battery", "shutdown_countdown_sec",        60))
INA219_ADDR            = int(_get("sensor",  "ina219_addr",               "0x43"), 0)
INA219_BUS             = int(_get("sensor",  "i2c_bus",                        1))
UPS_CONTROLLER_ADDR    = int(_get("sensor",  "ups_controller_addr",        "0x2d"), 0)
CHARGE_THRESHOLD_MA    = int(_get("sensor",  "charge_current_threshold_ma",   50))
POLL_INTERVAL_MS       = int(_get("sensor",  "poll_interval_ms",            1000))
_CURRENT_HISTORY_LEN   = max(5, 300_000 // POLL_INTERVAL_MS)  # ~5 min window
LOG_FILE               = _get("logging", "log_file",                           "")
LOG_MAX_BYTES          = int(_get("logging", "max_bytes",                 1048576))
LOG_BACKUP_COUNT       = int(_get("logging", "backup_count",                    3))
DISCHARGE_CURVE        = _get("battery", "discharge_curve",               "lipo")
NOTIFY_BACKEND         = _get("notifications", "backend",                    "qt")
BATTERY_GOVERNOR       = _get("power",   "battery_governor",           "powersave")
AC_GOVERNOR            = _get("power",   "ac_governor",                 "ondemand")
WIFI_IFACE             = _get("power",   "wifi_interface",                "wlan0")
WIFI_POWERSAVE         = _get("power",   "wifi_powersave_on_battery",     "true").lower() == "true"
STATUS_FILE            = _get("output",  "status_file", "/tmp/battery_status.json")

_handlers = [logging.StreamHandler()]
if LOG_FILE:
    _log_path = LOG_FILE if os.path.isabs(LOG_FILE) else os.path.join(BASE_DIR, LOG_FILE)
    _handlers.append(logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    ))
logging.basicConfig(format="%(message)s", level=logging.INFO, handlers=_handlers)


_LIPO_SOC = [
    (4.20, 100), (4.15, 95), (4.11, 90), (4.08, 85),
    (4.02,  80), (3.98, 75), (3.95, 70), (3.91, 65),
    (3.87,  60), (3.83, 55), (3.79, 50), (3.75, 45),
    (3.71,  40), (3.67, 35), (3.61, 30), (3.55, 25),
    (3.49,  20), (3.42, 15), (3.35, 10), (3.20,  5),
    (3.00,   0),
]

def voltage_to_percent(v):
    if DISCHARGE_CURVE == "lipo":
        if v >= _LIPO_SOC[0][0]:
            return 100
        if v <= _LIPO_SOC[-1][0]:
            return 0
        for i in range(len(_LIPO_SOC) - 1):
            v_hi, p_hi = _LIPO_SOC[i]
            v_lo, p_lo = _LIPO_SOC[i + 1]
            if v_lo <= v <= v_hi:
                t = (v - v_lo) / (v_hi - v_lo)
                return int(p_lo + t * (p_hi - p_lo))
        return 0
    if v >= MAX_VOLTAGE_V:
        return 100
    if v <= MIN_VOLTAGE_V:
        return 0
    return int((v - MIN_VOLTAGE_V) / (MAX_VOLTAGE_V - MIN_VOLTAGE_V) * 100)


def _sd_notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET", "")
    if not addr:
        return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr if not addr.startswith("@") else "\0" + addr[1:])
            s.sendall(state.encode())
    except OSError:
        pass


def _img(name):
    return os.path.join(BASE_DIR, "images", name)

def format_time(minutes):
    if minutes >= 60:
        return f"~{minutes // 60}h {minutes % 60:02d}m"
    return f"~{minutes}m"


class Worker(QObject):
    reading = pyqtSignal(float, float, float)  # voltage, current_mA, power_W
    error   = pyqtSignal(str)

    _RECOVERY_AFTER = 3
    _INIT_RETRY_MS  = 10_000

    def __init__(self, ina):
        super().__init__()
        self._ina = ina
        self._timer = None
        self._in_error = False
        self._error_streak = 0

    def run(self):
        self._timer = QTimer()
        self._timer.timeout.connect(self._poll)
        if self._ina is None:
            logging.info("INA219 not ready at startup — will retry every 10 s")
            self._timer.setInterval(self._INIT_RETRY_MS)
        else:
            self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.start()

    def _poll(self):
        if self._ina is None:
            try:
                self._ina = INA219.INA219(i2c_bus=INA219_BUS, addr=INA219_ADDR)
                logging.info("INA219 initialised successfully")
                self._timer.setInterval(POLL_INTERVAL_MS)
            except Exception as e:
                logging.warning(f"INA219 init retry failed: {e}")
            return

        try:
            v = self._ina.getBusVoltage_V()
            c = int(self._ina.getCurrent_mA())
            w = self._ina.getPower_W()
            if self._in_error:
                self._in_error = False
                self._error_streak = 0
                self._timer.setInterval(POLL_INTERVAL_MS)
            self.reading.emit(v, c, w)
        except Exception as e:
            self._error_streak += 1
            self._in_error = True
            self._timer.setInterval(5000)
            self.error.emit(str(e))
            if self._error_streak >= self._RECOVERY_AFTER:
                self._error_streak = 0
                self._recover_bus()

    def _recover_bus(self):
        logging.info("I2C bus locked — attempting driver reset...")
        try:
            dev_link = f"/sys/class/i2c-adapter/i2c-{INA219_BUS}/device"
            dev_name = os.path.basename(os.readlink(dev_link))
            unbind = "/sys/bus/platform/drivers/mv64xxx_i2c/unbind"
            with open(unbind, "w") as f:
                f.write(dev_name)
            QTimer.singleShot(1000, lambda: self._recover_bind(dev_name))
        except Exception as e:
            logging.error(f"I2C bus recovery failed: {e}")

    def _recover_bind(self, dev_name):
        try:
            bind = "/sys/bus/platform/drivers/mv64xxx_i2c/bind"
            with open(bind, "w") as f:
                f.write(dev_name)
            QTimer.singleShot(1000, self._recover_reconnect)
        except Exception as e:
            logging.error(f"I2C bus recovery (bind) failed: {e}")

    def _recover_reconnect(self):
        try:
            self._ina.bus.close()
            self._ina.bus = smbus2.SMBus(INA219_BUS)
            logging.info("I2C bus recovery succeeded")
        except Exception as e:
            logging.error(f"I2C bus recovery (reconnect) failed: {e}")

    @pyqtSlot(int)
    def set_interval(self, ms: int):
        if self._timer:
            self._timer.setInterval(ms)

    def stop(self):
        if self._timer:
            self._timer.stop()
        try:
            self._ina.bus.close()
        except Exception:
            pass


class BatteryMonitor(QObject):
    _poll_interval_changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._charging = False
        self._prev_charging = None
        self._percent = 0
        self._voltage = 0.0
        self._current = 0.0
        self._power = 0.0
        self._buf = deque(maxlen=5)
        self._current_history = deque(maxlen=_CURRENT_HISTORY_LEN)
        self._charge_history  = deque(maxlen=_CURRENT_HISTORY_LEN)
        self._low30_notified = False
        self._low20_notified = False
        self._low10_notified = False
        self._low5_notified = False
        self._i2c_error_notified = False
        self._countdown = 0
        self._status_dlg = None
        self._warn20_dlg = None
        self._critical_dlg = None
        self._about_dlg = None

        self._tray = QSystemTrayIcon()
        self._tray.setIcon(QIcon(_img("battery.png")))
        self._tray.setContextMenu(self._build_menu())
        self._tray.activated.connect(
            lambda r: self._show_status() if r == QSystemTrayIcon.Trigger else None
        )
        self._tray.show()

        self._shutdown_timer = QTimer()
        self._shutdown_timer.setInterval(1000)
        self._shutdown_timer.timeout.connect(self._tick)

        ina = None
        try:
            ina = INA219.INA219(i2c_bus=INA219_BUS, addr=INA219_ADDR)
        except Exception as e:
            logging.warning(f"INA219 init failed at startup, will retry in background: {e}")
            self._notify("Sensor Error",
                         "Cannot initialise battery sensor. Retrying...",
                         QSystemTrayIcon.Warning, 8000)
            self._tray.setToolTip("Sensor init failed — retrying...")

        if ina is not None:
            try:
                self._charging = ina.getCurrent_mA() < -CHARGE_THRESHOLD_MA
            except Exception:
                pass
        self._prev_charging = self._charging
        self._set_cpu_governor(AC_GOVERNOR if self._charging else BATTERY_GOVERNOR)
        self._set_wifi_powersave(not self._charging)

        self._thread = QThread()
        self._worker = Worker(ina)
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.started.connect(self._worker.run)
        self._worker.reading.connect(self._on_reading)
        self._worker.error.connect(self._on_i2c_error)
        self._poll_interval_changed.connect(self._worker.set_interval)
        QApplication.instance().aboutToQuit.connect(self._stop_worker)
        self._thread.start()

        self._config_path = os.path.join(BASE_DIR, "config.ini")
        self._config_watcher = QFileSystemWatcher([self._config_path])
        self._config_watcher.fileChanged.connect(self._reload_config)

        self._watchdog_timer = QTimer()
        self._watchdog_timer.setInterval(15_000)
        self._watchdog_timer.timeout.connect(lambda: _sd_notify("WATCHDOG=1"))
        self._watchdog_timer.start()
        _sd_notify("READY=1")

    def _build_menu(self):
        status_act = QAction("Status", self)
        about_act  = QAction("About",  self)
        quit_act   = QAction("Exit",   self)
        status_act.triggered.connect(self._show_status)
        about_act.triggered.connect(self._show_about)
        quit_act.triggered.connect(QApplication.instance().quit)
        menu = QMenu()
        menu.addAction(status_act)
        menu.addAction(about_act)
        menu.addSeparator()
        menu.addAction(quit_act)
        return menu

    def _on_reading(self, v, c, w):
        if not (MIN_VOLTAGE_V - 0.5 <= v <= MAX_VOLTAGE_V + 0.5):
            logging.warning(f"Ignoring out-of-range voltage: {v:.2f}V")
            return
        if self._i2c_error_notified:
            self._buf.clear()
        self._buf.append((v, c, w))
        mv = statistics.median(r[0] for r in self._buf)
        mc = statistics.median(r[1] for r in self._buf)
        mw = statistics.median(r[2] for r in self._buf)
        self._voltage = mv
        self._current = mc
        self._power = mw
        self._i2c_error_notified = False
        self._charging = mc < -CHARGE_THRESHOLD_MA

        if self._prev_charging is not None and self._prev_charging != self._charging:
            if self._charging:
                self._current_history.clear()
                self._notify("Power Connected", "Battery is charging.",
                             QSystemTrayIcon.Information, 4000)
                if self._shutdown_timer.isActive():
                    self._cancel_shutdown(user_dismissed=False)
                self._set_cpu_governor(AC_GOVERNOR)
                self._set_wifi_powersave(False)
            else:
                self._charge_history.clear()
                self._notify("Power Disconnected", "Running on battery.",
                             QSystemTrayIcon.Warning, 4000)
                self._set_cpu_governor(BATTERY_GOVERNOR)
                self._set_wifi_powersave(True)
        self._prev_charging = self._charging

        if self._charging:
            self._charge_history.append(abs(mc))
        else:
            self._current_history.append(abs(mc))

        self._percent = voltage_to_percent(mv)
        icon_idx = int(self._percent / 10) + (11 if self._charging else 0)
        self._tray.setIcon(QIcon(_img(f"battery.{icon_idx}.png")))

        time_str = self._time_str()
        tooltip = f"{self._percent}%  {mv:.2f}V  {abs(int(mc))}mA  {mw:.1f}W  {time_str}"
        self._tray.setToolTip(tooltip.strip())
        logging.info(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {tooltip.strip()}")

        if self._status_dlg and self._status_dlg.isVisible():
            self._status_dlg.setInformativeText(self._status_text())

        self._write_status_file()

        if self._charging or self._percent > WARN_30_PCT:
            self._low30_notified = False
        if self._charging or self._percent > WARN_20_PCT:
            self._low20_notified = False
        if self._charging or self._percent > WARN_10_PCT:
            self._low10_notified = False
        if self._charging or self._percent > WARN_5_PCT:
            self._low5_notified = False

        if not self._charging:
            self._check_warnings()

    def _time_str(self):
        if self._charging:
            if not self._charge_history:
                return "charging"
            avg_mA = statistics.mean(self._charge_history)
            if avg_mA > 10:
                to_full = (1.0 - self._percent / 100.0) * BATTERY_CAPACITY_MAH
                return f"full in {format_time(int(to_full / avg_mA * 60))}"
            return "charging"
        if not self._current_history:
            return ""
        avg_mA = statistics.mean(self._current_history)
        if avg_mA > 10:
            remaining = (self._percent / 100.0) * BATTERY_CAPACITY_MAH
            return format_time(int(remaining / avg_mA * 60))
        return ""

    def _status_text(self):
        return (
            f"Percent:    {self._percent}%\n"
            f"Voltage:    {self._voltage:.2f}V\n"
            f"Current:    {int(abs(self._current)):4d}mA\n"
            f"Power:      {self._power:.1f}W\n"
            f"Remaining:  {self._time_str()}"
        )

    def _check_warnings(self):
        p = self._percent
        if p <= WARN_5_PCT and not self._low5_notified:
            self._low5_notified = True
            self._low10_notified = True
            self._low20_notified = True
            self._low30_notified = True
            if self._warn20_dlg:
                self._warn20_dlg.close()
                self._warn20_dlg = None
            self._countdown = SHUTDOWN_COUNTDOWN_SEC
            self._shutdown_timer.start()
            dlg = QMessageBox(QMessageBox.NoIcon, "Battery Critical",
                              "<p><strong>Battery at 5% — critical!<br>"
                              "Connect the power adapter.</strong>")
            dlg.setIconPixmap(QPixmap(_img("batteryQ.png")))
            dlg.setInformativeText(f"Auto-shutdown in {self._countdown} seconds")
            dlg.addButton("Suspend", QMessageBox.ActionRole).clicked.connect(
                self._do_suspend
            )
            dlg.addButton("Cancel", QMessageBox.RejectRole).clicked.connect(
                lambda: self._cancel_shutdown(user_dismissed=True)
            )
            self._critical_dlg = dlg
            dlg.show()
        elif p <= WARN_10_PCT and not self._low10_notified:
            self._low10_notified = True
            self._low20_notified = True
            self._low30_notified = True
            self._notify("Battery Critical",
                         "Battery at 10%. Connect power adapter immediately!",
                         QSystemTrayIcon.Critical, 8000)
        elif p <= WARN_20_PCT and not self._low20_notified:
            self._low20_notified = True
            self._low30_notified = True
            dlg = QMessageBox(QMessageBox.Warning, "Battery Warning",
                              "<p><strong>Battery at 20%!<br>"
                              "Connect the power adapter.</strong>")
            dlg.setIconPixmap(QPixmap(_img("batteryQ.png")))
            dlg.addButton("OK", QMessageBox.AcceptRole)
            dlg.finished.connect(lambda: setattr(self, "_warn20_dlg", None))
            self._warn20_dlg = dlg
            dlg.show()
        elif p <= WARN_30_PCT and not self._low30_notified:
            self._low30_notified = True
            self._notify("Battery Low",
                         "Battery at 30%. Consider connecting the power adapter.",
                         QSystemTrayIcon.Warning, 5000)

    def _tick(self):
        self._countdown -= 1
        if self._countdown <= 0:
            self._shutdown_timer.stop()
            self._do_shutdown()
        elif self._critical_dlg:
            self._critical_dlg.setInformativeText(
                f"Auto-shutdown in {self._countdown} seconds"
            )

    def _cancel_shutdown(self, user_dismissed=True):
        self._shutdown_timer.stop()
        if self._critical_dlg:
            self._critical_dlg.close()
            self._critical_dlg = None
        if not user_dismissed:
            self._low10_notified = False

    def _do_shutdown(self):
        if self._critical_dlg:
            self._critical_dlg.close()
        try:
            addr_hex = hex(UPS_CONTROLLER_ADDR)
            addr_str = format(UPS_CONTROLLER_ADDR, '02x')
            result = subprocess.run(
                ["i2cdetect", "-y", "-r", str(INA219_BUS), addr_hex, addr_hex],
                capture_output=True, text=True, timeout=5)
            if addr_str in result.stdout:
                subprocess.run(["i2cset", "-y", str(INA219_BUS), addr_hex, "0x01", "0x55"], timeout=5)
        except Exception as e:
            logging.error(f"i2c shutdown sequence failed: {e}")
        subprocess.run(["sudo", "poweroff"])

    def _show_status(self):
        if self._status_dlg and self._status_dlg.isVisible():
            self._status_dlg.raise_()
            self._status_dlg.activateWindow()
            return
        dlg = QMessageBox(QMessageBox.NoIcon, "Battery Status", "Battery Monitor")
        dlg.setIconPixmap(QPixmap(_img(f"battery.{int(self._percent / 10)}.png")))
        dlg.setInformativeText(self._status_text())
        dlg.finished.connect(lambda: setattr(self, "_status_dlg", None))
        self._status_dlg = dlg
        dlg.show()

    def _show_about(self):
        if self._about_dlg and self._about_dlg.isVisible():
            self._about_dlg.raise_()
            return
        dlg = QMessageBox(QMessageBox.NoIcon, "About",
                          "<p><strong>Battery Monitor</strong>"
                          "<p>Version: v1.2"
                          "<p>UPS HAT battery tray for Orange Pi Zero 3")
        dlg.setInformativeText('<a href="https://www.waveshare.com">WaveShare Official Website</a>')
        dlg.setIconPixmap(QPixmap(_img("logo.png")))
        dlg.finished.connect(lambda: setattr(self, "_about_dlg", None))
        self._about_dlg = dlg
        dlg.show()

    def _on_i2c_error(self, msg):
        logging.error(f"I2C error: {msg}. Retrying in 5s...")
        self._tray.setToolTip("I2C error — sensor unavailable")
        if not self._i2c_error_notified:
            self._i2c_error_notified = True
            self._notify("Sensor Error", "Cannot read battery sensor. Retrying...",
                         QSystemTrayIcon.Critical, 4000)

    def _notify(self, title: str, body: str, icon=QSystemTrayIcon.Information, timeout_ms: int = 5000):
        if NOTIFY_BACKEND == "notify-send":
            urgency = {
                QSystemTrayIcon.Information: "low",
                QSystemTrayIcon.Warning:     "normal",
                QSystemTrayIcon.Critical:    "critical",
            }.get(icon, "normal")
            try:
                subprocess.run(
                    ["notify-send", "-u", urgency, "-t", str(timeout_ms), title, body],
                    capture_output=True, timeout=3
                )
            except Exception as e:
                logging.warning(f"notify-send failed: {e}")
        else:
            self._tray.showMessage(title, body, icon, timeout_ms)

    def _set_cpu_governor(self, governor: str):
        for cpu in range(4):
            try:
                with open(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor", "w") as f:
                    f.write(governor)
            except OSError as e:
                logging.warning(f"CPU governor set failed for cpu{cpu}: {e}")

    def _set_wifi_powersave(self, enable: bool):
        if not WIFI_IFACE or not WIFI_POWERSAVE:
            return
        try:
            subprocess.run(["iwconfig", WIFI_IFACE, "power", "on" if enable else "off"],
                           capture_output=True, timeout=3)
        except Exception as e:
            logging.warning(f"WiFi power mode set failed: {e}")

    def _write_status_file(self):
        if not STATUS_FILE:
            return
        data = {
            "percent":       self._percent,
            "voltage":       round(self._voltage, 3),
            "current_ma":    int(self._current),
            "power_w":       round(self._power, 2),
            "charging":      self._charging,
            "time_remaining": self._time_str(),
            "timestamp":     int(time.time()),
        }
        try:
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, STATUS_FILE)
        except OSError as e:
            logging.warning(f"Status file write failed: {e}")

    def _do_suspend(self):
        self._cancel_shutdown(user_dismissed=True)
        try:
            subprocess.run(["loginctl", "lock-session"], timeout=3)
        except Exception as e:
            logging.warning(f"Screen lock failed: {e}")
        subprocess.run(["systemctl", "suspend"])

    def _reload_config(self, path: str):
        global WARN_30_PCT, WARN_20_PCT, WARN_10_PCT, WARN_5_PCT
        global SHUTDOWN_COUNTDOWN_SEC, CHARGE_THRESHOLD_MA, POLL_INTERVAL_MS
        global NOTIFY_BACKEND, BATTERY_GOVERNOR, AC_GOVERNOR, WIFI_IFACE, WIFI_POWERSAVE, STATUS_FILE
        _cfg.read(self._config_path)
        WARN_30_PCT            = int(_get("battery", "warn_30_pct",                  30))
        WARN_20_PCT            = int(_get("battery", "warn_20_pct",                  20))
        WARN_10_PCT            = int(_get("battery", "warn_10_pct",                  10))
        WARN_5_PCT             = int(_get("battery", "warn_5_pct",                    5))
        SHUTDOWN_COUNTDOWN_SEC = int(_get("battery", "shutdown_countdown_sec",        60))
        CHARGE_THRESHOLD_MA    = int(_get("sensor",  "charge_current_threshold_ma",   50))
        new_poll               = int(_get("sensor",  "poll_interval_ms",            1000))
        NOTIFY_BACKEND         = _get("notifications", "backend",                    "qt")
        BATTERY_GOVERNOR       = _get("power",   "battery_governor",           "powersave")
        AC_GOVERNOR            = _get("power",   "ac_governor",                 "ondemand")
        WIFI_IFACE             = _get("power",   "wifi_interface",                "wlan0")
        WIFI_POWERSAVE         = _get("power",   "wifi_powersave_on_battery", "true").lower() == "true"
        STATUS_FILE            = _get("output",  "status_file", "/tmp/battery_status.json")
        if new_poll != POLL_INTERVAL_MS:
            POLL_INTERVAL_MS = new_poll
            self._poll_interval_changed.emit(new_poll)
        logging.info("config.ini reloaded")
        if path not in self._config_watcher.files():
            self._config_watcher.addPath(path)

    def _stop_worker(self):
        self._worker.stop()
        self._thread.quit()
        self._thread.wait()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    monitor = BatteryMonitor()
    sys.exit(app.exec_())
