"""Loopback-only HTTP transport for the Fleet read model."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from importlib.resources import files
from threading import BoundedSemaphore, RLock
from pathlib import Path
from typing import Any, Callable, Protocol, cast
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


def _fleet_event_id(envelope: Mapping[str, Any]) -> str:
    """Hash only state-bearing fields, excluding the sliding session expiry."""
    session = envelope["session"]
    if not isinstance(session, Mapping):
        raise ValueError("fleet_session_invalid")
    event_identity = {
        "snapshot": envelope["snapshot"],
        "annotations": envelope["annotations"],
        "eligibility": envelope["eligibility"],
        "pending": envelope["pending"],
        "session": {
            "write_capable": session["write_capable"],
            "read_only_reason": session["read_only_reason"],
        },
    }
    encoded = json.dumps(
        event_identity,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


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
        command_id: str | None = None,
    ) -> Mapping[str, Any]: ...

    def inject_artifact(
        self,
        content: bytes,
        *,
        target_role: str | None = None,
        media_type: str,
        source_name: str,
        confirm_direct: bool = False,
        command_id: str | None = None,
    ) -> Mapping[str, Any]: ...


class ActionResolver(Protocol):
    @property
    def root(self) -> Path: ...

    def resolve_action(
        self,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]: ...


class RecoveryController(Protocol):
    @property
    def root(self) -> Path: ...

    def abandon(
        self, attempt_ids: list[str], last_sequence: int
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

    def authenticate(
        self, cookie_header: str | None, *, touch: bool = True
    ) -> _Session | None:
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
            if touch:
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
    action_resolver: ActionResolver | None = None,
    recovery_controller: RecoveryController | None = None,
    sessions: FleetSessionManager | None = None,
    control_service: FleetControlService | None = None,
    operational_state_provider: Callable[[], Mapping[str, Any]] | None = None,
) -> ThreadingHTTPServer:
    if not _loopback_host(host):
        raise ValueError("fleet_loopback_required")
    if not 0 <= port <= 65535:
        raise ValueError("fleet_port_invalid")
    resolved_action_resolver = action_resolver
    if resolved_action_resolver is None and context_injector is not None and hasattr(
        context_injector, "resolve_action"
    ):
        resolved_action_resolver = cast(ActionResolver, context_injector)
    if (
        context_injector is not None
        and context_injector.root.resolve() != projector.run_root.resolve()
    ):
        raise ValueError("fleet_control_run_mismatch")
    for controller in (resolved_action_resolver, recovery_controller):
        if (
            controller is not None
            and controller.root.resolve() != projector.run_root.resolve()
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
        if (
            not isinstance(observed_at, str)
            or not isinstance(
            raw_orphans, (list, tuple)
            )
            or raw_operational.get("worker_pid") is not None
            or raw_operational.get("lifecycle") != "recovery_required"
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
        action_available=resolved_action_resolver is not None,
        recovery_available=recovery_controller is not None,
        annotation_provider=operational_annotations,
    )
    sse_slots = BoundedSemaphore(4)

    def fleet_envelope(session: _Session) -> dict[str, Any]:
        snapshot = projector.snapshot()
        run = snapshot.get("run")
        if isinstance(run, Mapping) and run.get("workflow_state") in {
            "closed",
            "abandoned",
        }:
            session_manager.downgrade(session)
        return controls.envelope(
            snapshot,
            session_write_capable=not session.read_only,
            expires_at=session_manager.expires_at(session),
            read_only_reason=("session_read_only" if session.read_only else None),
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
            if parsed.path == "/api/v1/fleet/events":
                self._events(session)
                return
            if parsed.path == "/api/v1/fleet":
                self._json(200, fleet_envelope(session))
                return
            self._json(404, {"status": "not_found"})

        def _events(self, session: _Session) -> None:
            if not sse_slots.acquire(blocking=False):
                self._json(
                    503,
                    {"status": "blocked", "finding": "fleet_sse_capacity_exceeded"},
                )
                return
            cookie = self.headers.get("Cookie")
            last_id = self.headers.get("Last-Event-ID", "")
            if len(last_id) > 128 or any(ord(char) < 0x20 for char in last_id):
                last_id = ""
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "keep-alive")
                self._security_headers()
                self.end_headers()
                self.connection.settimeout(5)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    authenticated = session_manager.authenticate(cookie, touch=False)
                    if authenticated is None:
                        break
                    session = authenticated
                    envelope = fleet_envelope(session)
                    encoded = json.dumps(
                        envelope,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    event_id = _fleet_event_id(envelope)
                    if event_id != last_id:
                        frame = (
                            f"id: {event_id}\n"
                            "event: fleet\n"
                            f"data: {encoded.decode('utf-8')}\n\n"
                        ).encode("utf-8")
                        self.wfile.write(frame)
                        self.wfile.flush()
                        last_id = event_id
                    run = envelope["snapshot"].get("run")
                    if isinstance(run, Mapping) and run.get("workflow_state") in {
                        "closed",
                        "abandoned",
                    }:
                        break
                    time.sleep(3)
            except (BrokenPipeError, ConnectionResetError, socket.timeout):
                pass
            finally:
                sse_slots.release()

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self._json(404, {"status": "not_found"})
                return
            action_match = re.fullmatch(
                r"/api/v1/fleet/actions/([A-Za-z0-9][A-Za-z0-9:._-]{0,127})/resolve",
                parsed.path,
            )
            if parsed.path in {"/api/v1/context", "/api/v1/fleet/context"}:
                operation = "context"
                service_present = context_injector is not None
            elif action_match is not None:
                operation = "resolve_action"
                service_present = resolved_action_resolver is not None
            elif parsed.path in {
                "/api/v1/fleet/recover/confirm",
                "/api/v1/fleet/recover",
            }:
                operation = "recover_run"
                service_present = recovery_controller is not None
            else:
                operation = "unknown"
                service_present = False
            if not service_present:
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
            if operation == "resolve_action":
                assert action_match is not None
                action_id = action_match.group(1)
                eligibility = envelope["eligibility"]["resolve_action"].get(
                    action_id,
                    {"eligible": False, "reason": "action_not_open"},
                )
            else:
                eligibility = envelope["eligibility"][operation]
            if not eligibility["eligible"]:
                token = session_manager.rotate(session)
                self._json(
                    409,
                    {
                        "status": "blocked",
                        "finding": eligibility["reason"],
                    },
                    session_token=token,
                )
                return
            if operation != "context":
                self._control_post(
                    parsed.path,
                    action_match.group(1) if action_match is not None else None,
                    session,
                    snapshot,
                )
                return
            context_correlation: str | None = None
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
                    "correlation_id",
                }
                if set(payload) - allowed:
                    raise ValueError("context_request_invalid")
                raw_correlation = payload.get("correlation_id")
                if raw_correlation is None and parsed.path == "/api/v1/context":
                    raw_correlation = "legacy-" + secrets.token_hex(16)
                if (
                    not isinstance(raw_correlation, str)
                    or re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", raw_correlation
                    )
                    is None
                ):
                    raise ValueError("fleet_correlation_invalid")
                context_correlation = raw_correlation
                controls.begin(context_correlation, "context")
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
                    assert context_injector is not None
                    result = context_injector.inject(
                        content,
                        target_role=target_role,
                        media_type=media_type,
                        source_name=source_name,
                        confirm_direct=confirm_direct,
                        command_id=context_correlation,
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
                    assert context_injector is not None
                    result = context_injector.inject_artifact(
                        decoded,
                        target_role=target_role,
                        media_type=media_type,
                        source_name=source_name,
                        confirm_direct=confirm_direct,
                        command_id=context_correlation,
                    )
                else:
                    raise ValueError("context_request_invalid")
            except RedactionBlocked as exc:
                if context_correlation is not None:
                    controls.failed(context_correlation)
                token = session_manager.rotate(session)
                self._json(
                    400,
                    {"status": "blocked", "finding": str(exc)},
                    session_token=token,
                )
                return
            except OrchestrationBlocked as exc:
                if context_correlation is not None:
                    controls.failed(context_correlation)
                token = session_manager.rotate(session)
                self._json(
                    400,
                    {"status": "blocked", "finding": str(exc)},
                    session_token=token,
                )
                return
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                if context_correlation is not None:
                    controls.failed(context_correlation)
                token = session_manager.rotate(session)
                self._json(400, {
                    "status": "blocked",
                    "finding": "context_request_invalid",
                }, session_token=token)
                return
            expected_sequence = result.get("sequence")
            if not isinstance(expected_sequence, int) or context_correlation is None:
                if context_correlation is not None:
                    controls.failed(context_correlation)
                token = session_manager.rotate(session)
                self._json(
                    500,
                    {"status": "internal_error", "finding": "context_result_invalid"},
                    session_token=token,
                )
                return
            controls.committed(context_correlation, expected_sequence)
            rotated = session_manager.rotate(session)
            self._json(
                202,
                {
                    "status": "accepted",
                    "correlation_id": context_correlation,
                    "context": result,
                },
                session_token=rotated,
            )

        def _read_control_payload(self) -> Mapping[str, Any]:
            content_types = self.headers.get_all("Content-Type", failobj=[])
            if len(content_types) != 1 or content_types[0].casefold() not in {
                "application/json",
                "application/json; charset=utf-8",
            }:
                raise ValueError("fleet_control_content_type_invalid")
            if self.headers.get_all("Transfer-Encoding", failobj=[]):
                raise ValueError("fleet_control_transfer_encoding_denied")
            lengths = self.headers.get_all("Content-Length", failobj=[])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError("fleet_control_size_invalid")
            length = int(lengths[0])
            if length <= 0 or length > 16_384:
                raise ValueError("fleet_control_size_invalid")
            payload = json.loads(
                self.rfile.read(length),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, Mapping):
                raise ValueError("fleet_control_request_invalid")
            return payload

        def _control_post(
            self,
            path: str,
            action_id: str | None,
            session: _Session,
            snapshot: Mapping[str, Any],
        ) -> None:
            correlation_id: str | None = None
            try:
                payload = self._read_control_payload()
                correlation_id = payload.get("correlation_id")
                if (
                    not isinstance(correlation_id, str)
                    or re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}",
                        correlation_id,
                    )
                    is None
                ):
                    raise ValueError("fleet_correlation_invalid")
                if path == "/api/v1/fleet/recover/confirm":
                    if set(payload) != {"correlation_id"}:
                        raise ValueError("recovery_confirmation_request_invalid")
                    confirmation = controls.issue_recovery_confirmation(
                        snapshot,
                        correlation_id=correlation_id,
                        session_write_capable=True,
                        expires_at=session_manager.expires_at(session),
                    )
                    rotated = session_manager.rotate(session)
                    self._json(
                        200,
                        {
                            "status": "confirmation_required",
                            "correlation_id": correlation_id,
                            "confirmation_token": confirmation,
                            "effect": "Permanently abandon this governed run",
                        },
                        session_token=rotated,
                    )
                    return
                if path == "/api/v1/fleet/recover":
                    if set(payload) != {"correlation_id", "confirmation_token"}:
                        raise ValueError("recovery_request_invalid")
                    confirmation_token = payload.get("confirmation_token")
                    if (
                        not isinstance(confirmation_token, str)
                        or len(confirmation_token) > 256
                    ):
                        raise ValueError("recovery_confirmation_invalid")
                    controls.consume_recovery_confirmation(
                        confirmation_token,
                        snapshot,
                        correlation_id=correlation_id,
                    )
                    assert recovery_controller is not None
                    verification = snapshot.get("verification")
                    lanes = snapshot.get("lanes")
                    if not isinstance(verification, Mapping) or not isinstance(
                        lanes, list
                    ):
                        raise ValueError("recovery_snapshot_invalid")
                    covered = verification.get("covered_sequence")
                    if not isinstance(covered, int):
                        raise ValueError("recovery_snapshot_invalid")
                    attempt_ids = [
                        str(attempt["attempt_id"])
                        for lane in lanes
                        if isinstance(lane, Mapping)
                        for attempt in lane.get("attempts", [])
                        if isinstance(attempt, Mapping)
                        and attempt.get("terminal_sequence") is None
                        and isinstance(attempt.get("attempt_id"), str)
                    ]
                    controls.begin(correlation_id, "recover_run")
                    result = recovery_controller.abandon(attempt_ids, covered)
                    expected = result.get("sequence")
                    if not isinstance(expected, int):
                        raise ValueError("recovery_result_invalid")
                    controls.committed(correlation_id, expected)
                else:
                    if action_id is None or set(payload) != {
                        "correlation_id",
                        "resolution",
                    }:
                        raise ValueError("action_resolution_request_invalid")
                    resolution = payload.get("resolution")
                    if (
                        not isinstance(resolution, str)
                        or re.fullmatch(
                            r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", resolution
                        )
                        is None
                    ):
                        raise ValueError("action_resolution_request_invalid")
                    assert resolved_action_resolver is not None
                    controls.begin(correlation_id, "resolve_action")
                    result = resolved_action_resolver.resolve_action(
                        action_id=action_id,
                        resolution=resolution,
                        resolver_identity="operator:local-session",
                    )
                    expected = result.get("run_decision_sequence")
                    if not isinstance(expected, int):
                        raise ValueError("action_resolution_result_invalid")
                    controls.committed(correlation_id, expected)
                rotated = session_manager.rotate(session)
                self._json(
                    202,
                    {
                        "status": "accepted",
                        "correlation_id": correlation_id,
                        "result": dict(result),
                    },
                    session_token=rotated,
                )
            except OSError:
                if correlation_id is not None:
                    controls.failed(correlation_id)
                token = session_manager.rotate(session)
                self._json(
                    500,
                    {"status": "internal_error", "finding": "fleet_control_io_error"},
                    session_token=token,
                )
            except (ValueError, OrchestrationBlocked, PermissionError) as exc:
                if correlation_id is not None:
                    controls.failed(correlation_id)
                token = session_manager.rotate(session)
                self._json(
                    409,
                    {"status": "blocked", "finding": str(exc)},
                    session_token=token,
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
