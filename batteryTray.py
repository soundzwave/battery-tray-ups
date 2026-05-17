#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
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
from PyQt5.QtCore import QObject, QThread, pyqtSignal, QTimer

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
CHARGE_THRESHOLD_MA    = int(_get("sensor",  "charge_current_threshold_ma",   50))
POLL_INTERVAL_MS       = int(_get("sensor",  "poll_interval_ms",            1000))
_CURRENT_HISTORY_LEN   = max(5, 300_000 // POLL_INTERVAL_MS)  # ~5 min window
LOG_FILE               = _get("logging", "log_file",                           "")
LOG_MAX_BYTES          = int(_get("logging", "max_bytes",                 1048576))
LOG_BACKUP_COUNT       = int(_get("logging", "backup_count",                    3))

_handlers = [logging.StreamHandler()]
if LOG_FILE:
    _log_path = LOG_FILE if os.path.isabs(LOG_FILE) else os.path.join(BASE_DIR, LOG_FILE)
    _handlers.append(logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
    ))
logging.basicConfig(format="%(message)s", level=logging.INFO, handlers=_handlers)


def voltage_to_percent(v):
    if v >= MAX_VOLTAGE_V:
        return 100
    if v <= MIN_VOLTAGE_V:
        return 0
    return int((v - MIN_VOLTAGE_V) / (MAX_VOLTAGE_V - MIN_VOLTAGE_V) * 100)


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

    def stop(self):
        if self._timer:
            self._timer.stop()
        try:
            self._ina.bus.close()
        except Exception:
            pass


class BatteryMonitor(QObject):
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
        self._tray.show()

        self._shutdown_timer = QTimer()
        self._shutdown_timer.setInterval(1000)
        self._shutdown_timer.timeout.connect(self._tick)

        ina = None
        try:
            ina = INA219.INA219(i2c_bus=INA219_BUS, addr=INA219_ADDR)
        except Exception as e:
            logging.warning(f"INA219 init failed at startup, will retry in background: {e}")
            self._tray.showMessage("Sensor Error",
                                   "Cannot initialise battery sensor. Retrying...",
                                   QSystemTrayIcon.Warning, 8000)
            self._tray.setToolTip("Sensor init failed — retrying...")

        self._thread = QThread()
        self._worker = Worker(ina)
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.started.connect(self._worker.run)
        self._worker.reading.connect(self._on_reading)
        self._worker.error.connect(self._on_i2c_error)
        QApplication.instance().aboutToQuit.connect(self._stop_worker)
        self._thread.start()

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
                self._tray.showMessage("Power Connected", "Battery is charging.",
                                       QSystemTrayIcon.Information, 4000)
                if self._shutdown_timer.isActive():
                    self._cancel_shutdown(user_dismissed=False)
            else:
                self._tray.showMessage("Power Disconnected", "Running on battery.",
                                       QSystemTrayIcon.Warning, 4000)
        self._prev_charging = self._charging

        if not self._charging:
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
            dlg.addButton("Cancel", QMessageBox.RejectRole).clicked.connect(
                lambda: self._cancel_shutdown(user_dismissed=True)
            )
            self._critical_dlg = dlg
            dlg.show()
        elif p <= WARN_10_PCT and not self._low10_notified:
            self._low10_notified = True
            self._low20_notified = True
            self._low30_notified = True
            self._tray.showMessage("Battery Critical",
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
            self._tray.showMessage("Battery Low",
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
            result = subprocess.run(["i2cdetect", "-y", "-r", str(INA219_BUS), "0x2d", "0x2d"],
                                    capture_output=True, text=True, timeout=5)
            if "2d" in result.stdout:
                subprocess.run(["i2cset", "-y", str(INA219_BUS), "0x2d", "0x01", "0x55"], timeout=5)
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
                          "<p>UPS HAT battery tray for Raspberry Pi")
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
            self._tray.showMessage("Sensor Error", "Cannot read battery sensor. Retrying...",
                                   QSystemTrayIcon.Critical, 4000)

    def _stop_worker(self):
        self._worker.stop()
        self._thread.quit()
        self._thread.wait()


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    monitor = BatteryMonitor()
    sys.exit(app.exec_())
