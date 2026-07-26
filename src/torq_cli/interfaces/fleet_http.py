"""Loopback-only HTTP transport for the Fleet read model."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from importlib.resources import files
from threading import RLock
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urlsplit

from torq_cli.application.fleet import FleetProjector
from torq_cli.application.fleet_controls import FleetControlService
from torq_cli.application.orchestrator import OrchestrationBlocked
from torq_cli.core.redaction import RedactionBlocked


_FLEET_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/fleet.css": ("fleet.css", "text/css; charset=utf-8"),
    "/assets/fleet.js": ("fleet.js", "text/javascript; charset=utf-8"),
}


def _load_fleet_assets() -> dict[str, tuple[bytes, str]]:
    root = files("torq_cli").joinpath("data", "fleet")
    return {
        route: (root.joinpath(filename).read_bytes(), content_type)
        for route, (filename, content_type) in _FLEET_ASSETS.items()
    }


class ContextInjector(Protocol):
    @property
    def root(self) -> Path: ...

    def inject(
        self,
        content: str,
        *,
        target_role: str | None = None,
        media_type: str = "text/plain",
        source_name: str | None = None,
        confirm_direct: bool = False,
    ) -> Mapping[str, Any]: ...

    def inject_artifact(
        self,
        content: bytes,
        *,
        target_role: str | None = None,
        media_type: str,
        source_name: str,
        confirm_direct: bool = False,
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
        wall_clock: Callable[[], float] = time.time,
        idle_seconds: float = 900,
        absolute_seconds: float = 14_400,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
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

    def expires_at(self, session: _Session) -> str:
        """Return the effective idle/absolute expiry as an RFC 3339 timestamp."""
        with self._lock:
            now = self._clock()
            remaining = max(
                0.0,
                min(
                    self._idle_seconds - (now - session.last_seen),
                    self._absolute_seconds - (now - session.issued_at),
                ),
            )
            return (
                datetime.fromtimestamp(
                    self._wall_clock() + remaining,
                    timezone.utc,
                )
                .isoformat()
                .replace("+00:00", "Z")
            )

    def rotate(self, session: _Session) -> str:
        with self._lock:
            self._sessions.pop(session.token, None)
            now = self._clock()
            token = secrets.token_urlsafe(48)
            self._sessions[token] = _Session(
                token,
                session.issued_at,
                now,
                read_only=session.read_only,
            )
            return token

    def claim_mutation(self, cookie_header: str | None) -> _Session | None:
        """Atomically consume a write session so concurrent POSTs cannot fork it."""
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
            session = self._sessions.pop(morsel.value, None)
            if session is None:
                return None
            now = self._clock()
            if (
                now - session.last_seen >= self._idle_seconds
                or now - session.issued_at >= self._absolute_seconds
            ):
                return None
            session.last_seen = now
            return session


def _loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("context_request_duplicate_field")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    del value
    raise ValueError("context_request_non_finite")


def create_fleet_server(
    projector: FleetProjector,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    context_injector: ContextInjector | None = None,
    sessions: FleetSessionManager | None = None,
    control_service: FleetControlService | None = None,
    operational_state_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    if not _loopback_host(host):
        raise ValueError("fleet_loopback_required")
    if not 0 <= port <= 65535:
        raise ValueError("fleet_port_invalid")
    if (
        context_injector is not None
        and context_injector.root.resolve() != projector.run_root.resolve()
    ):
        raise ValueError("fleet_control_run_mismatch")
    session_manager = sessions or FleetSessionManager()
    fleet_assets = _load_fleet_assets()

    def operational_annotations() -> list[dict[str, str]]:
        raw_operational = (
            operational_state_provider()
            if operational_state_provider is not None
            else projector.operational_state
        )
        if not isinstance(raw_operational, Mapping):
            return []
        observed_at = raw_operational.get("heartbeat_at")
        raw_orphans = raw_operational.get("orphaned_roles", ())
        if not isinstance(observed_at, str) or not isinstance(
            raw_orphans, (list, tuple)
        ):
            return []
        annotations = [
            {
                "kind": "orphaned",
                "scope": str(role),
                "observed_at": observed_at,
                "source": "supervisor",
            }
            for role in raw_orphans
        ]
        if raw_orphans:
            annotations.append(
                {
                    "kind": "recovery_required",
                    "scope": "run",
                    "observed_at": observed_at,
                    "source": "supervisor",
                }
            )
        return annotations

    controls = control_service or FleetControlService(
        context_available=context_injector is not None,
        annotation_provider=operational_annotations,
    )

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
            asset = fleet_assets.get(parsed.path)
            if asset is not None:
                self._asset(*asset)
                return
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
                self.send_header("Location", "/")
                self.send_header(
                    "Set-Cookie",
                    "torq_fleet_session="
                    + token
                    + "; HttpOnly; SameSite=Strict; Path=/",
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
                envelope = controls.envelope(
                    snapshot,
                    session_write_capable=not session.read_only,
                    expires_at=session_manager.expires_at(session),
                    read_only_reason=(
                        "session_read_only" if session.read_only else None
                    ),
                )
                self._json(200, envelope)
                return
            self._json(404, {"status": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
            if self.path != "/api/v1/context" or context_injector is None:
                self._json(405, {"status": "read_only"})
                return
            session = session_manager.claim_mutation(self.headers.get("Cookie"))
            if session is None:
                self._json(401, {"status": "blocked", "finding": "fleet_session_required"})
                return
            if session.read_only:
                token = session_manager.rotate(session)
                self._json(
                    409,
                    {"status": "blocked", "finding": "fleet_session_read_only"},
                    session_token=token,
                )
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
            origin_values = self.headers.get_all("Origin", failobj=[])
            if len(origin_values) != 1 or origin_values[0] not in allowed_origins:
                token = session_manager.rotate(session)
                self._json(
                    403,
                    {"status": "blocked", "finding": "fleet_origin_denied"},
                    session_token=token,
                )
                return
            snapshot = projector.snapshot()
            envelope = controls.envelope(
                snapshot,
                session_write_capable=True,
                expires_at=session_manager.expires_at(session),
            )
            context_eligibility = envelope["eligibility"]["context"]
            if not context_eligibility["eligible"]:
                token = session_manager.rotate(session)
                self._json(
                    409,
                    {
                        "status": "blocked",
                        "finding": context_eligibility["reason"],
                    },
                    session_token=token,
                )
                return
            try:
                content_types = self.headers.get_all("Content-Type", failobj=[])
                if len(content_types) != 1 or content_types[0].casefold() not in {
                    "application/json",
                    "application/json; charset=utf-8",
                }:
                    raise ValueError("context_content_type_invalid")
                if self.headers.get_all("Transfer-Encoding", failobj=[]):
                    raise ValueError("context_transfer_encoding_denied")
                lengths = self.headers.get_all("Content-Length", failobj=[])
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise ValueError("context_size_invalid")
                length = int(lengths[0])
                if length <= 0 or length > 1_500_000:
                    raise ValueError("context_size_invalid")
                raw = self.rfile.read(length)
                payload = json.loads(
                    raw,
                    object_pairs_hook=_unique_json_object,
                    parse_constant=_reject_json_constant,
                )
                if not isinstance(payload, Mapping):
                    raise ValueError("context_request_invalid")
                allowed = {
                    "input_kind",
                    "content",
                    "content_base64",
                    "target_role",
                    "media_type",
                    "source_name",
                    "confirm_direct",
                }
                if set(payload) - allowed:
                    raise ValueError("context_request_invalid")
                target_role = payload.get("target_role")
                confirm_direct = payload.get("confirm_direct", False)
                source_name = payload.get("source_name")
                if target_role is not None and not isinstance(target_role, str):
                    raise ValueError("context_request_invalid")
                if not isinstance(confirm_direct, bool):
                    raise ValueError("context_request_invalid")
                input_kind = payload.get("input_kind", "inline_text")
                if input_kind == "inline_text":
                    content = payload.get("content")
                    if (
                        not isinstance(content, str)
                        or "content_base64" in payload
                        or source_name is not None and not isinstance(source_name, str)
                    ):
                        raise ValueError("context_request_invalid")
                    media_type = payload.get("media_type", "text/plain")
                    if not isinstance(media_type, str):
                        raise ValueError("context_request_invalid")
                    result = context_injector.inject(
                        content,
                        target_role=target_role,
                        media_type=media_type,
                        source_name=source_name,
                        confirm_direct=confirm_direct,
                    )
                elif input_kind == "file":
                    encoded = payload.get("content_base64")
                    media_type = payload.get("media_type")
                    if (
                        not isinstance(encoded, str)
                        or "content" in payload
                        or not isinstance(media_type, str)
                        or not isinstance(source_name, str)
                    ):
                        raise ValueError("context_request_invalid")
                    try:
                        decoded = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError) as exc:
                        raise ValueError("context_base64_invalid") from exc
                    if base64.b64encode(decoded).decode("ascii") != encoded:
                        raise ValueError("context_base64_invalid")
                    result = context_injector.inject_artifact(
                        decoded,
                        target_role=target_role,
                        media_type=media_type,
                        source_name=source_name,
                        confirm_direct=confirm_direct,
                    )
                else:
                    raise ValueError("context_request_invalid")
            except RedactionBlocked as exc:
                token = session_manager.rotate(session)
                self._json(
                    400,
                    {"status": "blocked", "finding": str(exc)},
                    session_token=token,
                )
                return
            except OrchestrationBlocked as exc:
                token = session_manager.rotate(session)
                self._json(
                    400,
                    {"status": "blocked", "finding": str(exc)},
                    session_token=token,
                )
                return
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                token = session_manager.rotate(session)
                self._json(400, {
                    "status": "blocked",
                    "finding": "context_request_invalid",
                }, session_token=token)
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
                    + "; HttpOnly; SameSite=Strict; Path=/",
                )
            self._security_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def _asset(self, encoded: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self._security_headers(allow_ui=True)
            self.end_headers()
            self.wfile.write(encoded)

        def _security_headers(self, *, allow_ui: bool = False) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            policy = "default-src 'none'"
            if allow_ui:
                policy += (
                    "; script-src 'self'; style-src 'self'; connect-src 'self'"
                    "; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
                    "; form-action 'none'"
                )
            self.send_header("Content-Security-Policy", policy)
            self.send_header("Referrer-Policy", "no-referrer")

    server = ThreadingHTTPServer((host, port), Handler)
    setattr(server, "fleet_bootstrap_nonce", session_manager.bootstrap_nonce)
    return server


__all__ = ["FleetSessionManager", "create_fleet_server"]
