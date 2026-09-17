"""Loopback-only HTTP transport for the Fleet read model."""

from __future__ import annotations

import base64
import binascii
import codecs
import hashlib
import ipaddress
import json
import queue
import re
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from threading import BoundedSemaphore, RLock
from typing import Any, Protocol, cast
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlsplit

from torq_cli.application.fleet import FleetProjector
from torq_cli.application.fleet_controls import FleetControlService
from torq_cli.application.orchestrator import OrchestrationBlocked
from torq_cli.application.workspace import classify_workspace_root
from torq_cli.core.redaction import RedactionBlocked

_FLEET_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/assets/fleet.css": ("fleet.css", "text/css; charset=utf-8"),
    "/assets/fleet.js": ("fleet.js", "text/javascript; charset=utf-8"),
    "/assets/chat.css": ("chat.css", "text/css; charset=utf-8"),
    "/assets/chat.js": ("chat.js", "text/javascript; charset=utf-8"),
    "/assets/task.css": ("task.css", "text/css; charset=utf-8"),
    "/assets/task.js": ("task.js", "text/javascript; charset=utf-8"),
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
        self,
        attempt_ids: list[str],
        last_sequence: int,
        manifest_generation: int,
    ) -> Mapping[str, Any]: ...


class ChatController(Protocol):
    @property
    def root(self) -> Path: ...

    def submit(
        self,
        *,
        turn_id: str,
        text: str,
        attachments: list[Mapping[str, str]],
    ) -> Mapping[str, Any]: ...

    def cancel(self, turn_id: str, *, timeout: float = 5.0) -> Mapping[str, Any]: ...

    def subscribe(self, *, capacity: int = 256) -> queue.Queue[Any]: ...

    def unsubscribe(self, channel: queue.Queue[Any]) -> None: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def shutdown(self, *, timeout: float = 5.0) -> Mapping[str, Any] | None: ...


@dataclass
class _Session:
    token: str
    issued_at: float
    last_seen: float
    read_only: bool = False
    subject_id: str = ""


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
        self._read_recovery: dict[str, str] = {}
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
            self._sessions[token] = _Session(
                token, now, now, subject_id="session-" + secrets.token_hex(16)
            )
            return token

    def authenticate(self, cookie_header: str | None, *, touch: bool = True) -> _Session | None:
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
                    UTC,
                )
                .isoformat()
                .replace("+00:00", "Z")
            )

    def rotate(self, session: _Session) -> str:
        with self._lock:
            self._sessions.pop(session.token, None)
            self._read_recovery = {
                old: target
                for old, target in self._read_recovery.items()
                if target != session.token
            }
            now = self._clock()
            token = secrets.token_urlsafe(48)
            self._sessions[token] = _Session(
                token,
                session.issued_at,
                now,
                read_only=session.read_only,
                subject_id=session.subject_id,
            )
            self._read_recovery[session.token] = token
            return token

    def recover_rotated_session(self, cookie_header: str | None) -> tuple[_Session, str] | None:
        """Resolve a prior token only for the handler's idempotent task replay path."""
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
            current = self._read_recovery.get(morsel.value)
            session = self._sessions.get(current or "")
            if session is None:
                return None
            now = self._clock()
            if now - session.last_seen >= self._idle_seconds or now - session.issued_at >= self._absolute_seconds:
                self._read_recovery.pop(morsel.value, None)
                return None
            session.last_seen = now
            return session, session.token

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


def _safe_finding(exc: BaseException, default: str) -> str:
    if exc.args and isinstance(exc.args[0], str):
        value = exc.args[0].split(":", 1)[0]
        if (
            value.startswith(("task_", "candidate_"))
            and value.replace("_", "").isalnum()
            and len(value) <= 64
        ):
            return value
    return default


