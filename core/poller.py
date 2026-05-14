"""
Background INA219 polling thread.

Architecture
------------
PollerWorker  — a QObject that owns a QTimer and lives in PollerThread.
PollerThread  — a QThread that runs its own Qt event loop (exec()).

The QTimer fires in the worker's thread context, so all smbus2 I/O
happens off the main thread.  Results are emitted via Qt signals, which
Qt automatically routes as QueuedConnection across thread boundaries,
giving us thread-safe delivery to the main-thread slots (BatteryManager,
tray, window) without any manual locking in application code.

Shutdown
--------
Call stop(), then wait() on the thread.  stop() posts a quit() to the
thread's event loop; the thread then exits cleanly and the worker's
timer is garbage-collected.
"""

import logging

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal, pyqtSlot

from core.ina219 import INA219, INA219Reading

logger = logging.getLogger(__name__)


class _PollerWorker(QObject):
    """Lives in the polling thread; owns the QTimer and calls INA219.read()."""

    reading_ready = pyqtSignal(object)   # INA219Reading

    def __init__(self, ina219: INA219, interval_ms: int) -> None:
        super().__init__()
        self._ina219 = ina219
        self._interval_ms = interval_ms
        self._timer: QTimer | None = None

    @pyqtSlot()
    def start_polling(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(self._interval_ms)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        logger.debug("Polling started — interval=%d ms", self._interval_ms)

    @pyqtSlot()
    def _poll(self) -> None:
        reading = self._ina219.read()
        if not reading.is_valid:
            logger.warning("Invalid reading: %s", reading.error)
        self.reading_ready.emit(reading)


class PollerThread(QThread):
    """
    QThread wrapper that manages the lifetime of _PollerWorker.

    Usage:
        poller = PollerThread(ina219, interval_ms=2000)
        poller.reading_ready.connect(some_slot)
        poller.start()
        ...
        poller.stop()
        poller.wait(3000)
    """

    reading_ready = pyqtSignal(object)   # re-exported from worker

    def __init__(self, ina219: INA219, interval_ms: int, parent=None) -> None:
        super().__init__(parent)
        self._worker = _PollerWorker(ina219, interval_ms)
        # Move worker to this thread so its timer fires in our event loop
        self._worker.moveToThread(self)
        # Wire internal signal through to our public signal
        self._worker.reading_ready.connect(self.reading_ready)
        # Start polling as soon as the thread's event loop is running
        self.started.connect(self._worker.start_polling)

    def run(self) -> None:
        logger.debug("Poller thread started (tid=%s)", int(self.currentThreadId()))
        self.exec()   # blocks here, running the Qt event loop
        logger.debug("Poller thread exiting")

    def stop(self) -> None:
        """Request the thread's event loop to exit."""
        self.quit()
