"""Verified deterministic projection for the local TORQ Fleet UI."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from torq_cli.domain.run_evidence import (
    ATTEMPT_TRANSITIONS,
    TERMINAL_ATTEMPT_TRANSITIONS,
)
from torq_cli.safety.receipts import StoreVerification, verify_receipt_store
from torq_cli.safety.usage import summarize_usage

_REASON_GLOSSES = {
    "plan_window_exceeded": "The subscription account has no remaining dispatch capacity.",
    "budget_preflight_blocked": "The metered API ceiling would be exceeded.",
    "entitlement_unknown": "No explicit settlement account covers this provider.",
    "cost_ceiling_required": "A metered lane has no configured cost ceiling.",
    "live_dispatcher_required": "No production provider dispatcher was configured.",
}


def _gloss(reason: str | None) -> str | None:
    if reason is None:
        return None
    return _REASON_GLOSSES.get(
        reason.partition(":")[0],
        "The governed run stopped before this stage could continue.",
    )


def _dispatch_message(value: object) -> str:
    if value is True:
        return "Provider transport attempted"
    if value is False:
        return "No provider transport was invoked"
    return "Dispatch status unknown"


def _unavailable_snapshot(verification: StoreVerification) -> dict[str, Any]:
    return {
        "schema": "torq-fleet-snapshot-v3",
        "verification": {
            "state": verification.status,
            "finding": verification.finding,
            "covered_sequence": None,
            "store_sequence": None,
            "manifest_generation": None,
        },
        "data_status": "unavailable",
        "run": None,
        "summary": None,
        "lanes": [],
        "actions": [],
        "settlement": None,
        "accounting": None,
    }


def _writer(receipt: Mapping[str, Any]) -> dict[str, str]:
    if receipt.get("schema_version") == "2.0.0":
        return {
            "writer_role": str(receipt.get("writer_role")),
            "evidence_basis": str(receipt.get("evidence_basis")),
            "writer_key_id": str(receipt.get("writer_key_id")),
        }
    return {
        "writer_role": "legacy_unclassified",
        "evidence_basis": "legacy_unclassified",
        "writer_key_id": "legacy_unclassified",
    }


def reduce_fleet_snapshot(
    receipts: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    verification_state: str,
    store_sequence: int | None = None,
    legacy_certificate: bool = False,
    accounting: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold one stable receipt snapshot into UI-facing normalized state."""
    lane_rows: dict[str, dict[str, Any]] = {}
    attempt_rows: dict[str, dict[str, Any]] = {}
    action_rows: dict[str, dict[str, Any]] = {}
    usage_rows: list[dict[str, Any]] = []
    catalog_order: list[str] = []
    run_mode: str | None = None
    profile_id: str | None = None
    run_status = "running"
    run_outcome: str | None = None
    decision_reason: str | None = None
    decision_writer: dict[str, str] | None = None
    context_injections = 0
    context_commands_accepted = 0
    initial_plan_hash: str | None = None
    current_plan_hash: str | None = None
    plan_revision = 0
    replans: list[dict[str, Any]] = []
    reduction_errors: list[str] = []
    abandoned_attempts: set[str] = set()

    def lane(role: str) -> dict[str, Any]:
        if role not in lane_rows:
            lane_rows[role] = {
                "role": role,
                "order": len(lane_rows),
                "kind": "legacy",
                "state": "queued",
                "provider": None,
                "model": None,
                "prompt_id": None,
                "prompt_version": None,
                "contract_id": None,
                "contract_version": None,
                "required": False,
                "active": True,
                "attempt_ordinal": None,
                "latest_sequence": None,
                "latest_transition": "run_planned",
                "latest_writer_role": None,
                "latest_evidence_basis": None,
                "latest_writer_key_id": None,
                "reason": None,
                "reason_gloss": None,
                "provider_dispatch": None,
                "dispatch_message": None,
                "usage": None,
                "billed_usd": None,
                "metered_usd": None,
                "pricing_status": None,
                "lane_settlement": None,
                "entitlement": None,
                "attempts": [],
            }
        return lane_rows[role]

    for receipt in receipts:
        transition = str(receipt.get("transition", ""))
        sequence = int(receipt.get("sequence", 0))
        observed_at = receipt.get("observed_at")
        payload = receipt.get("payload")
        if not isinstance(payload, Mapping):
            continue
        writer = _writer(receipt)
        if transition == "run_planned":
            run_mode = str(payload.get("mode", "unknown"))
            profile_id = str(payload.get("profile_id", "unknown"))
            raw_plan_hash = payload.get("plan_hash")
            if isinstance(raw_plan_hash, str):
                initial_plan_hash = raw_plan_hash
                current_plan_hash = raw_plan_hash
            catalog = payload.get("lane_catalog")
            if isinstance(catalog, list):
                for raw in catalog:
                    if not isinstance(raw, Mapping) or not isinstance(
                        raw.get("role"), str
                    ):
                        continue
                    role = str(raw["role"])
                    catalog_order.append(role)
                    row = lane(role)
                    for key in (
                        "order",
                        "kind",
                        "required",
                        "provider",
                        "model",
                        "prompt_id",
                        "prompt_version",
                        "contract_id",
                        "contract_version",
                    ):
                        row[key] = raw.get(key)
                    conditional = raw.get("kind") == "conditional"
                    row["active"] = not conditional
                    row["state"] = "dormant" if conditional else "queued"
            else:
                roles = payload.get("planned_roles", ())
                if isinstance(roles, (list, tuple)):
                    for role_value in roles:
                        role = str(role_value)
                        catalog_order.append(role)
                        lane(role)
            continue
        if transition == "run_replanned":
            raw_revision = payload.get("plan_revision")
            raw_hash = payload.get("new_plan_hash")
            if isinstance(raw_revision, int) and isinstance(raw_hash, str):
                plan_revision = raw_revision
                current_plan_hash = raw_hash
                replans.append({"sequence": sequence, **dict(payload), **writer})
            continue
        if transition == "run_decision":
            run_status = str(
                payload.get("decision", payload.get("status", "unknown"))
            )
            raw_outcome = payload.get("outcome")
            run_outcome = str(raw_outcome) if raw_outcome is not None else None
            raw_reason = payload.get("reason")
            decision_reason = str(raw_reason) if raw_reason is not None else None
            decision_writer = writer
            continue
        if transition == "run_abandoned":
            raw_attempts = payload.get("attempt_ids")
            if isinstance(raw_attempts, list):
                abandoned_attempts.update(str(value) for value in raw_attempts)
            run_status = "workflow_abandoned"
            run_outcome = "abandoned"
            decision_writer = writer
            continue
        if transition == "command_accepted" and payload.get("command_type") in {
            "context",
            "artifact",
        }:
            context_commands_accepted += 1
            continue
        if transition == "context_injected":
            context_injections += 1
            target = payload.get("target_role")
            if isinstance(target, str) and target != "lead":
                row = lane(target)
                row["latest_sequence"] = sequence
                row["latest_transition"] = transition
                row["latest_writer_role"] = writer["writer_role"]
                row["latest_evidence_basis"] = writer["evidence_basis"]
                row["latest_writer_key_id"] = writer["writer_key_id"]
            continue
        if transition == "action_opened":
            action_id = str(payload.get("action_id", ""))
            action_rows[action_id] = {
                "action_id": action_id,
                "type": payload.get("type"),
                "scope": payload.get("scope"),
                "target": payload.get("target"),
                "caused_by_sequence": payload.get("caused_by_sequence"),
                "summary": None if legacy_certificate else payload.get("summary"),
                "allowed_resolutions": (
                    [] if legacy_certificate else payload.get("allowed_resolutions", [])
                ),
                "outcome_map": (
                    {} if legacy_certificate else payload.get("outcome_map", {})
                ),
                "resolution": None,
                "resolver_identity": None,
                "opened_sequence": sequence,
                "resolved_sequence": None,
                "state": "open",
                **writer,
            }
            if legacy_certificate:
                reduction_errors.append("legacy_operator_fields_suppressed")
            continue
        if transition == "action_resolved":
            action_id = str(payload.get("action_id", ""))
            action = action_rows.get(action_id)
            if action is None:
                reduction_errors.append("action_open_missing")
            else:
                action.update(
                    {
                        "resolution": payload.get("resolution"),
                        "resolver_identity": (
                            None
                            if legacy_certificate
                            else payload.get("resolver_identity")
                        ),
                        "resolved_sequence": sequence,
                        "state": "resolved",
                        **writer,
                    }
                )
            continue
        if transition == "repair_routed":
            role = str(payload.get("target_role", ""))
            if role:
                row = lane(role)
                row["active"] = True
                row["state"] = "queued"
                row["latest_sequence"] = sequence
                row["latest_transition"] = transition
            continue
        if transition not in ATTEMPT_TRANSITIONS and transition != "stage_started":
            continue
        role_value = payload.get("role")
        if not isinstance(role_value, str):
            continue
        row = lane(role_value)
        row["active"] = True
        row["latest_sequence"] = sequence
        row["latest_transition"] = transition
        row["latest_writer_role"] = writer["writer_role"]
        row["latest_evidence_basis"] = writer["evidence_basis"]
        row["latest_writer_key_id"] = writer["writer_key_id"]
        row["provider"] = payload.get("provider", row["provider"])
        row["model"] = payload.get("model", row["model"])

        if transition == "stage_started":
            row["state"] = "running"
            continue
        attempt_id = str(payload.get("attempt_id", ""))
        attempt: dict[str, Any] | None
        if transition == "stage_attempt_created":
            row["state"] = "running"
            attempt = {
                "attempt_id": attempt_id,
                "attempt_ordinal": payload.get("attempt_ordinal"),
                "repair_cycle": payload.get("repair_cycle"),
                "state": "running",
                "provider_dispatch": False,
                "dispatch_message": _dispatch_message(False),
                "created_sequence": sequence,
                "terminal_sequence": None,
                "reason": None,
                "transitions": [],
            }
            attempt_rows[attempt_id] = attempt
            row["attempts"].append(attempt)
        else:
            attempt = attempt_rows.get(attempt_id)
            if attempt is None:
                reduction_errors.append("attempt_created_missing")
                continue
        assert attempt is not None
        attempt["transitions"].append(
            {
                "sequence": sequence,
                "transition": transition,
                "observed_at": observed_at,
                **writer,
            }
        )
        row["attempt_ordinal"] = payload.get("attempt_ordinal")
        dispatch = payload.get("provider_dispatch")
        row["provider_dispatch"] = dispatch
        row["dispatch_message"] = _dispatch_message(dispatch)
        attempt["provider_dispatch"] = dispatch
        attempt["dispatch_message"] = _dispatch_message(dispatch)
        if transition == "stage_dispatch_started":
            row["state"] = "running"
            attempt["state"] = "running"
            row["lane_settlement"] = payload.get("settlement")
            row["entitlement"] = payload.get("entitlement")
        elif transition == "stage_completed":
            row["state"] = "sealed"
            attempt["state"] = "sealed"
            for key in (
                "usage",
                "billed_usd",
                "metered_usd",
                "pricing_status",
                "entitlement",
            ):
                row[key] = payload.get(key)
            row["lane_settlement"] = payload.get("settlement")
            usage_rows.append(
                {
                    "agent": role_value,
                    "provider": str(payload.get("provider", "unknown")),
                    "cost_usd": payload.get("cost_usd"),
                    "billed_usd": payload.get("billed_usd"),
                    "metered_usd": payload.get("metered_usd"),
                    "pricing_status": payload.get("pricing_status"),
                    "rate_table_version": payload.get("rate_table_version"),
                    "settlement": payload.get("settlement"),
                    "usage": payload.get("usage", "unreported"),
                }
            )
        elif transition in {"stage_blocked", "stage_rejected"}:
            row["state"] = "blocked"
            attempt["state"] = "blocked"
        elif transition == "stage_failed":
            row["state"] = "failed"
            attempt["state"] = "failed"
        elif transition == "stage_interrupted":
            row["state"] = "interrupted"
            attempt["state"] = "interrupted"
        if transition in TERMINAL_ATTEMPT_TRANSITIONS:
            reason = str(payload.get("reason", transition))
            attempt["terminal_sequence"] = sequence
            attempt["reason"] = reason
            row["reason"] = reason
            row["reason_gloss"] = _gloss(reason)

    for row in lane_rows.values():
        attempts: list[dict[str, Any]] = row["attempts"]
        if not attempts or attempts[-1]["terminal_sequence"] is not None:
            continue
        if str(attempts[-1]["attempt_id"]) in abandoned_attempts:
            row["state"] = "abandoned"
            attempts[-1]["state"] = "abandoned"
            attempts[-1]["reason"] = "Attempt outcome unrecorded"
        elif bool(manifest.get("sealed")):
            reduction_errors.append("attempt_terminal_missing")

    planned_index = {role: index for index, role in enumerate(catalog_order)}

    def lane_order(row: Mapping[str, Any]) -> int:
        value = row.get("order")
        if isinstance(value, int):
            return value
        return planned_index.get(str(row["role"]), len(planned_index))

    lanes = sorted(
        lane_rows.values(),
        key=lane_order,
    )
    actions = sorted(action_rows.values(), key=lambda row: int(row["opened_sequence"]))
    open_actions = [action for action in actions if action["state"] == "open"]
    for action in open_actions:
        if action.get("scope") != "lane":
            continue
        target = action.get("target")
        if isinstance(target, str) and target in lane_rows:
            lane_rows[target]["state"] = "needs_you"

    states = (
        "dormant",
        "queued",
        "running",
        "sealed",
        "blocked",
        "needs_you",
        "failed",
        "interrupted",
        "abandoned",
    )
    counts = {state: sum(row["state"] == state for row in lanes) for state in states}
    first_time = receipts[0].get("observed_at") if receipts else None
    last_time = receipts[-1].get("observed_at") if receipts else None
    times_available = isinstance(first_time, str) and isinstance(last_time, str)
    settlement = summarize_usage(usage_rows, budget_usd=0.0)["settlement"]
    if run_status in {"awaiting_approval", "execution_complete_action_open"}:
        execution_state, workflow_state = "complete", "action_open"
    elif run_status in {"completed", "workflow_closed"}:
        execution_state, workflow_state = "complete", "closed"
    elif run_status == "workflow_abandoned":
        execution_state, workflow_state = "abandoned", "abandoned"
    elif run_status in {"blocked", "failed", "workflow_failed"}:
        execution_state, workflow_state = "blocked", "closed"
    else:
        execution_state, workflow_state = "running", "open"
    normalized_verification = (
        "sealed_verified"
        if verification_state == "verified" and bool(manifest.get("sealed"))
        else "live_verified"
        if verification_state == "verified"
        else verification_state
    )
    return {
        "schema": "torq-fleet-snapshot-v3",
        "verification": {
            "state": normalized_verification,
            "finding": None,
            "covered_sequence": manifest.get("receipt_count"),
            "store_sequence": (
                store_sequence
                if store_sequence is not None
                else manifest.get("receipt_count")
            ),
            "manifest_generation": manifest.get("manifest_generation"),
        },
        "data_status": "reduction_error" if reduction_errors else "available",
        "run": {
            "run_id": manifest.get("run_id"),
            "decision": run_status,
            "outcome": run_outcome,
            "execution_state": execution_state,
            "workflow_state": workflow_state,
            "reason": decision_reason,
            "decision_writer": decision_writer,
            "mode": run_mode,
            "profile_id": profile_id,
            "sealed": bool(manifest.get("sealed")),
            "receipt_count": manifest.get("receipt_count"),
            "terminal_receipt_hash": manifest.get("terminal_receipt_hash"),
            "waiting_on": [str(action["action_id"]) for action in open_actions],
            "started_at": first_time if times_available else None,
            "updated_at": last_time if times_available else None,
            "elapsed_status": (
                "receipt_timestamps_available"
                if times_available
                else "receipt_timestamps_unavailable"
            ),
            "context_injections": context_injections,
            "context_commands_accepted": context_commands_accepted,
            "plan": {
                "initial_hash": initial_plan_hash,
                "current_hash": current_plan_hash,
                "revision": plan_revision,
                "replans": replans,
            },
        },
        "summary": {
            **counts,
            "open_actions": len(open_actions),
            "needs_operator": bool(open_actions),
            "reduction_errors": reduction_errors,
        },
        "lanes": lanes,
        "actions": actions,
        "settlement": settlement,
        "accounting": dict(accounting) if accounting is not None else None,
    }


