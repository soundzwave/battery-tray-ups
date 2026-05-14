"""
Main application window.

Tabs
----
Overview     — current readings displayed as large labelled values
Charts       — real-time pyqtgraph plots (voltage, current, power)
Events       — power event log table
Diagnostics  — I2C info and raw register dump commands

The window is designed to be lightweight:
  - Charts update only when the window is visible (skip_if_hidden flag)
  - Data is stored in the BatteryManager ring buffer, not duplicated here
  - PyQtGraph plots are the most CPU-intensive part; kept to 3 traces
"""

import logging
import math
import subprocess
import time
from collections import deque
from typing import Optional

import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSlot, QTimer
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QPlainTextEdit, QPushButton, QSizePolicy,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.battery import BatteryState, ChargeState
from core.power_events import PowerEvent

logger = logging.getLogger(__name__)

# Colour constants
_GREEN  = "#4caf50"
_YELLOW = "#ffc107"
_RED    = "#f44336"
_BLUE   = "#2196f3"
_GREY   = "#9e9e9e"
_BG     = "#1e1e2e"
_CARD   = "#2a2a3e"
_TEXT   = "#cdd6f4"

pg.setConfigOptions(antialias=True, foreground=_TEXT, background=_BG)


def _pct_color(pct: float) -> str:
    if pct > 40:
        return _GREEN
    if pct > 20:
        return _YELLOW
    return _RED


