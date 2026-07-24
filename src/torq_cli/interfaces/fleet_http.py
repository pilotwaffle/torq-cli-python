"""Loopback-only HTTP transport for the Fleet read model."""

from __future__ import annotations

import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from torq_cli.application.fleet import FleetProjector


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def create_fleet_server(
    projector: FleetProjector,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    if not _loopback_host(host):
        raise ValueError("fleet_loopback_required")
    if not 0 <= port <= 65535:
        raise ValueError("fleet_port_invalid")

    class Handler(BaseHTTPRequestHandler):
        server_version = "TORQFleet/1"

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/healthz":
                snapshot = projector.snapshot()
                body = {
                    "status": "ok",
                    "verification": snapshot["verification"],
                }
                self._json(200, body)
                return
            if self.path == "/api/v1/fleet":
                self._json(200, projector.snapshot())
                return
            self._json(404, {"status": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            self._json(405, {"status": "read_only"})

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _json(self, status: int, value: object) -> None:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.end_headers()
            self.wfile.write(encoded)

    return ThreadingHTTPServer((host, port), Handler)


__all__ = ["create_fleet_server"]
