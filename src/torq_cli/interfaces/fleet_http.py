"""Loopback-only HTTP transport for the Fleet read model."""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol

from torq_cli.application.fleet import FleetProjector
from torq_cli.application.orchestrator import OrchestrationBlocked
from torq_cli.core.redaction import RedactionBlocked


class ContextInjector(Protocol):
    def inject(
        self,
        content: str,
        *,
        target_role: str | None = None,
        media_type: str = "text/plain",
        source_name: str | None = None,
    ) -> Mapping[str, Any]: ...


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
    context_injector: ContextInjector | None = None,
) -> ThreadingHTTPServer:
    if not _loopback_host(host):
        raise ValueError("fleet_loopback_required")
    if not 0 <= port <= 65535:
        raise ValueError("fleet_port_invalid")

    class Handler(BaseHTTPRequestHandler):
        server_version = "TORQFleet/1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
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
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
            if self.path != "/api/v1/context" or context_injector is None:
                self._json(405, {"status": "read_only"})
                return
            address = self.server.server_address
            if not isinstance(address, tuple) or len(address) < 2:
                self._json(500, {"status": "internal_error"})
                return
            port_number = int(address[1])
            allowed_origins = {
                f"http://127.0.0.1:{port_number}",
                f"http://localhost:{port_number}",
                f"http://[::1]:{port_number}",
            }
            if self.headers.get("Origin") not in allowed_origins:
                self._json(403, {"status": "blocked", "finding": "fleet_origin_denied"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 1_048_576:
                    raise ValueError("context_size_invalid")
                raw = self.rfile.read(length)
                payload = json.loads(raw)
                if not isinstance(payload, Mapping) or set(payload) - {
                    "content", "target_role", "media_type", "source_name",
                }:
                    raise ValueError("context_request_invalid")
                content = payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("context_request_invalid")
                target_role = payload.get("target_role")
                media_type = payload.get("media_type", "text/plain")
                source_name = payload.get("source_name")
                if target_role is not None and not isinstance(target_role, str):
                    raise ValueError("context_request_invalid")
                if not isinstance(media_type, str):
                    raise ValueError("context_request_invalid")
                if source_name is not None and not isinstance(source_name, str):
                    raise ValueError("context_request_invalid")
                result = context_injector.inject(
                    content,
                    target_role=target_role,
                    media_type=media_type,
                    source_name=source_name,
                )
            except RedactionBlocked as exc:
                self._json(400, {"status": "blocked", "finding": str(exc)})
                return
            except OrchestrationBlocked as exc:
                self._json(400, {"status": "blocked", "finding": str(exc)})
                return
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                self._json(400, {
                    "status": "blocked",
                    "finding": "context_request_invalid",
                })
                return
            self._json(202, {"status": "accepted", "context": result})

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _host_allowed(self) -> bool:
            values = self.headers.get_all("Host", failobj=[])
            address = self.server.server_address
            if len(values) != 1 or not isinstance(address, tuple) or len(address) < 2:
                return False
            port_number = int(address[1])
            allowed = {
                f"127.0.0.1:{port_number}",
                f"localhost:{port_number}",
                f"[::1]:{port_number}",
            }
            return values[0].casefold() in allowed

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
