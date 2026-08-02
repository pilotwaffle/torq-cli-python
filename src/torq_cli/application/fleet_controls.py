"""Compose and track non-evidentiary Fleet controls around a snapshot."""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from typing import Any

_ANNOTATION_PRIORITY = {
    "broker_unavailable": 0,
    "workflow_reconciled": 1,
    "orphaned": 2,
    "recovery_required": 3,
}
_OPERATIONS = frozenset({"context", "resolve_action", "recover_run"})
_MAX_RECOVERY_CONFIRMATIONS = 32


def _reason(eligible: bool, finding: str) -> dict[str, object]:
    return {"eligible": eligible, "reason": "eligible" if eligible else finding}


def _normalize_annotations(
    annotations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for annotation in annotations:
        kind = annotation.get("kind")
        scope = annotation.get("scope")
        source = annotation.get("source")
        observed_at = annotation.get("observed_at")
        if (
            kind not in _ANNOTATION_PRIORITY
            or not isinstance(scope, str)
            or source not in {"supervisor", "broker"}
            or not isinstance(observed_at, str)
        ):
            raise ValueError("fleet_annotation_invalid")
        normalized.append(
            {
                "kind": kind,
                "scope": scope,
                "observed_at": observed_at,
                "source": source,
            }
        )
    normalized.sort(
        key=lambda item: (
            _ANNOTATION_PRIORITY[str(item["kind"])],
            str(item["scope"]),
            str(item["observed_at"]),
        )
    )
    return normalized


def _mutable_reason(snapshot: Mapping[str, Any], session_write_capable: bool) -> str:
    verification = snapshot.get("verification")
    run = snapshot.get("run")
    if not session_write_capable:
        return "session_read_only"
    if not (
        isinstance(verification, Mapping)
        and verification.get("state") in {"live_verified", "sealed_verified"}
    ):
        return "verification_not_mutable"
    if snapshot.get("data_status") != "available":
        return "snapshot_not_available"
    if not isinstance(run, Mapping) or run.get("workflow_state") in {
        "closed",
        "abandoned",
    }:
        return "run_terminal"
    return "eligible"


def compose_fleet_envelope(
    snapshot: Mapping[str, Any],
    *,
    session_write_capable: bool,
    expires_at: str | None,
    service_availability: Mapping[str, bool] | None = None,
    read_only_reason: str | None = None,
    annotations: Sequence[Mapping[str, Any]] = (),
    pending: Sequence[str] = (),
) -> dict[str, Any]:
    """Add operational state without contaminating deterministic evidence state."""
    services = dict(service_availability or {})
    if set(services) - _OPERATIONS or any(
        not isinstance(value, bool) for value in services.values()
    ):
        raise ValueError("fleet_service_availability_invalid")
    normalized_annotations = _normalize_annotations(annotations)
    base_reason = _mutable_reason(snapshot, session_write_capable)
    mutable = base_reason == "eligible"

    def operation_eligibility(operation: str, precondition: bool = True) -> dict[str, object]:
        available = services.get(operation, False)
        if not mutable:
            return _reason(False, base_reason)
        if not available:
            return _reason(False, f"{operation}_service_unavailable")
        if not precondition:
            return _reason(False, f"{operation}_precondition_not_met")
        return _reason(True, "eligible")

    action_eligibility: dict[str, dict[str, object]] = {}
    raw_actions = snapshot.get("actions")
    if isinstance(raw_actions, list):
        for action in raw_actions:
            if not isinstance(action, Mapping):
                continue
            action_id = action.get("action_id")
            if not isinstance(action_id, str):
                continue
            has_contract = (
                isinstance(action.get("allowed_resolutions"), list)
                and bool(action.get("allowed_resolutions"))
                and isinstance(action.get("outcome_map"), Mapping)
            )
            action_eligibility[action_id] = operation_eligibility(
                "resolve_action",
                action.get("state") == "open" and has_contract,
            )

    recover_required = any(
        item["kind"] == "recovery_required" for item in normalized_annotations
    )
    normalized_pending: list[str] = []
    for correlation_id in pending:
        if (
            not isinstance(correlation_id, str)
            or not correlation_id
            or len(correlation_id) > 128
        ):
            raise ValueError("fleet_pending_invalid")
        if correlation_id not in normalized_pending:
            normalized_pending.append(correlation_id)

    return {
        "snapshot": dict(snapshot),
        "annotations": normalized_annotations,
        "session": {
            "write_capable": session_write_capable,
            "expires_at": expires_at,
            "read_only_reason": read_only_reason,
        },
        "eligibility": {
            "context": operation_eligibility("context"),
            "resolve_action": action_eligibility,
            "recover_run": operation_eligibility(
                "recover_run", recover_required
            ),
        },
        "pending": normalized_pending,
    }


@dataclass
class _PendingMutation:
    operation: str
    expected_sequence: int | None = None


@dataclass
class _RecoveryConfirmation:
    correlation_id: str
    run_id: str
    covered_sequence: int
    manifest_generation: int | None
    expires_at: float


class FleetControlService:
    """Own service availability and pending correlations for one run."""

    def __init__(
        self,
        *,
        context_available: bool = False,
        action_available: bool = False,
        recovery_available: bool = False,
        annotation_provider: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        confirmation_seconds: float = 60,
    ) -> None:
        self._services = {
            "context": context_available,
            "resolve_action": action_available,
            "recover_run": recovery_available,
        }
        self._annotation_provider = annotation_provider or (lambda: ())
        self._clock = clock
        self._confirmation_seconds = confirmation_seconds
        self._pending: dict[str, _PendingMutation] = {}
        self._recovery_confirmations: dict[str, _RecoveryConfirmation] = {}
        self._lock = RLock()

    def begin(self, correlation_id: str, operation: str) -> None:
        if (
            not isinstance(correlation_id, str)
            or not correlation_id
            or len(correlation_id) > 128
            or operation not in _OPERATIONS
        ):
            raise ValueError("fleet_pending_invalid")
        with self._lock:
            if correlation_id in self._pending:
                raise ValueError("fleet_correlation_duplicate")
            self._pending[correlation_id] = _PendingMutation(operation)

    def committed(self, correlation_id: str, expected_sequence: int) -> None:
        if not isinstance(expected_sequence, int) or expected_sequence <= 0:
            raise ValueError("fleet_pending_sequence_invalid")
        with self._lock:
            mutation = self._pending.get(correlation_id)
            if mutation is None:
                raise ValueError("fleet_correlation_unknown")
            mutation.expected_sequence = expected_sequence

    def failed(self, correlation_id: str) -> None:
        with self._lock:
            self._pending.pop(correlation_id, None)

    def issue_recovery_confirmation(
        self,
        snapshot: Mapping[str, Any],
        *,
        correlation_id: str,
        session_write_capable: bool,
        expires_at: str | None,
    ) -> str:
        envelope = self.envelope(
            snapshot,
            session_write_capable=session_write_capable,
            expires_at=expires_at,
        )
        eligibility = envelope["eligibility"]["recover_run"]
        if not eligibility["eligible"]:
            raise ValueError(str(eligibility["reason"]))
        run = snapshot.get("run")
        verification = snapshot.get("verification")
        if not isinstance(run, Mapping) or not isinstance(verification, Mapping):
            raise ValueError("recovery_snapshot_invalid")
        run_id = run.get("run_id")
        covered = verification.get("covered_sequence")
        generation = verification.get("manifest_generation")
        if (
            not isinstance(run_id, str)
            or not isinstance(covered, int)
            or generation is not None
            and not isinstance(generation, int)
        ):
            raise ValueError("recovery_snapshot_invalid")
        token = secrets.token_urlsafe(48)
        with self._lock:
            now = self._clock()
            self._recovery_confirmations = {
                key: value
                for key, value in self._recovery_confirmations.items()
                if value.expires_at > now
                and value.correlation_id != correlation_id
            }
            if len(self._recovery_confirmations) >= _MAX_RECOVERY_CONFIRMATIONS:
                raise ValueError("recovery_confirmation_capacity")
            self._recovery_confirmations[token] = _RecoveryConfirmation(
                correlation_id=correlation_id,
                run_id=run_id,
                covered_sequence=covered,
                manifest_generation=generation,
                expires_at=now + self._confirmation_seconds,
            )
        return token

    def consume_recovery_confirmation(
        self,
        token: str,
        snapshot: Mapping[str, Any],
        *,
        correlation_id: str,
    ) -> None:
        with self._lock:
            now = self._clock()
            self._recovery_confirmations = {
                key: value
                for key, value in self._recovery_confirmations.items()
                if value.expires_at > now or key == token
            }
            confirmation = self._recovery_confirmations.pop(token, None)
        if confirmation is None:
            raise ValueError("recovery_confirmation_invalid")
        if now >= confirmation.expires_at:
            raise ValueError("recovery_confirmation_expired")
        run = snapshot.get("run")
        verification = snapshot.get("verification")
        if (
            confirmation.correlation_id != correlation_id
            or not isinstance(run, Mapping)
            or run.get("run_id") != confirmation.run_id
            or not isinstance(verification, Mapping)
            or verification.get("covered_sequence")
            != confirmation.covered_sequence
            or verification.get("manifest_generation")
            != confirmation.manifest_generation
        ):
            raise ValueError("recovery_confirmation_stale")

    def envelope(
        self,
        snapshot: Mapping[str, Any],
        *,
        session_write_capable: bool,
        expires_at: str | None,
        read_only_reason: str | None = None,
    ) -> dict[str, Any]:
        verification = snapshot.get("verification")
        covered = (
            verification.get("covered_sequence")
            if isinstance(verification, Mapping)
            else None
        )
        with self._lock:
            if isinstance(covered, int):
                self._pending = {
                    key: value
                    for key, value in self._pending.items()
                    if value.expected_sequence is None
                    or value.expected_sequence > covered
                }
            pending = tuple(self._pending)
        return compose_fleet_envelope(
            snapshot,
            session_write_capable=session_write_capable,
            expires_at=expires_at,
            service_availability=self._services,
            read_only_reason=read_only_reason,
            annotations=self._annotation_provider(),
            pending=pending,
        )


__all__ = ["FleetControlService", "compose_fleet_envelope"]