class TaskHTTPService(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def draft(self, project_id: str) -> dict[str, Any] | None: ...
    def save_draft(self, project_id: str, *, goal: str, input_paths: list[str], output_paths: list[str], expected_revision: int) -> dict[str, Any]: ...
    def delete_draft(self, project_id: str, *, expected_revision: int) -> None: ...
    def review_plan(self, *, project_id: str, draft_revision: int, input_paths: list[str], output_paths: list[str]) -> dict[str, Any]: ...
    def start(self, *, request_id: str, plan_hash: str) -> dict[str, Any]: ...
    def replay_start(self, *, request_id: str, plan_hash: str) -> dict[str, Any]: ...
    def stop(self, task_id: str) -> dict[str, Any]: ...
    def task(self, task_id: str) -> dict[str, Any]: ...
    def history(self) -> list[dict[str, Any]]: ...


class ReviewHTTPService(Protocol):
    def review(self, task_id: str, ready_sequence: int) -> dict[str, Any]: ...
    def correction(self, task_id: str, ready_sequence: int) -> dict[str, Any]: ...
    def save_correction(self, task_id: str, ready_sequence: int, *, text: str, expected_revision: int) -> dict[str, Any]: ...
    def review_child_plan(self, task_id: str, ready_sequence: int, *, request_id: str, correction_revision: int, correction_hash: str, review_hash: str, subject_id: str) -> dict[str, Any]: ...
    def replay_child_plan(self, task_id: str, ready_sequence: int, *, request_id: str, correction_revision: int, correction_hash: str, review_hash: str) -> dict[str, Any]: ...
    def start_revision(self, *, request_id: str, plan_hash: str, replay_only: bool = False) -> dict[str, Any]: ...
    def continue_task(self, *, request_id: str, application_id: str) -> dict[str, Any]: ...
    def replay_continuation(self, *, request_id: str, application_id: str) -> dict[str, Any]: ...
    def accept(self, *, request_id: str, task_id: str, ready_sequence: int, review_hash: str, subject_id: str) -> dict[str, Any]: ...
    def replay_accept(self, *, request_id: str, task_id: str, ready_sequence: int, review_hash: str) -> dict[str, Any]: ...
    def apply(self, *, request_id: str, candidate: Mapping[str, Any], acceptance: Mapping[str, Any], subject_id: str) -> dict[str, Any]: ...
    def replay_apply(self, *, request_id: str, candidate: Mapping[str, Any], acceptance: Mapping[str, Any]) -> dict[str, Any]: ...
    def application(self, application_id: str) -> dict[str, Any]: ...
    def history(self, *, query: str = "", limit: int = 50, cursor: str | None = None) -> dict[str, Any]: ...
    def recovery_status(self, project_id: str) -> dict[str, Any]: ...
    def recover(self, project_id: str) -> dict[str, Any]: ...


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
    chat_controller: ChatController | None = None,
    chat_snapshot_provider: Callable[[], Mapping[str, Any]] | None = None,
    workspace_chat_provider: str | None = None,
    task_service: TaskHTTPService | None = None,
    review_service: ReviewHTTPService | None = None,
) -> ThreadingHTTPServer:
    if not _loopback_host(host):
        raise ValueError("fleet_loopback_required")
    if not 0 <= port <= 65535:
        raise ValueError("fleet_port_invalid")
    resolved_action_resolver = action_resolver
    if (
        resolved_action_resolver is None
        and context_injector is not None
        and hasattr(context_injector, "resolve_action")
    ):
        resolved_action_resolver = cast(ActionResolver, context_injector)
    if (
        context_injector is not None
        and context_injector.root.resolve() != projector.run_root.resolve()
    ):
        raise ValueError("fleet_control_run_mismatch")
    for controller in (resolved_action_resolver, recovery_controller):
        if controller is not None and controller.root.resolve() != projector.run_root.resolve():
            raise ValueError("fleet_control_run_mismatch")
    if (
        chat_controller is not None
        and chat_controller.root.resolve() != projector.run_root.resolve()
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
        if (
            isinstance(observed_at, str)
            and raw_operational.get("worker_pid") is None
            and raw_operational.get("lifecycle") == "workflow_reconciled"
        ):
            return [
                {
                    "kind": "workflow_reconciled",
                    "scope": "run",
                    "observed_at": observed_at,
                    "source": "supervisor",
                }
            ]
        raw_orphans = raw_operational.get("orphaned_roles", ())
        if (
            not isinstance(observed_at, str)
            or not isinstance(raw_orphans, (list, tuple))
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
    chat_sse_slots = BoundedSemaphore(4)

    def chat_snapshot() -> dict[str, Any]:
        if chat_controller is None or chat_snapshot_provider is None:
            return {
                "schema": "torq-chat-projection-v1",
                "data_status": "unavailable",
                "status": "unavailable",
                "finding": "chat_runtime_unavailable",
                "messages": [],
                "active_turn_id": None,
                "last_sequence": 0,
            }
        snapshot = dict(chat_snapshot_provider())
        runtime = chat_controller.snapshot()
        active = runtime.get("active_turn_id")
        if isinstance(active, str) and snapshot.get("data_status") == "available":
            snapshot["active_turn_id"] = active
            snapshot["status"] = "running"
        snapshot["stream_sequence"] = runtime.get("stream_sequence", 0)
        return snapshot

    def fleet_envelope(session: _Session) -> dict[str, Any]:
        snapshot = projector.snapshot()
        run = snapshot.get("run")
        annotations = operational_annotations()
        if (
            isinstance(run, Mapping)
            and run.get("workflow_state")
            in {
                "closed",
                "abandoned",
            }
            or any(item.get("kind") == "workflow_reconciled" for item in annotations)
        ):
            session_manager.downgrade(session)
        return controls.envelope(
            snapshot,
            session_write_capable=not session.read_only,
            expires_at=session_manager.expires_at(session),
            read_only_reason=("session_read_only" if session.read_only else None),
        )

    def workspace_metadata(session: _Session) -> dict[str, Any]:
        root = classify_workspace_root(projector.run_root)
        if root["root_kind"] == "individual_run":
            envelope = fleet_envelope(session)
            snapshot = envelope["snapshot"]
            root = classify_workspace_root(projector.run_root, snapshot)
            write_capable = bool(envelope["session"].get("write_capable"))
        else:
            snapshot = {}
            write_capable = not session.read_only
        chat = chat_snapshot()
        runtime_present = chat_controller is not None and chat_snapshot_provider is not None
        chat_available = runtime_present and chat.get("data_status") == "available"
        active_turn_id = chat.get("active_turn_id")
        active = isinstance(active_turn_id, str) and bool(active_turn_id)
        configured_provider = workspace_chat_provider.casefold() if workspace_chat_provider else None
        supported_provider = configured_provider in {"claude", "deepseek", "kimi", "qwen", "zai"}
        provider = configured_provider if supported_provider else None
        attachment_types = (
            []
            if provider in {None, "claude"}
            else [
                "application/json",
                "application/pdf",
                "image/gif",
                "image/jpeg",
                "image/png",
                "image/webp",
                "text/markdown",
                "text/plain",
            ]
        )
        root_ready = root["root_kind"] == "individual_run" and root["trusted"] is True
        can_discuss = bool(root_ready and provider and chat_available and write_capable and not active)
        can_cancel = bool(root_ready and runtime_present and write_capable and active)
        if not root_ready:
            reason_code = root["reason_code"]
            message = root["message"]
            remediation = root["remediation"]
        elif provider is None:
            reason_code = "workspace_chat_provider_missing"
            message = "Discussion is not enabled for this session."
            remediation = "Relaunch with --chat-provider and --chat-model to discuss this run."
        elif not write_capable:
            run = snapshot.get("run") if isinstance(snapshot, Mapping) else None
            closed = isinstance(run, Mapping) and run.get("workflow_state") in {"closed", "abandoned"}
            reason_code = "workspace_run_closed" if closed else "fleet_session_read_only"
            message = (
                "This completed run is available for review."
                if closed
                else "This session is open read-only."
            )
            remediation = (
                "Use Fleet to inspect its verified evidence. Discussion cannot be added to a closed run."
                if closed
                else "Relaunch this individual run to obtain a new session."
            )
        elif not chat_available:
            reason_code = "chat_runtime_unavailable"
            message = "Discussion history is not available."
            remediation = "Inspect Details, then relaunch the configured individual run."
        elif active:
            reason_code = "chat_turn_active"
            message = "TORQ is responding. You can write the next draft now."
            remediation = "Send the draft manually after this response finishes, or stop the active turn."
        else:
            reason_code = None
            message = "Ask about this verified run."
            remediation = None
        return {
            "schema": "torq-workspace-v1",
            "workspace_id": root["workspace_id"],
            "root": {
                "kind": root["root_kind"],
                "trusted": root["trusted"],
                "verification_state": root["verification_state"],
                "run_mode": root["run_mode"],
            },
            "selected_run_id": root["selected_run_id"],
            "runs": root["runs"],
            "provider": {
                "name": provider,
                "configuration": (
                    "configured"
                    if provider
                    else "unsupported"
                    if configured_provider
                    else "not_configured"
                ),
                "authentication": "not_checked",
            },
            "capabilities": {
                "can_discuss_run": can_discuss,
                "can_cancel": can_cancel,
                "can_start_task": False,
                "execution_supported": False,
                "chat_stream_available": bool(runtime_present and chat_available),
                "draft_storage": "browser_session",
                "attachment_types": attachment_types,
                "limits": {
                    "attachments": 0 if not attachment_types else 6,
                    "attachment_bytes": 0 if not attachment_types else 5 * 1024 * 1024,
                },
            },
            "active_turn_id": active_turn_id if active else None,
            "guidance": {
                "reason_code": reason_code,
                "message": message,
                "remediation": remediation,
                "consequence": "Drafts are never sent automatically.",
            },
        }

    def preflight_chat_submit(session: _Session) -> None:
        root = classify_workspace_root(projector.run_root)
        if root["root_kind"] != "individual_run":
            raise ValueError(str(root["reason_code"]))
        snapshot = projector.snapshot()
        root = classify_workspace_root(projector.run_root, snapshot)
        if root["trusted"] is not True:
            raise ValueError("workspace_run_untrusted")
        run = snapshot.get("run")
        if isinstance(run, Mapping) and run.get("workflow_state") in {"closed", "abandoned"}:
            session_manager.downgrade(session)
        if session.read_only:
            raise ValueError("fleet_session_read_only")
        chat = chat_snapshot()
        if chat.get("data_status") != "available":
            raise ValueError("chat_runtime_unavailable")
        active_turn_id = chat.get("active_turn_id")
        if isinstance(active_turn_id, str) and active_turn_id:
            raise ValueError("chat_turn_active")

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
                requested_view = values.get("view", [])
                location = "/?view=task" if requested_view == ["task"] else "/"
                self.send_header("Location", location)
                self.send_header(
                    "Set-Cookie",
                    "torq_fleet_session=" + token + "; HttpOnly; SameSite=Strict; Path=/",
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
            if parsed.path == "/api/v1/chat/events":
                self._chat_events(session)
                return
            if parsed.path == "/api/v1/fleet":
                self._json(200, fleet_envelope(session))
                return
            if parsed.path == "/api/v1/workspace":
                self._json(200, workspace_metadata(session))
                return
            if parsed.path == "/api/v1/tasks/capabilities" and task_service is not None:
                try:
                    self._json(200, task_service.capabilities())
                except (OSError, ValueError):
                    self._json(409, {"status": "blocked", "finding": "task_state_unavailable"})
                return
            if parsed.path == "/api/v1/tasks" and task_service is not None:
                try:
                    self._json(200, {"schema": "torq-task-history-v1", "tasks": task_service.history()})
                except (OSError, ValueError):
                    self._json(409, {"status": "blocked", "finding": "task_state_unavailable"})
                return
            review_match = re.fullmatch(
                r"/api/v1/task-reviews/(run-task-[a-f0-9]{24})/([1-9][0-9]*)",
                parsed.path,
            )
            correction_match = re.fullmatch(
                r"/api/v1/task-reviews/(run-task-[a-f0-9]{24})/([1-9][0-9]*)/correction",
                parsed.path,
            )
            application_match = re.fullmatch(
                r"/api/v1/task-applications/(application-[a-f0-9]{24})",
                parsed.path,
            )
            if review_match is not None and review_service is not None:
                try:
                    self._json(200, review_service.review(review_match.group(1), int(review_match.group(2))))
                except (OSError, ValueError) as exc:
                    self._json(409, {"status": "blocked", "finding": _safe_finding(exc, "task_review_unavailable")})
                return
            if correction_match is not None and review_service is not None:
                try:
                    self._json(200, {"correction": review_service.correction(correction_match.group(1), int(correction_match.group(2)))})
                except (OSError, ValueError) as exc:
                    self._json(409, {"status": "blocked", "finding": _safe_finding(exc, "task_review_unavailable")})
                return
            if application_match is not None and review_service is not None:
                try:
                    self._json(200, review_service.application(application_match.group(1)))
                except (OSError, ValueError) as exc:
                    self._json(409, {"status": "blocked", "finding": _safe_finding(exc, "task_application_unavailable")})
                return
            if parsed.path == "/api/v1/task-history" and review_service is not None:
                values = parse_qs(parsed.query, keep_blank_values=True)
                if set(values) - {"q", "limit", "cursor"} or any(len(item) != 1 for item in values.values()):
                    self._json(409, {"status": "blocked", "finding": "candidate_history_query_invalid"})
                    return
                try:
                    limit = int(values.get("limit", ["50"])[0])
                    self._json(200, review_service.history(
                        query=values.get("q", [""])[0], limit=limit,
                        cursor=values.get("cursor", [None])[0],
                    ))
                except (OSError, ValueError) as exc:
                    self._json(409, {"status": "blocked", "finding": _safe_finding(exc, "candidate_history_query_invalid")})
                return
            recovery_match = re.fullmatch(
                r"/api/v1/task-recovery/([A-Za-z0-9][A-Za-z0-9._-]{0,63})", parsed.path
            )
            if recovery_match is not None and review_service is not None:
                try:
                    self._json(200, review_service.recovery_status(recovery_match.group(1)))
                except (OSError, ValueError) as exc:
                    self._json(409, {"status": "blocked", "finding": _safe_finding(exc, "task_apply_recovery_unavailable")})
                return
            draft_match = re.fullmatch(r"/api/v1/tasks/drafts/([A-Za-z0-9][A-Za-z0-9._-]{0,63})", parsed.path)
            if draft_match is not None and task_service is not None:
                try:
                    self._json(200, {"draft": task_service.draft(draft_match.group(1))})
                except ValueError:
                    self._json(404, {"status": "not_found"})
                return
            task_match = re.fullmatch(r"/api/v1/tasks/(run-task-[a-f0-9]{24})", parsed.path)
            if task_match is not None and task_service is not None:
                try:
                    self._json(200, {"task": task_service.task(task_match.group(1))})
                except ValueError:
                    self._json(404, {"status": "not_found"})
                return
            if parsed.path == "/api/v1/chat":
                self._json(200, chat_snapshot())
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
                            f"id: {event_id}\nevent: fleet\ndata: {encoded.decode('utf-8')}\n\n"
                        ).encode()
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
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                sse_slots.release()

        def _chat_events(self, session: _Session) -> None:
            if chat_controller is None:
                self._json(
                    405,
                    {"status": "unavailable", "finding": "chat_runtime_unavailable"},
                )
                return
            if not chat_sse_slots.acquire(blocking=False):
                self._json(
                    503,
                    {"status": "blocked", "finding": "chat_sse_capacity_exceeded"},
                )
                return
            channel = chat_controller.subscribe(capacity=256)
            decoders: dict[tuple[str, str], codecs.IncrementalDecoder] = {}
            cookie = self.headers.get("Cookie")
            last_snapshot = ""
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
                    if session_manager.authenticate(cookie, touch=False) is None:
                        break
                    try:
                        runtime_event = channel.get(timeout=0.25)
                    except queue.Empty:
                        runtime_event = None
                    if runtime_event is not None:
                        decoder = decoders.setdefault(
                            (runtime_event.turn_id, runtime_event.kind),
                            codecs.getincrementaldecoder("utf-8")("replace"),
                        )
                        decoded_text = decoder.decode(runtime_event.data, final=False)
                        if not decoded_text:
                            continue
                        self._sse_json(
                            {
                                "type": "output_delta",
                                "turn_id": runtime_event.turn_id,
                                "stream_sequence": runtime_event.sequence,
                                "channel": runtime_event.kind,
                                "text": decoded_text,
                            }
                        )
                    snapshot = chat_snapshot()
                    active_turn = snapshot.get("active_turn_id")
                    if isinstance(active_turn, str):
                        decoders = {
                            key: value for key, value in decoders.items() if key[0] == active_turn
                        }
                    else:
                        decoders.clear()
                    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
                    if encoded != last_snapshot:
                        self._sse_json({"type": "snapshot", "snapshot": snapshot})
                        last_snapshot = encoded
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                chat_controller.unsubscribe(channel)
                chat_sse_slots.release()

        def _sse_json(self, value: Mapping[str, Any]) -> None:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            self.wfile.write(f"data: {encoded}\n\n".encode())
            self.wfile.flush()

        def do_POST(self) -> None:  # noqa: N802
            if not self._host_allowed():
                self._json(421, {"status": "blocked", "finding": "fleet_host_denied"})
                return
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self._json(404, {"status": "not_found"})
                return
            try:
                path = unquote(parsed.path, encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, ValueError):
                self._json(404, {"status": "not_found"})
                return
            action_match = re.fullmatch(
                r"/api/v1/fleet/actions/([A-Za-z0-9][A-Za-z0-9:._-]{0,127})/resolve",
                path,
            )
            chat_cancel_match = re.fullmatch(
                r"/api/v1/chat/turns/([A-Za-z0-9][A-Za-z0-9:._-]{0,127})/cancel",
                path,
            )
            task_draft_match = re.fullmatch(
                r"/api/v1/tasks/drafts/([A-Za-z0-9][A-Za-z0-9._-]{0,63})", path
            )
            task_draft_delete_match = re.fullmatch(
                r"/api/v1/tasks/drafts/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/delete", path
            )
            task_stop_match = re.fullmatch(r"/api/v1/tasks/(run-task-[a-f0-9]{24})/stop", path)
            review_correction_match = re.fullmatch(
                r"/api/v1/task-reviews/(run-task-[a-f0-9]{24})/([1-9][0-9]*)/correction", path
            )
            review_child_match = re.fullmatch(
                r"/api/v1/task-reviews/(run-task-[a-f0-9]{24})/([1-9][0-9]*)/child-plans", path
            )
            review_recovery_match = re.fullmatch(
                r"/api/v1/task-recovery/([A-Za-z0-9][A-Za-z0-9._-]{0,63})/recover", path
            )
            if path == "/api/v1/chat/turns":
                operation = "chat_submit"
                service_present = chat_controller is not None
            elif chat_cancel_match is not None:
                operation = "chat_cancel"
                service_present = chat_controller is not None
            elif task_draft_delete_match is not None:
                operation = "task_draft_delete"
                service_present = task_service is not None
            elif task_draft_match is not None:
                operation = "task_draft"
                service_present = task_service is not None
            elif path == "/api/v1/tasks/plans":
                operation = "task_plan"
                service_present = task_service is not None
            elif path == "/api/v1/tasks":
                operation = "task_start"
                service_present = task_service is not None
            elif task_stop_match is not None:
                operation = "task_stop"
                service_present = task_service is not None
            elif review_correction_match is not None:
                operation = "review_correction"
                service_present = review_service is not None
            elif review_child_match is not None:
                operation = "review_child_plan"
                service_present = review_service is not None
            elif path == "/api/v1/task-revisions":
                operation = "review_revision"
                service_present = review_service is not None
            elif path == "/api/v1/task-continuations":
                operation = "review_continuation"
                service_present = review_service is not None
            elif path == "/api/v1/task-reviews/accept":
                operation = "review_accept"
                service_present = review_service is not None
            elif path == "/api/v1/task-applications":
                operation = "review_apply"
                service_present = review_service is not None
            elif review_recovery_match is not None:
                operation = "review_recover"
                service_present = review_service is not None
            elif path in {"/api/v1/context", "/api/v1/fleet/context"}:
                operation = "context"
                service_present = context_injector is not None
            elif action_match is not None:
                operation = "resolve_action"
                service_present = resolved_action_resolver is not None
            elif path in {
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
            replay_only = False
            session = session_manager.claim_mutation(self.headers.get("Cookie"))
            if session is None and operation in {
                "task_start", "review_child_plan", "review_revision", "review_continuation",
                "review_accept", "review_apply"
            }:
                recovered = session_manager.recover_rotated_session(self.headers.get("Cookie"))
                if recovered is not None:
                    session, _ = recovered
                    replay_only = True
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
            if operation in {"chat_submit", "chat_cancel"}:
                self._chat_post(operation, chat_cancel_match, session)
                return
            if operation.startswith("task_"):
                self._task_post(
                    operation,
                    task_draft_delete_match or task_draft_match,
                    task_stop_match,
                    session,
                    replay_only=replay_only,
                )
                return
            if operation.startswith("review_"):
                self._review_post(
                    operation, review_correction_match, review_child_match, review_recovery_match,
                    session, replay_only=replay_only
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
                    or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", raw_correlation) is None
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
                        or source_name is not None
                        and not isinstance(source_name, str)
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
                self._json(
                    400,
                    {
                        "status": "blocked",
                        "finding": "context_request_invalid",
                    },
                    session_token=token,
                )
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

        def _chat_post(
            self,
            operation: str,
            cancel_match: re.Match[str] | None,
            session: _Session,
        ) -> None:
            assert chat_controller is not None
            try:
                if operation == "chat_submit":
                    preflight_chat_submit(session)
                payload = self._read_chat_payload()
                if operation == "chat_cancel":
                    if payload or cancel_match is None:
                        raise ValueError("chat_cancel_request_invalid")
                    result = chat_controller.cancel(cancel_match.group(1), timeout=5.0)
                else:
                    if set(payload) != {"turn_id", "text", "attachments"}:
                        raise ValueError("chat_turn_request_invalid")
                    turn_id = payload.get("turn_id")
                    text_value = payload.get("text")
                    attachments_value = payload.get("attachments")
                    if (
                        not isinstance(turn_id, str)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", turn_id) is None
                        or not isinstance(text_value, str)
                        or not isinstance(attachments_value, list)
                        or not all(isinstance(item, Mapping) for item in attachments_value)
                    ):
                        raise ValueError("chat_turn_request_invalid")
                    attachments = [cast(Mapping[str, str], item) for item in attachments_value]
                    if (
                        isinstance(workspace_chat_provider, str)
                        and workspace_chat_provider.casefold() == "claude"
                        and attachments
                    ):
                        raise ValueError("chat_subscription_attachments_unsupported")
                    result = chat_controller.submit(
                        turn_id=turn_id,
                        text=text_value,
                        attachments=attachments,
                    )
                rotated = session_manager.rotate(session)
                self._json(
                    202,
                    {"status": "accepted", "result": dict(result)},
                    session_token=rotated,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                rotated = session_manager.rotate(session)
                self._json(
                    409,
                    {"status": "blocked", "finding": str(exc)},
                    session_token=rotated,
                )

        def _read_chat_payload(self) -> Mapping[str, Any]:
            content_types = self.headers.get_all("Content-Type", failobj=[])
            if len(content_types) != 1 or content_types[0].casefold() not in {
                "application/json",
                "application/json; charset=utf-8",
            }:
                raise ValueError("chat_content_type_invalid")
            if self.headers.get_all("Transfer-Encoding", failobj=[]):
                raise ValueError("chat_transfer_encoding_denied")
            lengths = self.headers.get_all("Content-Length", failobj=[])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError("chat_size_invalid")
            length = int(lengths[0])
            if length <= 0 or length > 42_000_000:
                raise ValueError("chat_size_invalid")
            payload = json.loads(
                self.rfile.read(length),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, Mapping):
                raise ValueError("chat_request_invalid")
            return payload

        def _read_task_payload(self, *, allow_empty: bool = False) -> Mapping[str, Any]:
            content_types = self.headers.get_all("Content-Type", failobj=[])
            if len(content_types) != 1 or content_types[0].casefold() not in {
                "application/json", "application/json; charset=utf-8"
            }:
                raise ValueError("task_content_type_invalid")
            if self.headers.get_all("Transfer-Encoding", failobj=[]):
                raise ValueError("task_transfer_encoding_denied")
            lengths = self.headers.get_all("Content-Length", failobj=[])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError("task_size_invalid")
            length = int(lengths[0])
            if length > 65_536 or length == 0 and not allow_empty:
                raise ValueError("task_size_invalid")
            if length == 0:
                return {}
            payload = json.loads(
                self.rfile.read(length),
                object_pairs_hook=_unique_json_object,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(payload, Mapping):
                raise ValueError("task_request_invalid")
            return payload

        def _task_post(
            self,
            operation: str,
            draft_match: re.Match[str] | None,
            stop_match: re.Match[str] | None,
            session: _Session,
            *,
            replay_only: bool,
        ) -> None:
            assert task_service is not None
            try:
                payload = self._read_task_payload(allow_empty=operation == "task_stop")
                if operation == "task_draft":
                    if set(payload) != {"goal", "input_paths", "output_paths", "expected_revision"} or draft_match is None:
                        raise ValueError("task_draft_request_invalid")
                    goal = payload["goal"]
                    revision = payload["expected_revision"]
                    input_paths = payload["input_paths"]
                    output_paths = payload["output_paths"]
                    if (
                        not isinstance(goal, str)
                        or not isinstance(revision, int)
                        or isinstance(revision, bool)
                        or not isinstance(input_paths, list)
                        or not isinstance(output_paths, list)
                        or not all(isinstance(item, str) for item in input_paths + output_paths)
                    ):
                        raise ValueError("task_draft_request_invalid")
                    result: object = task_service.save_draft(
                        draft_match.group(1), goal=goal, input_paths=input_paths,
                        output_paths=output_paths, expected_revision=revision,
                    )
                elif operation == "task_draft_delete":
                    if set(payload) != {"expected_revision"} or draft_match is None:
                        raise ValueError("task_draft_delete_request_invalid")
                    revision = payload["expected_revision"]
                    if not isinstance(revision, int) or isinstance(revision, bool):
                        raise ValueError("task_draft_delete_request_invalid")
                    task_service.delete_draft(draft_match.group(1), expected_revision=revision)
                    result = {"deleted": True, "revision": revision + 1}
                elif operation == "task_plan":
                    if set(payload) != {"project_id", "draft_revision", "input_paths", "output_paths"}:
                        raise ValueError("task_plan_request_invalid")
                    project_id = payload["project_id"]
                    draft_revision = payload["draft_revision"]
                    input_paths = payload["input_paths"]
                    output_paths = payload["output_paths"]
                    if (
                        not isinstance(project_id, str)
                        or not isinstance(draft_revision, int)
                        or isinstance(draft_revision, bool)
                        or not isinstance(input_paths, list)
                        or not isinstance(output_paths, list)
                        or not all(isinstance(item, str) for item in input_paths + output_paths)
                    ):
                        raise ValueError("task_plan_request_invalid")
                    result = task_service.review_plan(project_id=project_id, draft_revision=draft_revision, input_paths=input_paths, output_paths=output_paths)
                elif operation == "task_start":
                    if set(payload) != {"request_id", "plan_hash"} or not all(isinstance(payload.get(key), str) for key in ("request_id", "plan_hash")):
                        raise ValueError("task_start_request_invalid")
                    method = task_service.replay_start if replay_only else task_service.start
                    result = method(request_id=payload["request_id"], plan_hash=payload["plan_hash"])
                else:
                    if payload or stop_match is None:
                        raise ValueError("task_stop_request_invalid")
                    result = task_service.stop(stop_match.group(1))
                rotated = session_manager.rotate(session)
                self._json(202 if operation in {"task_start", "task_stop"} else 200, {"status": "accepted", "result": result}, session_token=rotated)
            except ConnectionError:
                return
            except (OSError, RuntimeError, ValueError) as exc:
                finding = str(exc).split(":", 1)[0]
                if not finding.startswith("task_"):
                    finding = "task_request_invalid"
                if replay_only:
                    self._json(409, {"status": "blocked", "finding": finding})
                else:
                    rotated = session_manager.rotate(session)
                    self._json(409, {"status": "blocked", "finding": finding}, session_token=rotated)

        def _review_post(
            self,
            operation: str,
            correction_match: re.Match[str] | None,
            child_match: re.Match[str] | None,
            recovery_match: re.Match[str] | None,
            session: _Session,
            *,
            replay_only: bool,
        ) -> None:
            assert review_service is not None
            try:
                payload = self._read_task_payload(allow_empty=operation == "review_recover")
                if operation == "review_correction":
                    if replay_only or correction_match is None or set(payload) != {"text", "expected_revision"}:
                        raise ValueError("candidate_correction_request_invalid")
                    text = payload.get("text")
                    revision = payload.get("expected_revision")
                    if not isinstance(text, str) or not isinstance(revision, int) or isinstance(revision, bool):
                        raise ValueError("candidate_correction_request_invalid")
                    result = review_service.save_correction(
                        correction_match.group(1), int(correction_match.group(2)),
                        text=text, expected_revision=revision,
                    )
                elif operation == "review_child_plan":
                    if child_match is None or set(payload) != {
                        "request_id", "correction_revision", "correction_hash", "review_hash"
                    }:
                        raise ValueError("candidate_child_plan_request_invalid")
                    request_id = payload.get("request_id")
                    revision = payload.get("correction_revision")
                    correction_hash = payload.get("correction_hash")
                    review_hash = payload.get("review_hash")
                    if (
                        not isinstance(request_id, str)
                        or not isinstance(revision, int)
                        or isinstance(revision, bool)
                        or not isinstance(correction_hash, str)
                        or not isinstance(review_hash, str)
                    ):
                        raise ValueError("candidate_child_plan_request_invalid")
                    if replay_only:
                        result = review_service.replay_child_plan(
                            child_match.group(1), int(child_match.group(2)),
                            request_id=request_id, correction_revision=revision,
                            correction_hash=correction_hash, review_hash=review_hash,
                        )
                    else:
                        result = review_service.review_child_plan(
                            child_match.group(1), int(child_match.group(2)),
                            request_id=request_id, correction_revision=revision,
                            correction_hash=correction_hash, review_hash=review_hash,
                            subject_id=session.subject_id,
                        )
                elif operation == "review_revision":
                    if set(payload) != {"request_id", "plan_hash"}:
                        raise ValueError("candidate_revision_request_invalid")
                    request_id = payload.get("request_id")
                    plan_hash = payload.get("plan_hash")
                    if not isinstance(request_id, str) or not isinstance(plan_hash, str):
                        raise ValueError("candidate_revision_request_invalid")
                    result = review_service.start_revision(
                        request_id=request_id, plan_hash=plan_hash, replay_only=replay_only
                    )
                elif operation == "review_continuation":
                    if set(payload) != {"request_id", "application_id"}:
                        raise ValueError("candidate_continuation_request_invalid")
                    request_id = payload.get("request_id")
                    application_id = payload.get("application_id")
                    if not isinstance(request_id, str) or not isinstance(application_id, str):
                        raise ValueError("candidate_continuation_request_invalid")
                    method = (
                        review_service.replay_continuation
                        if replay_only else review_service.continue_task
                    )
                    result = method(request_id=request_id, application_id=application_id)
                elif operation == "review_accept":
                    if set(payload) != {"request_id", "task_id", "ready_sequence", "review_hash"}:
                        raise ValueError("candidate_accept_request_invalid")
                    request_id = payload.get("request_id")
                    task_id = payload.get("task_id")
                    sequence = payload.get("ready_sequence")
                    review_hash = payload.get("review_hash")
                    if (
                        not isinstance(request_id, str) or not isinstance(task_id, str)
                        or not isinstance(sequence, int) or isinstance(sequence, bool)
                        or not isinstance(review_hash, str)
                    ):
                        raise ValueError("candidate_accept_request_invalid")
                    if replay_only:
                        result = review_service.replay_accept(
                            request_id=request_id, task_id=task_id,
                            ready_sequence=sequence, review_hash=review_hash,
                        )
                    else:
                        result = review_service.accept(
                            request_id=request_id, task_id=task_id, ready_sequence=sequence,
                            review_hash=review_hash, subject_id=session.subject_id,
                        )
                elif operation == "review_apply":
                    if set(payload) != {"request_id", "candidate", "acceptance"}:
                        raise ValueError("candidate_apply_request_invalid")
                    request_id = payload.get("request_id")
                    candidate = payload.get("candidate")
                    acceptance = payload.get("acceptance")
                    if not isinstance(request_id, str) or not isinstance(candidate, Mapping) or not isinstance(acceptance, Mapping):
                        raise ValueError("candidate_apply_request_invalid")
                    if replay_only:
                        result = review_service.replay_apply(
                            request_id=request_id, candidate=candidate, acceptance=acceptance,
                        )
                    else:
                        result = review_service.apply(
                            request_id=request_id, candidate=candidate, acceptance=acceptance,
                            subject_id=session.subject_id,
                        )
                elif operation == "review_recover":
                    if replay_only or payload or recovery_match is None:
                        raise ValueError("task_apply_recovery_request_invalid")
                    result = review_service.recover(recovery_match.group(1))
                else:
                    raise ValueError("candidate_request_invalid")
                rotated = session_manager.rotate(session)
                self._json(200, {"status": "accepted", "result": result}, session_token=rotated)
            except ConnectionError:
                return
            except (OSError, RuntimeError, ValueError) as exc:
                finding = _safe_finding(exc, "candidate_request_invalid")
                if replay_only:
                    self._json(409, {"status": "blocked", "finding": finding})
                else:
                    rotated = session_manager.rotate(session)
                    self._json(409, {"status": "blocked", "finding": finding}, session_token=rotated)

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
                    if not isinstance(confirmation_token, str) or len(confirmation_token) > 256:
                        raise ValueError("recovery_confirmation_invalid")
                    controls.consume_recovery_confirmation(
                        confirmation_token,
                        snapshot,
                        correlation_id=correlation_id,
                    )
                    assert recovery_controller is not None
                    verification = snapshot.get("verification")
                    lanes = snapshot.get("lanes")
                    if not isinstance(verification, Mapping) or not isinstance(lanes, list):
                        raise ValueError("recovery_snapshot_invalid")
                    covered = verification.get("covered_sequence")
                    manifest_generation = verification.get("manifest_generation")
                    if not isinstance(covered, int) or not isinstance(manifest_generation, int):
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
                    result = recovery_controller.abandon(
                        attempt_ids,
                        covered,
                        manifest_generation,
                    )
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
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", resolution) is None
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
                    "torq_fleet_session=" + session_token + "; HttpOnly; SameSite=Strict; Path=/",
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
