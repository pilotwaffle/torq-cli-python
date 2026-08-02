from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from torq_cli.adapters.live_provider import _validated_role_object
from torq_cli.application.orchestrator import GovernedOrchestrator, OrchestrationBlocked
from torq_cli.domain.stage_response import (
    STAGE_RESPONSE_CONTRACT_ID,
    STAGE_RESPONSE_CONTRACT_VERSION,
    stage_response_matches,
    stage_response_schema,
)

_VALID_RESPONSES: tuple[tuple[str, Mapping[str, object]], ...] = (
    ("g1d", {"status": "design_complete", "proposal": "safe design"}),
    ("g1r", {"verdict": "approve", "rationale": "sound"}),
    ("builder", {"status": "build_complete", "proposal": "safe build"}),
    ("g2a", {"verdict": "approve", "defects": []}),
    ("refine_bug", {"status": "repair_complete", "proposal": "safe repair"}),
    ("refine_ui", {"status": "repair_complete", "proposal": "safe repair"}),
)

_INVALID_RESPONSES: tuple[tuple[str, Mapping[str, object]], ...] = (
    ("g1d", {"status": "design_complete"}),
    ("g1r", {"verdict": "approve"}),
    (
        "builder",
        {
            "status": "build_complete",
            "proposal": "safe build",
            "undeclared": "smuggled",
        },
    ),
    ("refine_ui", {"status": "repair_complete", "proposal": "x" * 121}),
    ("g2a", {"verdict": "reject", "defects": ["not-an-object"]}),
    (
        "g2a",
        {
            "verdict": "reject",
            "defects": [
                {
                    "defect_id": "defect-1",
                    "severity": "HIGH",
                    "class": "bug",
                }
            ],
        },
    ),
    (
        "g2a",
        {
            "verdict": "reject",
            "defects": [
                {
                    "defect_id": "defect-1",
                    "severity": "HIGH",
                    "class": "bug",
                    "status": "open",
                    "undeclared": "smuggled",
                }
            ],
        },
    ),
)


def test_contract_identity_and_version_are_explicit() -> None:
    assert STAGE_RESPONSE_CONTRACT_ID == "torq-stage-response"
    assert STAGE_RESPONSE_CONTRACT_VERSION == "1.0.0"
    assert stage_response_schema("g1d") is not None
    assert stage_response_schema("unknown") is None


@pytest.mark.parametrize(("role", "body"), _VALID_RESPONSES)
def test_shared_contract_accepts_each_declared_role(role: str, body: Mapping[str, object]) -> None:
    assert stage_response_matches(role, body)
    assert json.loads(_validated_role_object(json.dumps(body), "test", role)) == body
    GovernedOrchestrator()._validate_response_contract(role, body)


@pytest.mark.parametrize(("role", "body"), _INVALID_RESPONSES)
def test_adapter_and_orchestrator_reject_the_same_contract_drift(
    role: str, body: Mapping[str, object]
) -> None:
    assert not stage_response_matches(role, body)
    with pytest.raises(OrchestrationBlocked, match="^live_provider_response_invalid:test$"):
        _validated_role_object(json.dumps(body), "test", role)
    with pytest.raises(
        OrchestrationBlocked,
        match=f"^off_contract_stage:{role}:response_contract$",
    ):
        GovernedOrchestrator()._validate_response_contract(role, body)
