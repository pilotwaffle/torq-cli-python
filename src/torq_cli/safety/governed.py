"""Governed run state machine and structurally required G2A evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class RunState(str, Enum):
    AWAITING_APPROVAL = "awaiting_approval"
    LOOP_BUDGET_EXHAUSTED = "loop_budget_exhausted"
    OFF_CONTRACT = "off_contract"


@dataclass(frozen=True)
class EvidenceBundle:
    changed_files: tuple[str, ...]
    diff_hash: str
    test_command: str
    test_results: str
    artifact_hashes: dict[str, str]

    def validate(self) -> None:
        if not self.changed_files or not self.diff_hash or not self.test_command or not self.test_results or not self.artifact_hashes:
            raise ValueError("g2a_evidence_incomplete")
        if set(self.changed_files) != set(self.artifact_hashes):
            raise ValueError("g2a_evidence_incomplete:artifact_hashes")


@dataclass(frozen=True)
class TimelineEvent:
    stage: str
    status: str
    next_action: str


@dataclass(frozen=True)
class GovernedResult:
    state: RunState
    timeline: tuple[TimelineEvent, ...]
    receipt: dict[str, Any]


class GovernedRun:
    def __init__(self, *, loop_budget: int) -> None:
        self.loop_budget = loop_budget

    def audit(self, evidence: EvidenceBundle) -> None:
        evidence.validate()

    def execute(
        self,
        *,
        evidence: EvidenceBundle,
        defect: dict[str, str] | None,
        escalation_trigger: str | None = None,
        escalation_model: str | None = None,
        escalation_cost: float = 0.0,
    ) -> GovernedResult:
        self.audit(evidence)
        needs_repair = defect is not None and defect.get("severity") in {"CRITICAL", "HIGH"}
        stages = ["Design", "Review", "Build", "Audit"]
        if needs_repair and self.loop_budget < 1:
            timeline = tuple(TimelineEvent(stage, "complete", "Continue governed run") for stage in stages)
            return GovernedResult(RunState.LOOP_BUDGET_EXHAUSTED, timeline, {"halt": "loop_budget_exhausted"})
        routing = None
        if needs_repair:
            routing = "refine_ui" if defect and defect.get("class") == "ui" else "refine_bug"
            stages.extend(("Repair", "Re-audit"))
        stages.append("Awaiting approval")
        timeline = tuple(
            TimelineEvent(stage, "waiting" if stage == "Awaiting approval" else "complete", "Operator approval is required" if stage == "Awaiting approval" else f"Proceed to {stages[index + 1]}")
            for index, stage in enumerate(stages)
        )
        escalation = None
        if escalation_trigger:
            if not escalation_model:
                return GovernedResult(RunState.OFF_CONTRACT, timeline, {"halt": "escalation_model_missing"})
            escalation = {"trigger": escalation_trigger, "model": escalation_model, "cost_usd": escalation_cost}
        receipt = {"routing": routing, "targeted_reaudit": needs_repair, "escalation": escalation, "evidence": evidence.__dict__}
        return GovernedResult(RunState.AWAITING_APPROVAL, timeline, receipt)

