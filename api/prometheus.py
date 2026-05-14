"""
Prometheus HTTP exposition server.

Runs in a daemon thread completely independent of the Qt event loop.
Uses prometheus_client's built-in WSGI server which handles concurrent
scrapes from Prometheus without locks — the global registry is thread-safe.

Endpoint:  GET /metrics  — standard Prometheus text format
           GET /health   — minimal JSON liveness probe
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path in ("/metrics", "/metrics/"):
            self._serve_metrics()
        elif self.path in ("/health", "/health/"):
            self._serve_health()
        else:
            self.send_error(404, "Not found")

    def _serve_metrics(self) -> None:
        output = generate_latest()
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE_LATEST)
        self.send_header("Content-Length", str(len(output)))
        self.end_headers()
        self.wfile.write(output)

    def _serve_health(self) -> None:
        body = json.dumps({"status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # Route HTTP access log through our structured logger at DEBUG level
        logger.debug("prometheus: " + fmt, *args)


class PrometheusServer:
    """
    Lifecycle manager for the exposition HTTP server.

    Call start() once after constructing.  The server thread is a daemon
    thread so it will not prevent application exit.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 9105) -> None:
        self._host   = host
        self._port   = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        try:
            self._server = HTTPServer((self._host, self._port), _Handler)
        except OSError as exc:
            logger.error(
                "Cannot start Prometheus server on %s:%d — %s",
                self._host, self._port, exc,
            )
            return

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="prometheus-http",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Prometheus metrics available at http://%s:%d/metrics",
            self._host, self._port,
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            logger.debug("Prometheus server stopped")
