"""Loopback-only HTTP transport for the Fleet read model."""

from __future__ import annotations

import ipaddress
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from threading import RLock
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlsplit

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


@dataclass
class _Session:
    token: str
    issued_at: float
    last_seen: float
    read_only: bool = False


class FleetSessionManager:
    """Single-use bootstrap exchange and expiring Fleet sessions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        idle_seconds: float = 900,
        absolute_seconds: float = 14_400,
    ) -> None:
        self._clock = clock
        self._idle_seconds = idle_seconds
        self._absolute_seconds = absolute_seconds
        self.bootstrap_nonce = secrets.token_urlsafe(32)
        self._nonce_spent = False
        self._sessions: dict[str, _Session] = {}
        self._lock = RLock()

    def exchange(self, nonce: str) -> str:
        with self._lock:
            if self._nonce_spent or not secrets.compare_digest(
                nonce,
                self.bootstrap_nonce,
            ):
                raise PermissionError("fleet_bootstrap_invalid")
            self._nonce_spent = True
            now = self._clock()
            token = secrets.token_urlsafe(48)
            self._sessions[token] = _Session(token, now, now)
            return token

    def authenticate(self, cookie_header: str | None) -> _Session | None:
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except ValueError:
            return None
        morsel = cookie.get("torq_fleet_session")
        if morsel is None:
            return None
        with self._lock:
            session = self._sessions.get(morsel.value)
            if session is None:
                return None
            now = self._clock()
            if (
                now - session.last_seen >= self._idle_seconds
                or now - session.issued_at >= self._absolute_seconds
            ):
                self._sessions.pop(session.token, None)
                return None
            session.last_seen = now
            return session

    def downgrade(self, session: _Session) -> None:
        with self._lock:
            session.read_only = True

    def rotate(self, session: _Session) -> str:
        with self._lock:
            self._sessions.pop(session.token, None)
            now = self._clock()
            token = secrets.token_urlsafe(48)
            self._sessions[token] = _Session(
                token,
                now,
                now,
                read_only=session.read_only,
            )
            return token


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
    sessions: FleetSessionManager | None = None,
) -> ThreadingHTTPServer:
    if not _loopback_host(host):
        raise ValueError("fleet_loopback_required")
    if not 0 <= port <= 65535:
        raise ValueError("fleet_port_invalid")
    session_manager = sessions or FleetSessionManager()

    class Handler(BaseHTTPRequestHandler):
        server_version = "TORQFleet/1"

        def do_GET(self) -> None:  # noqa: N802
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
            if self.path == "/healthz":
                self._json(200, {"status": "ok"})
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/bootstrap":
                values = parse_qs(parsed.query, keep_blank_values=True)
                nonce_values = values.get("nonce", [])
                if len(nonce_values) != 1:
                    self._json(403, {"status": "blocked", "finding": "fleet_bootstrap_invalid"})
                    return
                try:
                    token = session_manager.exchange(nonce_values[0])
                except PermissionError as exc:
                    self._json(403, {"status": "blocked", "finding": str(exc)})
                    return
                self.send_response(303)
                self.send_header("Location", "/api/v1/fleet")
                self.send_header(
                    "Set-Cookie",
                    "torq_fleet_session="
                    + token
                    + "; HttpOnly; SameSite=Strict; Path=/api/v1",
                )
                self._security_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            session = session_manager.authenticate(self.headers.get("Cookie"))
            if session is None:
                self._json(401, {"status": "blocked", "finding": "fleet_session_required"})
                return
            if parsed.path == "/api/v1/fleet":
                snapshot = projector.snapshot()
                run = snapshot.get("run")
                if isinstance(run, Mapping) and run.get("workflow_state") in {
                    "closed",
                    "abandoned",
                }:
                    session_manager.downgrade(session)
                self._json(200, snapshot)
                return
            self._json(404, {"status": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
            session = session_manager.authenticate(self.headers.get("Cookie"))
            if session is None:
                self._json(401, {"status": "blocked", "finding": "fleet_session_required"})
                return
            if session.read_only:
                self._json(409, {"status": "blocked", "finding": "fleet_session_read_only"})
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
            rotated = session_manager.rotate(session)
            self._json(
                202,
                {"status": "accepted", "context": result},
                session_token=rotated,
            )

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

        def _json(
            self,
            status: int,
            value: object,
            *,
            session_token: str | None = None,
        ) -> None:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            if session_token is not None:
                self.send_header(
                    "Set-Cookie",
                    "torq_fleet_session="
                    + session_token
                    + "; HttpOnly; SameSite=Strict; Path=/api/v1",
                )
            self._security_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def _security_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("Referrer-Policy", "no-referrer")

    server = ThreadingHTTPServer((host, port), Handler)
    setattr(server, "fleet_bootstrap_nonce", session_manager.bootstrap_nonce)
    return server


__all__ = ["FleetSessionManager", "create_fleet_server"]
