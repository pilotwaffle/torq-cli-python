"""Deterministic identities for initial and revised governed run plans."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from torq_cli.core.canonical_json import canonical_json

PLAN_CONTRACT = "torq-run-plan-v1"
REVISION_CONTRACT = "torq-run-plan-revision-v1"


def initial_plan_body(
    *,
    profile_id: str,
    strategy_id: str,
    planned_roles: Sequence[str],
    lane_catalog: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": PLAN_CONTRACT,
        "profile_id": profile_id,
        "strategy_id": strategy_id,
        "planned_roles": list(planned_roles),
        "lane_catalog": [dict(row) for row in lane_catalog],
    }


def revision_body(
    *,
    plan_revision: int,
    previous_plan_hash: str,
    command_id: str,
    accepted_sequence: int,
    affected_future_attempts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": REVISION_CONTRACT,
        "plan_revision": plan_revision,
        "previous_plan_hash": previous_plan_hash,
        "reason": "operator_context",
        "command_id": command_id,
        "accepted_sequence": accepted_sequence,
        "affected_future_attempts": [dict(row) for row in affected_future_attempts],
    }


def plan_hash(body: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()


__all__ = [
    "PLAN_CONTRACT",
    "REVISION_CONTRACT",
    "initial_plan_body",
    "plan_hash",
    "revision_body",
]
