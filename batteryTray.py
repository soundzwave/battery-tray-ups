#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import time
import logging
import signal
import subprocess
import configparser
import INA219
from PyQt5.QtGui import (
    QIcon,
    QPixmap
)
from PyQt5.QtWidgets import (
    QApplication,
    QSystemTrayIcon,
    QMenu,
    QAction,
    QMessageBox
)
from PyQt5.QtCore import (
    QObject,
    QThread,
    pyqtSignal,
    QTimer
)

signal.signal(signal.SIGINT, signal.SIG_DFL)
logging.basicConfig(format="%(message)s", level=logging.INFO)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_cfg = configparser.ConfigParser()
_cfg_path = os.path.join(BASE_DIR, "config.ini")
if not _cfg.read(_cfg_path):
    logging.warning(f"config.ini not found at {_cfg_path}, using defaults")

def _get(section, key, fallback):
    return _cfg.get(section, key, fallback=str(fallback))

BATTERY_CAPACITY_MAH        = int(_get("battery", "capacity_mah",              4000))
WARN_30_PCT                 = int(_get("battery", "warn_30_pct",                  30))
WARN_20_PCT                 = int(_get("battery", "warn_20_pct",                  20))
WARN_10_PCT                 = int(_get("battery", "warn_10_pct",                  10))
SHUTDOWN_COUNTDOWN_SEC      = int(_get("battery", "shutdown_countdown_sec",       60))
BATTERY_MIN_V               = float(_get("battery", "min_voltage_v",             3.0))
BATTERY_MAX_V               = float(_get("battery", "max_voltage_v",             4.2))
INA219_ADDR                 = int(_get("sensor",  "ina219_addr",               "0x43"), 0)
CHARGE_CURRENT_THRESHOLD_MA = int(_get("sensor", "charge_current_threshold_ma",   50))

def _img(name):
    return os.path.join(BASE_DIR, "images", name)