class _MetricCard(QFrame):
    """A bordered card displaying a label + large value + unit."""

    def __init__(self, label: str, unit: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(f"background:{_CARD}; border-radius:6px; padding:4px;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self._label = QLabel(label)
        self._label.setStyleSheet(f"color:{_GREY}; font-size:10px;")
        layout.addWidget(self._label)

        self._value = QLabel("—")
        font = QFont()
        font.setPointSize(18)
        font.setBold(True)
        self._value.setFont(font)
        self._value.setStyleSheet(f"color:{_TEXT};")
        layout.addWidget(self._value)

        if unit:
            self._unit = QLabel(unit)
            self._unit.setStyleSheet(f"color:{_GREY}; font-size:9px;")
            layout.addWidget(self._unit)

    def set_value(self, text: str, color: str = _TEXT) -> None:
        self._value.setText(text)
        self._value.setStyleSheet(f"color:{color};")


class _OverviewTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{_BG};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        # Status banner
        self._status = QLabel("Initialising…")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            "font-size:13px; font-weight:bold; padding:6px; border-radius:4px;"
        )
        layout.addWidget(self._status)

        # Main metrics grid
        grid = QGridLayout()
        grid.setSpacing(8)

        self._voltage = _MetricCard("Voltage", "V")
        self._current = _MetricCard("Current", "mA")
        self._power   = _MetricCard("Power", "mW")
        self._percent = _MetricCard("Charge", "%")
        self._runtime = _MetricCard("Est. Runtime", "")
        self._health  = _MetricCard("Battery Health", "%")
        self._cycles  = _MetricCard("Cycle Count (est.)", "")
        self._shunt   = _MetricCard("Shunt Voltage", "mV")

        grid.addWidget(self._percent, 0, 0)
        grid.addWidget(self._voltage, 0, 1)
        grid.addWidget(self._current, 0, 2)
        grid.addWidget(self._power,   1, 0)
        grid.addWidget(self._runtime, 1, 1)
        grid.addWidget(self._health,  1, 2)
        grid.addWidget(self._cycles,  2, 0)
        grid.addWidget(self._shunt,   2, 1)

        layout.addLayout(grid)
        layout.addStretch()

    def update_state(self, state: BatteryState) -> None:
        if not state.is_valid:
            self._status.setText(f"Sensor error: {state.error}")
            self._status.setStyleSheet(
                f"font-size:13px; font-weight:bold; padding:6px; "
                f"border-radius:4px; background:{_RED}20; color:{_RED};"
            )
            return

        # Status banner
        state_labels = {
            ChargeState.CHARGING:    ("Charging",    _BLUE),
            ChargeState.DISCHARGING: ("Discharging", _YELLOW),
            ChargeState.FULL:        ("Full",         _GREEN),
            ChargeState.UNKNOWN:     ("Unknown",      _GREY),
        }
        label, colour = state_labels.get(state.charge_state, ("Unknown", _GREY))
        ext = " — External power" if state.is_external_power else " — On battery"
        self._status.setText(label + ext)
        self._status.setStyleSheet(
            f"font-size:13px; font-weight:bold; padding:6px; "
            f"border-radius:4px; background:{colour}33; color:{colour};"
        )

        pct = state.percentage
        self._percent.set_value(f"{pct:.1f}", _pct_color(pct))
        self._voltage.set_value(f"{state.voltage_v:.3f}")
        self._current.set_value(f"{state.current_ma:+.1f}")
        self._power.set_value(f"{state.power_mw:.1f}")
        self._health.set_value(f"{state.health_percent:.1f}", _GREEN if state.health_percent > 80 else _YELLOW)
        self._cycles.set_value(str(state.cycle_count_estimate))
        self._shunt.set_value(f"{state.shunt_voltage_mv:.2f}")

        if state.estimated_runtime_min is not None:
            h, m = divmod(int(state.estimated_runtime_min), 60)
            rt   = f"{h}h {m:02d}m" if h else f"{m} min"
        else:
            rt = "—" if not state.is_external_power else "∞"
        self._runtime.set_value(rt)


class _ChartsTab(QWidget):
    def __init__(self, chart_points: int = 300, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{_BG};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        self._maxlen = chart_points
        self._times: deque[float]   = deque(maxlen=chart_points)
        self._volts: deque[float]   = deque(maxlen=chart_points)
        self._amps:  deque[float]   = deque(maxlen=chart_points)
        self._watts: deque[float]   = deque(maxlen=chart_points)
        self._pcts:  deque[float]   = deque(maxlen=chart_points)
        self._t0:    Optional[float] = None

        self._plot_v = self._make_plot("Voltage (V)",   _GREEN,  "V")
        self._plot_a = self._make_plot("Current (mA)",  _BLUE,   "mA")
        self._plot_w = self._make_plot("Power (mW)",    _YELLOW, "mW")
        self._plot_p = self._make_plot("Charge (%)",    _GREEN,  "%")

        layout.addWidget(self._plot_v["widget"])
        layout.addWidget(self._plot_a["widget"])
        layout.addWidget(self._plot_w["widget"])
        layout.addWidget(self._plot_p["widget"])

    def _make_plot(self, title: str, color: str, unit: str) -> dict:
        pw = pg.PlotWidget(title=title)
        pw.setBackground(_BG)
        pw.getPlotItem().getAxis("left").setLabel(unit, color=color)
        pw.getPlotItem().getAxis("bottom").setLabel("Time (s)", color=_GREY)
        pw.getPlotItem().showGrid(x=True, y=True, alpha=0.2)
        pw.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        pw.setMinimumHeight(100)
        curve = pw.plot(pen=pg.mkPen(color=color, width=1.5))
        return {"widget": pw, "curve": curve}

    def update_state(self, state: BatteryState) -> None:
        if not state.is_valid:
            return
        if self._t0 is None:
            self._t0 = state.timestamp

        t = state.timestamp - self._t0
        self._times.append(t)
        self._volts.append(state.voltage_v)
        self._amps.append(state.current_ma)
        self._watts.append(state.power_mw)
        self._pcts.append(state.percentage)

        # Skip expensive render if tab is hidden
        if not self.isVisible():
            return

        ts = list(self._times)
        self._plot_v["curve"].setData(ts, list(self._volts))
        self._plot_a["curve"].setData(ts, list(self._amps))
        self._plot_w["curve"].setData(ts, list(self._watts))
        self._plot_p["curve"].setData(ts, list(self._pcts))


class _EventsTab(QWidget):
    _COLUMNS = ["Time", "Event", "Message", "Voltage", "%"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{_BG}; color:{_TEXT};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        self._table = QTableWidget(0, len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(
            f"QTableWidget {{ background:{_CARD}; gridline-color:#444; }}"
            f"QHeaderView::section {{ background:{_BG}; color:{_TEXT}; }}"
        )
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table)

    def add_event(self, event: PowerEvent) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)
        cells = [
            event.iso_time(),
            event.kind,
            event.message,
            f"{event.voltage_v:.3f} V" if event.voltage_v is not None else "—",
            f"{event.percentage:.0f} %"  if event.percentage  is not None else "—",
        ]
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            item.setForeground(QColor(_TEXT))
            self._table.setItem(row, col, item)

        self._table.scrollToBottom()


class _DiagnosticsTab(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setStyleSheet(f"background:{_BG}; color:{_TEXT};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        info_label = QLabel(
            "I2C diagnostics — run in a terminal or via the buttons below.\n"
            "Requires i2c-tools: sudo apt install i2c-tools"
        )
        info_label.setStyleSheet(f"color:{_GREY}; font-size:10px;")
        layout.addWidget(info_label)

        btn_row = QHBoxLayout()
        for label, cmd in [
            ("i2cdetect", "i2cdetect -y 3"),
            ("i2cdump 0x43", "i2cdump -y 3 0x43"),
            ("Check perms", "ls -l /dev/i2c-3"),
        ]:
            btn = QPushButton(label)
            btn.setStyleSheet(f"background:{_CARD}; color:{_TEXT}; padding:4px 8px; border-radius:3px;")
            btn.clicked.connect(lambda _, c=cmd: self._run_cmd(c))
            btn_row.addWidget(btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._output = QPlainTextEdit()
        self._output.setReadOnly(True)
        self._output.setStyleSheet(
            f"background:#111; color:#0f0; font-family:monospace; font-size:10px;"
        )
        layout.addWidget(self._output)

    def _run_cmd(self, cmd: str) -> None:
        self._output.appendPlainText(f"$ {cmd}")
        try:
            result = subprocess.run(
                cmd.split(), capture_output=True, text=True, timeout=5
            )
            out = result.stdout or result.stderr or "(no output)"
        except FileNotFoundError:
            out = "Command not found — install i2c-tools: sudo apt install i2c-tools"
        except subprocess.TimeoutExpired:
            out = "Command timed out"
        except Exception as exc:
            out = str(exc)
        self._output.appendPlainText(out)
        self._output.appendPlainText("")


class MainWindow(QMainWindow):
    def __init__(
        self,
        history_size: int = 1800,
        chart_points: int = 300,
        no_tray: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Battery Monitor — Waveshare UPS HAT D")
        self.setMinimumSize(700, 500)
        self.setStyleSheet(f"background:{_BG}; color:{_TEXT};")

        self._no_tray = no_tray

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self._tabs = QTabWidget()
        self._tabs.setStyleSheet(
            f"QTabWidget::pane {{ border:0; background:{_BG}; }}"
            f"QTabBar::tab {{ background:{_CARD}; color:{_TEXT}; padding:6px 14px; }}"
            f"QTabBar::tab:selected {{ background:{_BG}; border-top:2px solid {_BLUE}; }}"
        )
        layout.addWidget(self._tabs)

        self._overview    = _OverviewTab()
        self._charts      = _ChartsTab(chart_points)
        self._events_tab  = _EventsTab()
        self._diagnostics = _DiagnosticsTab()

        self._tabs.addTab(self._overview,    "Overview")
        self._tabs.addTab(self._charts,      "Charts")
        self._tabs.addTab(self._events_tab,  "Events")
        self._tabs.addTab(self._diagnostics, "Diagnostics")

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(object)
    def update_state(self, state: BatteryState) -> None:
        self._overview.update_state(state)
        self._charts.update_state(state)

    @pyqtSlot(object)
    def add_event(self, event: PowerEvent) -> None:
        self._events_tab.add_event(event)

    # ── Window close ─────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self._no_tray:
            event.accept()
        else:
            # Hide to tray instead of closing; user can Quit from tray menu
            self.hide()
            event.ignore()