class FleetProjector:
    """Reconstruct UI state exclusively from authenticated run evidence."""

    def __init__(
        self,
        run_root: Path,
        *,
        trusted_public_key: bytes | None = None,
        operational_state: Mapping[str, Any] | None = None,
        accounting: Mapping[str, Any] | None = None,
    ) -> None:
        self.run_root = run_root
        self.trusted_public_key = trusted_public_key
        # Kept as constructor compatibility only. Operational data belongs in
        # the Fleet envelope, never in the deterministic snapshot.
        self.operational_state = operational_state
        self.accounting = accounting

    def snapshot(self) -> dict[str, Any]:
        manifest_path = self.run_root / "terminal-manifest.json"
        receipts_path = self.run_root / "receipts.jsonl"
        certificate_path = self.run_root / "run-certificate.json"
        manifest_bytes: bytes | None = None
        receipt_bytes: bytes | None = None
        certificate_bytes: bytes | None = None
        verification = StoreVerification("incomplete", "evidence_unstable")
        for _ in range(3):
            try:
                before = (
                    manifest_path.read_bytes(),
                    receipts_path.read_bytes(),
                    certificate_path.read_bytes(),
                )
            except OSError:
                break
            verification = verify_receipt_store(
                self.run_root,
                trusted_public_key=self.trusted_public_key,
            )
            try:
                after = (
                    manifest_path.read_bytes(),
                    receipts_path.read_bytes(),
                    certificate_path.read_bytes(),
                )
            except OSError:
                break
            if before == after:
                manifest_bytes, receipt_bytes, certificate_bytes = after
                break
        if (
            manifest_bytes is None
            or receipt_bytes is None
            or certificate_bytes is None
        ):
            return _unavailable_snapshot(
                StoreVerification("incomplete", "evidence_unstable")
            )
        if verification.status not in {"verified", "live_catching_up"}:
            return _unavailable_snapshot(verification)
        try:
            manifest = json.loads(manifest_bytes)
            all_lines = receipt_bytes.splitlines()
            count = manifest.get("receipt_count")
            if not isinstance(count, int) or isinstance(count, bool):
                raise ValueError("manifest_receipt_count_invalid")
            receipts = [json.loads(line) for line in all_lines[:count]]
            certificate = json.loads(certificate_bytes)
            legacy_certificate = (
                isinstance(certificate, Mapping)
                and certificate.get("certificate_schema_version") == "1.0.0"
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return _unavailable_snapshot(
                StoreVerification("incomplete", "evidence_unreadable")
            )
        if (
            not isinstance(manifest, Mapping)
            or manifest.get("receipt_count") != len(receipts)
            or not receipts
        ):
            return _unavailable_snapshot(
                StoreVerification("incomplete", "manifest_coverage_mismatch")
            )
        return reduce_fleet_snapshot(
            receipts,
            manifest,
            verification_state=verification.status,
            store_sequence=len(all_lines),
            legacy_certificate=legacy_certificate,
            accounting=self.accounting,
        )


__all__ = ["FleetProjector", "reduce_fleet_snapshot"]
