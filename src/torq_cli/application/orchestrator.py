"""Governed provider orchestration for the TORQ V5 execution profile."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from torq_cli.connectors import Connector
from torq_cli.core.engine import NormalizedResponse
from torq_cli.core.graph import ExecutionMode
from torq_cli.core.policy import Defect, G2APolicy
from torq_cli.core.redaction import PatternRegistry
from torq_cli.domain.registry_schema import BindingSpec, ProfileSpec
from torq_cli.safety.receipts import ReceiptChain
from torq_cli.safety.usage import summarize_usage


class OrchestrationBlocked(ValueError):
    """Raised when execution cannot produce trustworthy governed evidence."""


class StageDispatcher(Protocol):
    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse: ...


class ConnectorDispatcher:
    """Adapt the connector registry to profile provider identifiers."""

    _CONNECTOR_NAMES = {
        "anthropic": "claude",
        "openai": "codex",
        "moonshot": "kimi",
        "zai": "zai",
        "deepseek": "deepseek",
    }

    def __init__(self, connectors: Mapping[str, Connector]) -> None:
        self.connectors = dict(connectors)

    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse:
        connector_name = self._CONNECTOR_NAMES.get(provider, provider)
        connector = self.connectors.get(connector_name)
        if connector is None:
            raise OrchestrationBlocked(f"connector_unavailable:{provider}")
        return connector.call(model=model, prompt=prompt, agent=role)


@dataclass(frozen=True)
class OrchestrationResult:
    status: str
    planned_roles: tuple[str, ...]
    dispatched_roles: tuple[str, ...]
    proposal: Mapping[str, Any] | None
    usage: Mapping[str, Any]
    repair_cycles: int = 0
    timeline: tuple[Mapping[str, str], ...] = ()


class GovernedOrchestrator:
    """Execute the fixed gate/build/audit flow and bounded repair loop."""

    _CORE_ROLES = ("g1d", "g1r", "builder", "g2a")

    def __init__(
        self,
        dispatcher: StageDispatcher | None = None,
        *,
        loop_budget: int = 1,
        budget_usd: float = 0.0,
        cost_ceiling_usd_by_role: Mapping[str, float] | None = None,
    ) -> None:
        if loop_budget < 0:
            raise ValueError("loop_budget_must_be_nonnegative")
        if budget_usd < 0:
            raise ValueError("budget_usd_must_be_nonnegative")
        self.dispatcher = dispatcher
        self.loop_budget = loop_budget
        self.budget_usd = budget_usd
        self.cost_ceiling_usd_by_role = dict(cost_ceiling_usd_by_role or {})
        if any(cost < 0 for cost in self.cost_ceiling_usd_by_role.values()):
            raise ValueError("cost_ceiling_must_be_nonnegative")
        self.registry = PatternRegistry.default()
        self.policy = G2APolicy()

    def execute(
        self,
        *,
        goal: str,
        profile: ProfileSpec,
        mode: ExecutionMode,
        chain: ReceiptChain,
    ) -> OrchestrationResult:
        planned = self._CORE_ROLES
        missing = tuple(role for role in planned if role not in profile.bindings)
        if missing:
            raise OrchestrationBlocked("profile_binding_missing:" + ",".join(missing))
        chain.append(
            "run_planned",
            {
                "mode": mode.value,
                "profile_id": profile.profile_id,
                "strategy_id": profile.strategy_id,
                "planned_roles": planned,
            },
        )
        if mode is ExecutionMode.DRY_RUN:
            chain.append(
                "run_decision",
                {"status": "dry_run_complete", "provider_dispatch": False},
            )
            return OrchestrationResult(
                "dry_run_complete",
                planned,
                (),
                None,
                summarize_usage((), budget_usd=self.budget_usd),
                timeline=tuple(
                    {"stage": role, "status": "planned", "next_action": "No provider dispatch in dry-run mode"}
                    for role in planned
                ),
            )
        if self.dispatcher is None:
            raise OrchestrationBlocked("live_dispatcher_required")

        dispatched: list[str] = []
        usage_rows: list[dict[str, Any]] = []
        outputs: dict[str, Mapping[str, Any]] = {}

        outputs["g1d"] = self._dispatch(
            role="g1d", goal=goal, context={}, profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        self._require_value(outputs["g1d"], "status", "design_complete", "g1d")
        outputs["g1r"] = self._dispatch(
            role="g1r", goal=goal, context=outputs["g1d"], profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        self._require_verdict(outputs["g1r"], "g1r")
        if self._verdict(outputs["g1r"]) != "approve":
            return self._finish(
                chain, "design_rejected", planned, dispatched, None, usage_rows
            )

        outputs["builder"] = self._dispatch(
            role="builder", goal=goal, context=outputs["g1d"], profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        self._require_value(outputs["builder"], "status", "build_complete", "builder")
        proposal: dict[str, Any] = {
            "source_role": "builder",
            "status": outputs["builder"].get("status", "unreported"),
        }
        audit = self._dispatch(
            role="g2a", goal=goal, context=outputs["builder"], profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        self._require_verdict(audit, "g2a")

        repair_cycles = 0
        while self._verdict(audit) != "approve":
            repair_role = self._repair_role(audit)
            if repair_role == "human_escalation":
                return self._finish(
                    chain, "human_escalation", planned, dispatched, proposal,
                    usage_rows, repair_cycles=repair_cycles,
                )
            if repair_role is None:
                return self._finish(
                    chain,
                    "audit_rejected", planned, dispatched, proposal, usage_rows,
                    repair_cycles=repair_cycles,
                )
            if repair_cycles >= self.loop_budget:
                return self._finish(
                    chain,
                    "repair_budget_exhausted", planned, dispatched, proposal,
                    usage_rows, repair_cycles=repair_cycles,
                )
            chain.append(
                "repair_routed",
                {
                    "target_role": repair_role,
                    "cycle": repair_cycles + 1,
                    "targeted_reaudit": True,
                },
            )
            repair = self._dispatch(
                role=repair_role, goal=goal, context=audit, profile=profile,
                chain=chain, dispatched=dispatched, usage_rows=usage_rows,
            )
            self._require_value(repair, "status", "repair_complete", repair_role)
            repair_cycles += 1
            audit = self._dispatch(
                role="g2a", goal=goal, context=repair, profile=profile,
                chain=chain, dispatched=dispatched, usage_rows=usage_rows,
            )
            self._require_verdict(audit, "g2a")

        return self._finish(
            chain, "awaiting_approval", planned, dispatched, proposal, usage_rows,
            repair_cycles=repair_cycles,
        )

    def _dispatch(
        self,
        *,
        role: str,
        goal: str,
        context: Mapping[str, Any],
        profile: ProfileSpec,
        chain: ReceiptChain,
        dispatched: list[str],
        usage_rows: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        binding = profile.bindings.get(role)
        if binding is None:
            raise OrchestrationBlocked(f"profile_binding_missing:{role}")
        cost_usd = self._preflight_cost(role, usage_rows)
        prompt = self._prompt(role, goal, context, binding)
        clean_prompt, findings = self.registry.scan(prompt)
        chain.append(
            "stage_started",
            {
                "role": role,
                "provider": binding.provider_id,
                "model": binding.model_id,
                "prompt_id": binding.prompt_id,
                "redactions": findings,
            },
        )
        assert self.dispatcher is not None
        response = self.dispatcher.dispatch(
            role=role,
            provider=binding.provider_id,
            model=binding.model_id,
            prompt=clean_prompt,
        )
        provenance = response.provenance
        if provenance.provider != binding.provider_id or provenance.model != binding.model_id:
            raise OrchestrationBlocked(f"resolved_model_mismatch:{role}")
        if provenance.fallback_used:
            raise OrchestrationBlocked(f"unattested_fallback:{role}")
        body = dict(self._response_object(role, response.visible_text))
        artifact = chain.write_artifact(f"{len(dispatched) + 1:02d}-{role}-output", response.visible_text)
        artifact_hash = chain.hash_file(artifact)
        body["_torq_stage_evidence"] = {
            "role": role,
            "provider": provenance.provider,
            "model": provenance.model,
            "artifact_hash": artifact_hash,
        }
        tokens = sum(int(response.usage.get(name, 0)) for name in (
            "prompt_tokens", "completion_tokens", "reasoning_tokens"
        ))
        usage = {"tokens": tokens}
        usage_rows.append({
            "provider": binding.provider_id,
            "agent": role,
            "cost_usd": cost_usd,
            "cost_basis": "configured_worst_case",
            "usage": usage,
        })
        dispatched.append(role)
        chain.append(
            "stage_completed",
            {
                "role": role,
                "provider": provenance.provider,
                "model": provenance.model,
                "fallback_used": provenance.fallback_used,
                "usage": usage,
                "cost_usd": cost_usd,
                "cost_basis": "configured_worst_case",
                "artifact": str(artifact.relative_to(chain.root)),
                "artifact_hash": artifact_hash,
            },
        )
        return body

    def _preflight_cost(
        self, role: str, usage_rows: list[dict[str, Any]]
    ) -> float:
        if role not in self.cost_ceiling_usd_by_role:
            raise OrchestrationBlocked(f"cost_ceiling_required:{role}")
        ceiling = float(self.cost_ceiling_usd_by_role[role])
        consumed = sum(float(row["cost_usd"]) for row in usage_rows)
        if consumed + ceiling > self.budget_usd:
            raise OrchestrationBlocked(f"budget_preflight_blocked:{role}")
        return ceiling

    @staticmethod
    def _prompt(
        role: str,
        goal: str,
        context: Mapping[str, Any],
        binding: BindingSpec,
    ) -> str:
        return json.dumps(
            {
                "role": role,
                "goal": goal,
                "prompt_contract": binding.prompt_id,
                "prompt_version": binding.prompt_version,
                "context": context,
                "response_contract": "Return one JSON object only.",
            },
            sort_keys=True,
        )

    @staticmethod
    def _response_object(role: str, text: str) -> Mapping[str, Any]:
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OrchestrationBlocked(f"malformed_stage_response:{role}") from exc
        if not isinstance(body, Mapping):
            raise OrchestrationBlocked(f"malformed_stage_response:{role}")
        return cast(Mapping[str, Any], body)

    @staticmethod
    def _verdict(body: Mapping[str, Any]) -> str:
        return str(body.get("verdict", "reject")).lower()

    @staticmethod
    def _require_value(
        body: Mapping[str, Any], key: str, expected: str, role: str
    ) -> None:
        if str(body.get(key, "")).lower() != expected:
            raise OrchestrationBlocked(f"off_contract_stage:{role}:{key}")

    @staticmethod
    def _require_verdict(body: Mapping[str, Any], role: str) -> None:
        if str(body.get("verdict", "")).lower() not in {"approve", "reject"}:
            raise OrchestrationBlocked(f"off_contract_stage:{role}:verdict")

    def _repair_role(self, audit: Mapping[str, Any]) -> str | None:
        defects = audit.get("defects", ())
        if not isinstance(defects, list):
            raise OrchestrationBlocked("malformed_audit_defects")
        repair_lane: str | None = None
        for index, raw in enumerate(defects):
            if not isinstance(raw, Mapping):
                raise OrchestrationBlocked("malformed_audit_defect")
            defect = Defect(
                defect_id=str(raw.get("defect_id", f"defect-{index + 1}")),
                severity=str(raw.get("severity", "UNKNOWN")),
                defect_class=str(raw.get("class", raw.get("defect_class", "bug"))),
                status=str(raw.get("status", "open")),
            )
            route = self.policy.route(defect)
            if route.lane == "human_escalation":
                return "human_escalation"
            if repair_lane is None and route.lane in {"refine_bug", "refine_ui"}:
                repair_lane = route.lane
        return repair_lane

    def _finish(
        self,
        chain: ReceiptChain,
        status: str,
        planned: tuple[str, ...],
        dispatched: list[str],
        proposal: Mapping[str, Any] | None,
        usage_rows: list[dict[str, Any]],
        *,
        repair_cycles: int = 0,
    ) -> OrchestrationResult:
        chain.append(
            "run_decision",
            {
                "status": status,
                "provider_dispatch": bool(dispatched),
                "repair_cycles": repair_cycles,
            },
        )
        return self._result(
            status, planned, dispatched, proposal, usage_rows,
            repair_cycles=repair_cycles,
        )

    def _result(
        self,
        status: str,
        planned: tuple[str, ...],
        dispatched: list[str],
        proposal: Mapping[str, Any] | None,
        usage_rows: list[dict[str, Any]],
        *,
        repair_cycles: int = 0,
    ) -> OrchestrationResult:
        return OrchestrationResult(
            status,
            planned,
            tuple(dispatched),
            proposal,
            summarize_usage(usage_rows, budget_usd=self.budget_usd),
            repair_cycles,
            tuple(
                {
                    "stage": role,
                    "status": "complete",
                    "next_action": (
                        "Operator approval is required"
                        if index + 1 == len(dispatched) and status == "awaiting_approval"
                        else "Continue governed run"
                    ),
                }
                for index, role in enumerate(dispatched)
            ),
        )


__all__ = [
    "ConnectorDispatcher",
    "GovernedOrchestrator",
    "OrchestrationBlocked",
    "OrchestrationResult",
    "StageDispatcher",
]
