"""Operational worker supervision and evidence-backed exception closure."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from torq_cli.safety.evidence_broker import EvidenceBroker


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SupervisorState:
    """Atomic, explicitly non-evidentiary liveness state for one run."""

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self._lock = RLock()
        self._state: dict[str, Any] = {
            "schema": "torq-supervisor-state-v1",
            "run_id": run_id,
            "lifecycle": "registered",
            "worker_pid": None,
            "heartbeat_at": None,
            "last_covered_sequence": 0,
            "open_actions": [],
            "orphaned_roles": [],
        }
        self._persist()

    def _persist(self) -> None:
        encoded = json.dumps(
            self._state,
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary = self.path.with_suffix(".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(encoded, encoding="utf-8")
        temporary.replace(self.path)

    def update(self, **values: Any) -> dict[str, Any]:
        with self._lock:
            self._state.update(values, heartbeat_at=_now())
            self._persist()
            return dict(self._state)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)


class RunSupervisor:
    """Record only supervisor-authorized interruption evidence."""

    def __init__(self, broker: EvidenceBroker, state: SupervisorState) -> None:
        self.broker = broker
        self.state = state

    def interrupt_attempt(
        self,
        attempt: Mapping[str, Any],
        *,
        provider_dispatch: bool | str = "unknown",
        reason: str = "worker_terminated",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if reason not in {"process_exit", "worker_crash", "worker_terminated"}:
            raise ValueError("supervisor_observation_required")
        capability = self.broker.issue("supervisor")
        interrupted = self.broker.append(
            capability.token,
            "stage_interrupted",
            {
                **dict(attempt),
                "provider_dispatch": provider_dispatch,
                "reason": reason,
                "observation_source": "worker_exit",
            },
        )
        decision_capability = self.broker.issue("supervisor")
        decision = self.broker.terminalize(
            decision_capability.token,
            "run_decision",
            {
                "decision": "failed",
                "interruption_sequence": interrupted["sequence"],
                "reason": reason,
            },
        )
        self.broker.seal()
        self.state.update(
            lifecycle="failed",
            worker_pid=None,
            last_covered_sequence=decision["sequence"],
        )
        return interrupted, decision

    def mark_orphaned(self, role: str) -> dict[str, Any]:
        snapshot = self.state.snapshot()
        roles = sorted({*snapshot.get("orphaned_roles", []), role})
        return self.state.update(
            lifecycle="recovery_required",
            worker_pid=None,
            orphaned_roles=roles,
        )

    def abandon(self, attempt_ids: list[str], last_sequence: int) -> dict[str, Any]:
        capability = self.broker.issue("recovery")
        receipt = self.broker.terminalize(
            capability.token,
            "run_abandoned",
            {
                "attempt_ids": attempt_ids,
                "last_covered_sequence": last_sequence,
                "operator_assertion": "no_live_worker",
            },
        )
        self.broker.seal()
        self.state.update(
            lifecycle="abandoned",
            orphaned_roles=[],
            last_covered_sequence=receipt["sequence"],
        )
        return receipt


__all__ = ["RunSupervisor", "SupervisorState"]
