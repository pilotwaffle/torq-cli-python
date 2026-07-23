"""Executable G2A routing policy v3.1.3."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Defect:
    defect_id: str
    severity: str
    defect_class: str
    status: str


@dataclass(frozen=True)
class Route:
    bucket: str
    lane: str
    subsystem_blocked: bool


class G2APolicy:
    version = "3.1.3"

    def route(self, defect: Defect) -> Route:
        severity = defect.severity.upper()
        if severity == "CRITICAL":
            return Route("A", "human_escalation", True)
        if severity == "HIGH":
            lane = "refine_ui" if defect.defect_class == "ui" else "refine_bug"
            return Route("B", lane, True)
        if severity in {"MEDIUM", "LOW"}:
            return Route("C", "bounded_repair", False)
        return Route("D", "observe", False)

    def queue_paused(self, defects: Sequence[Defect]) -> bool:
        return any(d.severity.upper() in {"CRITICAL", "HIGH"} and d.status != "resolved" for d in defects)

    def validate_fix(self, *, changed_paths: Sequence[str]) -> None:
        non_tests = [p for p in changed_paths if not p.replace("\\", "/").startswith(("tests/", "test/"))]
        if not non_tests:
            raise ValueError("no_test_only_fix")

    def receipt(self, *, run_id: str, defects: Sequence[Defect]) -> dict[str, Any]:
        return {"run_id": run_id, "policy_version": self.version, "defects": [asdict(d) for d in defects]}

