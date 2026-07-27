"""Operational worker supervision and evidence-backed exception closure."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
        if path.exists():
            self._state = self._load_existing()
        else:
            self._state = {
                "schema": "torq-supervisor-state-v1",
                "run_id": run_id,
                "generation": 0,
                "lifecycle": "registered",
                "worker_pid": None,
                "heartbeat_at": None,
                "last_covered_sequence": 0,
                "open_actions": [],
                "orphaned_roles": [],
            }
            self._persist()

    def _load_existing(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("supervisor_state_invalid") from exc
        required = {
            "schema",
            "run_id",
            "generation",
            "lifecycle",
            "worker_pid",
            "heartbeat_at",
            "last_covered_sequence",
            "open_actions",
            "orphaned_roles",
        }
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ValueError("supervisor_state_invalid")
        if raw.get("run_id") != self.run_id:
            raise ValueError("supervisor_run_id_mismatch")
        generation = raw.get("generation")
        worker_pid = raw.get("worker_pid")
        last_sequence = raw.get("last_covered_sequence")
        lifecycle = raw.get("lifecycle")
        valid_lifecycle = lifecycle in {
            "registered",
            "running",
            "recovery_required",
            "abandoned",
            "failed",
            "workflow_reconciled",
        }
        valid_lists = all(
            isinstance(raw.get(field), list)
            and all(isinstance(item, str) and item for item in raw[field])
            for field in ("open_actions", "orphaned_roles")
        )
        heartbeat = raw.get("heartbeat_at")
        if (
            raw.get("schema") != "torq-supervisor-state-v1"
            or not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 0
            or not valid_lifecycle
            or not (
                worker_pid is None
                or isinstance(worker_pid, int)
                and not isinstance(worker_pid, bool)
                and worker_pid > 0
            )
            or lifecycle in {"abandoned", "failed", "workflow_reconciled"}
            and worker_pid is not None
            or not (heartbeat is None or isinstance(heartbeat, str) and heartbeat)
            or not isinstance(last_sequence, int)
            or isinstance(last_sequence, bool)
            or last_sequence < 0
            or not valid_lists
        ):
            raise ValueError("supervisor_state_invalid")
        return dict(raw)

    @property
    def reconciliation_path(self) -> Path:
        """Return the non-evidentiary record path outside the run chain."""
        return self.path.with_name(f"{self.path.stem}.workflow-reconciliation.json")

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
            if (
                self._state["lifecycle"]
                in {"abandoned", "failed", "workflow_reconciled"}
                and values.get("worker_pid") is not None
            ):
                raise ValueError("supervisor_terminal_state_immutable")
            self._state.update(
                values,
                heartbeat_at=_now(),
                generation=int(self._state["generation"]) + 1,
            )
            self._persist()
            return dict(self._state)

    def record_workflow_reconciliation(
        self,
        *,
        manifest_generation: int | None,
        receipt_count: int,
        terminal_receipt_hash: str | None,
        operator_identity: str,
        reason: str,
    ) -> dict[str, Any]:
        """Close supervisor/UI lifetime without changing evidence or its manifest."""
        if (
            manifest_generation is not None
            and (
                not isinstance(manifest_generation, int)
                or isinstance(manifest_generation, bool)
                or manifest_generation < 1
            )
            or manifest_generation is None
            and reason != "legacy_run_closed"
            or manifest_generation is not None
            and reason == "legacy_run_closed"
            or not isinstance(receipt_count, int)
            or isinstance(receipt_count, bool)
            or receipt_count < 0
            or not isinstance(operator_identity, str)
            or not 1 <= len(operator_identity) <= 128
            or not isinstance(reason, str)
            or reason not in {"legacy_run_closed", "operator_reconciled"}
            or (
                terminal_receipt_hash is not None
                and (
                    not isinstance(terminal_receipt_hash, str)
                    or not terminal_receipt_hash.startswith("sha256:")
                    or len(terminal_receipt_hash) != 71
                )
            )
        ):
            raise ValueError("workflow_reconciliation_invalid")
        with self._lock:
            if self._state.get("worker_pid") is not None:
                raise ValueError("supervisor_live_worker_present")
            if self._state.get("lifecycle") in {
                "abandoned",
                "failed",
                "workflow_reconciled",
            }:
                raise ValueError("supervisor_terminal_state_immutable")
            recorded_at = _now()
            record = {
                "schema": "torq-workflow-reconciliation-v1",
                "run_id": self.run_id,
                "recorded_at": recorded_at,
                "operator_identity": operator_identity,
                "reason": reason,
                "evidence_link": {
                    "manifest_generation": manifest_generation,
                    "receipt_count": receipt_count,
                    "terminal_receipt_hash": terminal_receipt_hash,
                },
                "evidence_assertion": "none",
            }
            encoded = json.dumps(record, sort_keys=True, separators=(",", ":"))
            temporary = self.reconciliation_path.with_suffix(".tmp")
            temporary.write_text(encoded, encoding="utf-8")
            temporary.replace(self.reconciliation_path)
            self._state.update(
                lifecycle="workflow_reconciled",
                worker_pid=None,
                heartbeat_at=recorded_at,
                generation=int(self._state["generation"]) + 1,
                reconciliation_record=str(self.reconciliation_path),
            )
            self._persist()
            return record

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    @contextmanager
    def no_live_worker_guard(
        self,
        *,
        expected_generation: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Hold the operational-state generation stable during abandonment.

        Worker registration and heartbeat updates use the same lock.  Keeping
        it held through terminalization turns the operator assertion into an
        atomic check instead of a time-of-check/time-of-use promise.
        """
        with self._lock:
            generation = self._state.get("generation")
            if expected_generation is not None and generation != expected_generation:
                raise ValueError("supervisor_state_generation_changed")
            if self._state.get("worker_pid") is not None:
                raise ValueError("supervisor_live_worker_present")
            if self._state.get("lifecycle") != "recovery_required":
                raise ValueError("supervisor_recovery_not_required")
            yield dict(self._state)


class RunSupervisor:
    """Record only supervisor-authorized interruption evidence."""

    def __init__(self, broker: EvidenceBroker, state: SupervisorState) -> None:
        self.broker = broker
        self.state = state

    @property
    def root(self) -> Path:
        return self.broker.run_root

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

    def abandon(
        self,
        attempt_ids: list[str],
        last_sequence: int,
        manifest_generation: int,
        *,
        expected_state_generation: int | None = None,
    ) -> dict[str, Any]:
        with self.state.no_live_worker_guard(
            expected_generation=expected_state_generation,
        ):
            capability = self.broker.issue("recovery")
            receipt = self.broker.terminalize(
                capability.token,
                "run_abandoned",
                {
                    "attempt_ids": attempt_ids,
                    "operator_assertion": "no_live_worker",
                },
                expected_sequence=last_sequence,
                expected_manifest_generation=manifest_generation,
            )
            self.broker.seal()
            self.state.update(
                lifecycle="abandoned",
                worker_pid=None,
                orphaned_roles=[],
                last_covered_sequence=receipt["sequence"],
            )
        return receipt


__all__ = ["RunSupervisor", "SupervisorState"]
