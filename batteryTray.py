#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import sys
import smbus
import time
import logging
import signal
import INA219
import PyQt5
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

BATTERY_CAPACITY_MAH = 4000  # Set your battery capacity here

ina=INA219.INA219(addr=0x43)


def format_time_remaining(minutes):
    if minutes >= 60:
        return "~%dh %02dm" % (minutes // 60, minutes % 60)
    return "~%dm" % minutes
        
class Worker(QObject):
    trayMessage = pyqtSignal(float, float)

    def run(self):
        while True:
            bus_voltage = ina.getBusVoltage_V()             # voltage on V- (load side)
            current = -int(ina.getCurrent_mA())                   # current in mA
            self.trayMessage.emit(bus_voltage, current)
            time.sleep(1);
        
class MainWindow(QMessageBox):
    # Override the class constructor
    def __init__(self):
        # Be sure to call the super class method
        self.charge = 0
        self.tray_icon = None
        self.msgBox = None
        self.about = None
        self.counter = 0
        self.low20_notified = False
    
        QMessageBox.__init__(self)
        self.setWindowTitle("Status")  # Set a title
        self.setText("Battery Monitor Demo");
        
        # Init QSystemTrayIcon
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon("images/battery.png"))

        '''
            Define and add steps to work with the system tray icon
            show - show window
            Status - Status window
            exit - exit from application
        '''
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

        self._thread = QThread(self)
        self._worker = Worker()
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.started.connect(self._worker.run)
        self._worker.trayMessage.connect(self.refresh)
        self._thread.start()
        self._timer = QTimer(self,timeout=self.on_timeout)
        self._timer.stop()
    
    def on_timeout(self):
        self.counter -= 1
        if(self.counter > 0):  #countdown
            if(self.charge == 1):
                self.msgBox.hide()
                self.msgBox.close()
                self._timer.stop()
                self.msgBox = None
            else:
                self.msgBox.setInformativeText("auto shutdown after " +str(int(self.counter)) + " seconds");
                self.msgBox.show()
        else:                  #timeout
            address = os.popen("i2cdetect -y -r 1 0x2d 0x2d | egrep '2d' | awk '{print $2}'").read()
            if(address=='2d\n'):
                #print("If charged, the system can be powered on again.")
                #write 0x55 to 0x01 register of 0x2d Address device
                os.popen("i2cset -y 1 0x2d 0x01 0x55")
            os.system("sudo poweroff")

    def refresh(self, v, c):
        if(c > 50):self.charge = 1
        else:self.charge = 0
        
        p = int((v - 3)/1.2*100)    #Battery Percentage
        if(p > 100):p = 100
        if(p < 0):p = 0
        img = "images/battery." + str(int(p / 10 + self.charge * 11)) + ".png"
        self.tray_icon.setIcon(QIcon(img))
        self.setIconPixmap(QPixmap(img))
        s = "%d%%  %.1fV  %dmA" % (p,v,c)
        self.tray_icon.setToolTip(s)
        info = "Percent:    %d%%\nVoltage:    %.1fV\nCurrent:    %4dmA" % (p,v,c)
        self.setInformativeText(info);
        localTime = time.localtime(time.time())
        logging.info(f"{localTime.tm_year:04d}-{localTime.tm_mon:02d}-{localTime.tm_mday:02d} {localTime.tm_hour:02d}:{localTime.tm_min:02d}:{localTime.tm_sec:02d}  {s}")
        if(self.charge == 1 or p > 20):
            self.low20_notified = False

        if(p <= 20 and p > 10 and self.charge == 0 and not self.low20_notified):
            self.low20_notified = True
            self.tray_icon.showMessage(
                "Battery Warning",
                "Battery level is at 20%. Please connect the power adapter.",
                QSystemTrayIcon.Warning,
                5000
            )

        if(p <= 10 and self.charge == 0):
            if(self.msgBox == None):
                self.counter = 60
                self._timer.start(1000)
                self.msgBox = QMessageBox(QMessageBox.NoIcon,'Battery Warning',"<p><strong>The battery level is below<br>Please connect in the power adapter</strong>")
                self.msgBox.setIconPixmap(QPixmap("images/batteryQ.png"))
                self.msgBox.setInformativeText("auto shutdown after 60 seconds");
                self.msgBox.setStandardButtons(QMessageBox.NoButton);
                self.msgBox.exec()
            
    def show_about(self):
        if(self.about == None):
            self.about = QMessageBox(QMessageBox.NoIcon,'About',"<p><strong>Battery Monitor Demo</strong><p>Version: v1.0<p>It's a battery Display By waveshare\n")
            self.about.setInformativeText("<a href=\"https://www.waveshare.com\">WaveShare Official Website</a>");
            self.about.setIconPixmap(QPixmap("images/logo.png"))
            self.about.setDefaultButton(None)
            self.about.exec()
            self.about = None

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    mw = MainWindow()
    sys.exit(app.exec_())