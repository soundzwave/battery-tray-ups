"""
Structured logging setup.

Writes JSON-structured records to a rotating file and human-readable
lines to stderr. Both handlers share the same root logger.
"""

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Any


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line for log aggregators."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _StderrFormatter(logging.Formatter):
    _LEVEL_COLORS = {
        "DEBUG":    "\033[36m",   # cyan
        "INFO":     "\033[32m",   # green
        "WARNING":  "\033[33m",   # yellow
        "ERROR":    "\033[31m",   # red
        "CRITICAL": "\033[35m",   # magenta
    }
    _RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self._LEVEL_COLORS.get(record.levelname, "")
        ts = self.formatTime(record, "%H:%M:%S")
        prefix = f"{color}{record.levelname[0]}{self._RESET}"
        line = f"{ts} {prefix} [{record.name}] {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(
    level: str = "INFO",
    log_file: str = "logs/battery-monitor.log",
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any handlers added by Qt or earlier imports
    root.handlers.clear()

    # Rotating file handler — JSON format for structured querying
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    fh.setFormatter(_JsonFormatter())
    root.addHandler(fh)

    # Stderr handler — human-readable with ANSI colour
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(_StderrFormatter())
    root.addHandler(sh)

    # Silence noisy third-party loggers unless we are at DEBUG
    if numeric_level > logging.DEBUG:
        for name in ("smbus2", "pyqtgraph", "urllib3"):
            logging.getLogger(name).setLevel(logging.WARNING)