def format_time_remaining(minutes):
    if minutes >= 60:
        return "~%dh %02dm" % (minutes // 60, minutes % 60)
    return "~%dm" % minutes


class Worker(QObject):
    trayMessage = pyqtSignal(float, float)
    i2cError = pyqtSignal(str)

    def __init__(self, ina_device):
        super().__init__()
        self._ina = ina_device
        self._timer = None
        self._error_mode = False

    def run(self):
        self._timer = QTimer()
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()

    def _poll(self):
        try:
            bus_voltage = self._ina.getBusVoltage_V()
            current = -int(self._ina.getCurrent_mA())
            if self._error_mode:
                self._error_mode = False
                self._timer.setInterval(1000)
            self.trayMessage.emit(bus_voltage, current)
        except Exception as e:
            self._error_mode = True
            self._timer.setInterval(5000)
            self.i2cError.emit(str(e))

    def stop(self):
        if self._timer:
            self._timer.stop()
        
class MainWindow(QMessageBox):
    def __init__(self):
        self.charge = False
        self.prev_charge = None
        self.tray_icon = None
        self.msgBox = None
        self.about = None
        self.counter = 0
        self.low30_notified = False
        self.low20_notified = False
        self.low10_notified = False
        self.i2c_error_notified = False

        QMessageBox.__init__(self)
        self.setWindowTitle("Status")
        self.setText("Battery Monitor")

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon(_img("battery.png")))

        show_action = QAction("Status", self)
        quit_action = QAction("Exit", self)
        about_action = QAction("About", self)
        show_action.triggered.connect(self.show)
        about_action.triggered.connect(self.show_about)
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu = QMenu()
        tray_menu.addAction(show_action)
        tray_menu.addAction(about_action)
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

        try:
            ina = INA219.INA219(addr=INA219_ADDR)
        except Exception as e:
            logging.error(f"Failed to initialise INA219: {e}")
            self.tray_icon.showMessage(
                "Sensor Error",
                f"Cannot initialise battery sensor:\n{e}",
                QSystemTrayIcon.Critical,
                8000
            )
            self.tray_icon.setToolTip("Sensor init failed")
            return

        self._thread = QThread(self)
        self._worker = Worker(ina)
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.started.connect(self._worker.run)
        self._worker.trayMessage.connect(self.refresh)
        self._worker.i2cError.connect(self.on_i2c_error)
        QApplication.instance().aboutToQuit.connect(self._stop_worker)
        self._thread.start()
        self._timer = QTimer(self, timeout=self.on_timeout)
        self._timer.stop()
    
    def on_timeout(self):
        self.counter -= 1
        if self.counter > 0:
            if self.charge:
                self.msgBox.hide()
                self.msgBox.close()
                self._timer.stop()
                self.msgBox = None
                self.low10_notified = False
            else:
                self.msgBox.setInformativeText(f"auto shutdown after {int(self.counter)} seconds")
                self.msgBox.show()
        else:
            try:
                result = subprocess.run(
                    ["i2cdetect", "-y", "-r", "1", "0x2d", "0x2d"],
                    capture_output=True, text=True, timeout=5
                )
                if "2d" in result.stdout:
                    subprocess.run(
                        ["i2cset", "-y", "1", "0x2d", "0x01", "0x55"],
                        timeout=5
                    )
            except Exception as e:
                logging.error(f"i2c shutdown sequence failed: {e}")
            subprocess.run(["sudo", "poweroff"])

    def refresh(self, v, c):
        self.i2c_error_notified = False
        self.charge = c > CHARGE_CURRENT_THRESHOLD_MA

        if self.prev_charge is not None and self.prev_charge != self.charge:
            if self.charge:
                self.tray_icon.showMessage(
                    "Power Connected",
                    "Power adapter connected. Battery is charging.",
                    QSystemTrayIcon.Information,
                    4000
                )
            else:
                self.tray_icon.showMessage(
                    "Power Disconnected",
                    "Running on battery.",
                    QSystemTrayIcon.Warning,
                    4000
                )
        self.prev_charge = self.charge

        voltage_range = BATTERY_MAX_V - BATTERY_MIN_V
        p = int((v - BATTERY_MIN_V) / voltage_range * 100)
        p = max(0, min(100, p))

        icon_idx = int(p / 10) + (11 if self.charge else 0)
        img = _img(f"battery.{icon_idx}.png")
        self.tray_icon.setIcon(QIcon(img))
        self.setIconPixmap(QPixmap(img))

        discharge_mA = abs(c)
        if not self.charge and discharge_mA > 10:
            remaining_mAh = (p / 100.0) * BATTERY_CAPACITY_MAH
            time_str = format_time_remaining(int(remaining_mAh / discharge_mA * 60))
        elif self.charge:
            time_str = "charging"
        else:
            time_str = ""

        s = "%d%%  %.1fV  %dmA  %s" % (p, v, c, time_str)
        self.tray_icon.setToolTip(s)
        info = "Percent:    %d%%\nVoltage:    %.1fV\nCurrent:    %4dmA\nRemaining:  %s" % (p, v, c, time_str)
        self.setInformativeText(info)
        logging.info(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {s}")

        if self.charge or p > WARN_30_PCT:
            self.low30_notified = False
        if self.charge or p > WARN_20_PCT:
            self.low20_notified = False
        if self.charge or p > WARN_10_PCT:
            self.low10_notified = False

        if p <= WARN_30_PCT and p > WARN_20_PCT and not self.charge and not self.low30_notified:
            self.low30_notified = True
            self.tray_icon.showMessage(
                "Battery Low",
                "Battery level is at 30%. Consider connecting the power adapter.",
                QSystemTrayIcon.Warning,
                5000
            )

        if p <= WARN_20_PCT and p > WARN_10_PCT and not self.charge and not self.low20_notified:
            self.low20_notified = True
            self.tray_icon.showMessage(
                "Battery Warning",
                "Battery level is at 20%. Please connect the power adapter.",
                QSystemTrayIcon.Warning,
                5000
            )

        if p <= WARN_10_PCT and not self.charge and not self.low10_notified:
            self.low10_notified = True
            if self.msgBox is None:
                self.counter = SHUTDOWN_COUNTDOWN_SEC
                self._timer.start(1000)
                self.msgBox = QMessageBox(
                    QMessageBox.NoIcon,
                    "Battery Warning",
                    "<p><strong>Battery level critical!<br>Please connect the power adapter.</strong>"
                )
                self.msgBox.setIconPixmap(QPixmap(_img("batteryQ.png")))
                self.msgBox.setInformativeText(f"auto shutdown after {SHUTDOWN_COUNTDOWN_SEC} seconds")
                self.msgBox.setStandardButtons(QMessageBox.NoButton)
                self.msgBox.show()
            
    def _stop_worker(self):
        self._worker.stop()
        self._thread.quit()
        self._thread.wait()

    def on_i2c_error(self, msg):
        logging.error(f"I2C error: {msg}. Retrying in 5s...")
        self.tray_icon.setToolTip("I2C error — sensor unavailable")
        if not self.i2c_error_notified:
            self.i2c_error_notified = True
            self.tray_icon.showMessage(
                "Sensor Error",
                "Cannot read battery sensor. Retrying...",
                QSystemTrayIcon.Critical,
                4000
            )

    def show_about(self):
        if self.about is None:
            self.about = QMessageBox(
                QMessageBox.NoIcon,
                "About",
                "<p><strong>Battery Monitor</strong><p>Version: v1.1<p>UPS HAT battery tray for Raspberry Pi"
            )
            self.about.setInformativeText('<a href="https://www.waveshare.com">WaveShare Official Website</a>')
            self.about.setIconPixmap(QPixmap(_img("logo.png")))
            self.about.setDefaultButton(None)
            self.about.exec()
            self.about = None

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    mw = MainWindow()
    sys.exit(app.exec_())