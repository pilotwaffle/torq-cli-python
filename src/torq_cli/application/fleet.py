"""Verified receipt-chain projection for the local TORQ Fleet UI."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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


def _unavailable_snapshot(
    verification: StoreVerification,
) -> dict[str, Any]:
    return {
        "schema": "torq-fleet-snapshot-v1",
        "verification": {
            "status": verification.status,
            "finding": verification.finding,
        },
        "data_status": "unavailable",
        "run": None,
        "summary": None,
        "lanes": [],
        "settlement": None,
    }


class FleetProjector:
    """Reconstruct UI state exclusively from authenticated run evidence."""

    def __init__(
        self,
        run_root: Path,
        *,
        trusted_public_key: bytes | None = None,
    ) -> None:
        self.run_root = run_root
        self.trusted_public_key = trusted_public_key

    def snapshot(self) -> dict[str, Any]:
        verification = verify_receipt_store(
            self.run_root,
            trusted_public_key=self.trusted_public_key,
        )
        if verification.status != "verified":
            return _unavailable_snapshot(verification)
        try:
            receipt_bytes = (self.run_root / "receipts.jsonl").read_bytes()
            manifest_bytes = (self.run_root / "terminal-manifest.json").read_bytes()
            receipts = [json.loads(line) for line in receipt_bytes.splitlines()]
            manifest = json.loads(manifest_bytes)
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
        return self._project(receipts, manifest)

    @staticmethod
    def _project(
        receipts: list[dict[str, Any]],
        manifest: Mapping[str, Any],
    ) -> dict[str, Any]:
        planned: list[str] = []
        run_mode: str | None = None
        profile_id: str | None = None
        run_status = "running"
        decision_reason: str | None = None
        decision_authority: str | None = None
        context_injections = 0
        lane_rows: dict[str, dict[str, Any]] = {}
        usage_rows: list[dict[str, Any]] = []

        def lane(role: str) -> dict[str, Any]:
            if role not in lane_rows:
                lane_rows[role] = {
                    "role": role,
                    "state": "queued",
                    "first_sequence": None,
                    "latest_sequence": None,
                    "latest_transition": "run_planned",
                    "latest_authority": None,
                    "provider": None,
                    "model": None,
                    "reason": None,
                    "reason_gloss": None,
                    "provider_dispatch": None,
                    "usage": None,
                    "billed_usd": None,
                    "metered_usd": None,
                    "pricing_status": None,
                    "settlement": None,
                    "entitlement": None,
                    "transitions": [],
                }
            return lane_rows[role]

        for receipt in receipts:
            transition = str(receipt.get("transition", ""))
            sequence = int(receipt.get("sequence", 0))
            observed_at = receipt.get("observed_at")
            authority = (
                str(receipt["authority"])
                if receipt.get("schema_version") == "1.1.0"
                else "legacy_unspecified"
            )
            payload = receipt.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if transition == "run_planned":
                roles = payload.get("planned_roles", ())
                if isinstance(roles, (list, tuple)):
                    planned = [str(role) for role in roles]
                    for role in planned:
                        lane(role)
                run_mode = str(payload.get("mode", "unknown"))
                profile_id = str(payload.get("profile_id", "unknown"))
                continue
            if transition == "run_decision":
                run_status = str(payload.get("status", "unknown"))
                decision_authority = authority
                raw_reason = payload.get("reason")
                decision_reason = str(raw_reason) if raw_reason is not None else None
                continue
            if transition == "context_injected":
                context_injections += 1
                target = payload.get("target_role")
                if isinstance(target, str) and target != "lead":
                    row = lane(target)
                    if row["first_sequence"] is None:
                        row["first_sequence"] = sequence
                    row["latest_sequence"] = sequence
                    row["latest_transition"] = transition
                    row["latest_authority"] = authority
                    row["transitions"].append({
                        "sequence": sequence,
                        "transition": transition,
                        "observed_at": observed_at,
                        "authority": authority,
                    })
                continue
            role_value = payload.get("role")
            if not isinstance(role_value, str):
                if transition == "repair_routed":
                    role_value = str(payload.get("target_role", ""))
                if not role_value:
                    continue
            row = lane(role_value)
            if row["first_sequence"] is None:
                row["first_sequence"] = sequence
            row["latest_sequence"] = sequence
            row["latest_transition"] = transition
            row["latest_authority"] = authority
            row["provider"] = payload.get("provider", row["provider"])
            row["model"] = payload.get("model", row["model"])
            row["transitions"].append(
                {
                    "sequence": sequence,
                    "transition": transition,
                    "observed_at": observed_at,
                    "authority": authority,
                }
            )
            if transition == "stage_started":
                row["state"] = "running"
                row["settlement"] = payload.get("settlement")
                row["entitlement"] = payload.get("entitlement")
            elif transition == "stage_completed":
                row["state"] = "sealed"
                for key in (
                    "usage", "billed_usd", "metered_usd", "pricing_status",
                    "settlement", "entitlement",
                ):
                    row[key] = payload.get(key)
                usage_rows.append({
                    "agent": role_value,
                    "provider": str(payload.get("provider", "unknown")),
                    "cost_usd": payload.get("cost_usd"),
                    "billed_usd": payload.get("billed_usd"),
                    "metered_usd": payload.get("metered_usd"),
                    "pricing_status": payload.get("pricing_status"),
                    "rate_table_version": payload.get("rate_table_version"),
                    "settlement": payload.get("settlement"),
                    "usage": payload.get("usage", "unreported"),
                })
            elif transition == "stage_blocked":
                reason = str(payload.get("reason", "blocked"))
                row["state"] = "needs_you"
                row["reason"] = reason
                row["reason_gloss"] = _gloss(reason)
                row["provider_dispatch"] = payload.get("provider_dispatch")
                row["settlement"] = payload.get("settlement")
                row["entitlement"] = payload.get("entitlement")

        planned_index = {role: index for index, role in enumerate(planned)}
        lanes = sorted(
            lane_rows.values(),
            key=lambda row: (
                row["first_sequence"] is None,
                row["first_sequence"]
                if row["first_sequence"] is not None
                else planned_index.get(str(row["role"]), len(planned_index)),
            ),
        )
        counts = {state: 0 for state in ("sealed", "running", "needs_you", "queued")}
        for row in lanes:
            state = str(row["state"])
            if state in counts:
                counts[state] += 1
        first_time = receipts[0].get("observed_at")
        last_time = receipts[-1].get("observed_at")
        times_available = isinstance(first_time, str) and isinstance(last_time, str)
        settlement = summarize_usage(usage_rows, budget_usd=0.0)["settlement"]
        awaiting_approval = run_status == "awaiting_approval"
        waiting_on: list[str] = [
            str(row["role"]) for row in lanes if row["state"] == "needs_you"
        ]
        if awaiting_approval:
            waiting_on.append("operator_approval")
        return {
            "schema": "torq-fleet-snapshot-v1",
            "verification": {"status": "verified", "finding": None},
            "data_status": "available",
            "run": {
                "run_id": manifest.get("run_id"),
                "status": run_status,
                "reason": decision_reason,
                "decision_authority": decision_authority,
                "mode": run_mode,
                "profile_id": profile_id,
                "sealed": bool(manifest.get("sealed", True)),
                "receipt_count": manifest.get("receipt_count"),
                "terminal_receipt_hash": manifest.get("terminal_receipt_hash"),
                "waiting_on": waiting_on,
                "started_at": first_time if times_available else None,
                "updated_at": last_time if times_available else None,
                "elapsed_status": (
                    "receipt_timestamps_available"
                    if times_available
                    else "receipt_timestamps_unavailable"
                ),
                "context_injections": context_injections,
            },
            "summary": {
                **counts,
                "refused": run_status == "blocked",
                "needs_operator": bool(waiting_on),
            },
            "lanes": lanes,
            "settlement": settlement,
        }


__all__ = ["FleetProjector"]
