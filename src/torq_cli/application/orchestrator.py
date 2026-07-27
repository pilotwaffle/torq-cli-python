"""Governed provider orchestration for the TORQ V5 execution profile."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from torq_cli.connectors import Connector
from torq_cli.application.artifact_extraction import (
    ArtifactExtractionError,
    extract_supported_artifact,
    normalize_media_type,
    validate_source_label,
)
from torq_cli.core.engine import NormalizedResponse
from torq_cli.core.graph import ExecutionMode
from torq_cli.core.policy import Defect, G2APolicy
from torq_cli.core.redaction import PatternRegistry, RedactionBlocked
from torq_cli.domain.registry_schema import BindingSpec, ProfileSpec
from torq_cli.domain.run_evidence import CONDITIONAL_LANES, LANE_ORDER, is_bounded_media_type
from torq_cli.domain.stage_response import (
    STAGE_RESPONSE_CONTRACT_ID,
    STAGE_RESPONSE_CONTRACT_VERSION,
    stage_response_matches,
)
from torq_cli.domain.run_plan import (
    PLAN_CONTRACT,
    initial_plan_body,
    plan_hash,
    revision_body,
)
from torq_cli.safety.entitlements import EntitlementLedger, PlanWindow
from torq_cli.safety.pricing import RateTable, load_default_rate_table
from torq_cli.safety.receipts import ReceiptWriter
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


@dataclass(frozen=True)
class StageBudget:
    settlement: str
    ceiling_usd: float
    window: PlanWindow | None = None
    projected_calls: int = 0


@dataclass(frozen=True)
class StageAttempt:
    attempt_id: str
    role: str
    ordinal: int
    repair_cycle: int


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

    _PLANNED_ROLES = ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")

    def __init__(
        self,
        dispatcher: StageDispatcher | None = None,
        *,
        loop_budget: int = 1,
        budget_usd: float = 0.0,
        cost_ceiling_usd_by_role: Mapping[str, float] | None = None,
        entitlement_ledger: EntitlementLedger | None = None,
        projected_calls_by_role: Mapping[str, int] | None = None,
        rate_table: RateTable | None = None,
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
        self.entitlement_ledger = entitlement_ledger
        self.projected_calls_by_role = dict(projected_calls_by_role or {})
        if any(calls < 0 for calls in self.projected_calls_by_role.values()):
            raise ValueError("projected_calls_must_be_nonnegative")
        self.rate_table = rate_table or load_default_rate_table()
        self.registry = PatternRegistry.default()
        self.policy = G2APolicy()
        self._attempt_ordinals: dict[str, int] = {}

    def inject_context(
        self,
        chain: ReceiptWriter,
        content: str,
        *,
        target_role: str | None = None,
        media_type: str = "text/plain",
        source_name: str | None = None,
        confirm_direct: bool = False,
        command_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Durably acknowledge sanitized operator context and queue it."""
        command_id = command_id or "cmd-" + secrets.token_hex(12)
        existing = self._existing_command(chain, command_id)
        if existing is not None:
            return existing
        target = target_role or "lead"
        encoded = b""
        safe_media_type: str | None = None
        safe_source_name: str | None = None
        try:
            encoded = content.encode("utf-8", errors="strict")
            if not content.strip():
                raise OrchestrationBlocked("context_empty")
            if len(encoded) > 1_048_576:
                raise OrchestrationBlocked("context_too_large")
            if target != "lead" and target not in self._PLANNED_ROLES:
                raise OrchestrationBlocked("context_target_invalid")
            if target != "lead" and not confirm_direct:
                raise OrchestrationBlocked("context_direct_confirmation_required")
            if source_name is not None:
                safe_source_name = validate_source_label(source_name)
                scanned_name, name_findings = self.registry.scan(safe_source_name)
                if scanned_name != safe_source_name or name_findings:
                    raise OrchestrationBlocked("context_source_name_sensitive")
            normalized_media_type = normalize_media_type(media_type)
            safe_media_type = (
                normalized_media_type
                if is_bounded_media_type(normalized_media_type)
                else None
            )
            media_type = normalized_media_type
            if safe_media_type is None or not (
                media_type.startswith("text/")
                or media_type in {
                    "application/json",
                    "application/xml",
                    "application/yaml",
                    "application/x-yaml",
                }
            ):
                raise OrchestrationBlocked("context_media_type_invalid")
            clean, findings = self.registry.scan(content)
        except (
            ArtifactExtractionError,
            OrchestrationBlocked,
            RedactionBlocked,
            UnicodeError,
        ) as exc:
            finding = "context_encoding_invalid" if isinstance(exc, UnicodeError) else str(exc)
            chain.append(
                "command_rejected",
                {
                    "command_id": command_id,
                    "command_type": "context",
                    "target_role": target if isinstance(target, str) else None,
                    "media_type": safe_media_type,
                    "source_name": safe_source_name,
                    "content_bytes": len(encoded),
                    "finding": finding,
                    "earliest_eligible_attempt": None,
                    "provider_dispatch": False,
                },
                writer_role="operator_gateway",
                evidence_basis="submitted",
            )
            raise
        context_id = "ctx-" + secrets.token_hex(8)
        artifact = chain.write_artifact(context_id, clean)
        artifact_hash = chain.hash_file(artifact)
        accepted = chain.append(
            "command_accepted",
            {
                "command_id": command_id,
                "command_type": "context",
                "context_id": context_id,
                "target_role": target,
                "route": "lead_replan" if target == "lead" else "direct_lane",
                "artifact": str(artifact.relative_to(chain.root)),
                "artifact_hash": artifact_hash,
                "media_type": media_type,
                "source_name": safe_source_name,
                "content_bytes": len(clean.encode("utf-8")),
                "redactions": findings,
                "earliest_eligible_attempt": {
                    "kind": "attempt_created_after_acknowledgement"
                },
                "provider_dispatch": False,
                "direct_route_confirmed": target == "lead" or confirm_direct,
            },
            writer_role="operator_gateway",
            evidence_basis="submitted",
        )
        payload: dict[str, Any] = {
            "command_id": command_id,
            "accepted_sequence": accepted["sequence"],
            "context_id": context_id,
            "target_role": target,
            "route": "lead_replan" if target == "lead" else "direct_lane",
            "media_type": media_type,
            "source_name": safe_source_name,
            "content_bytes": len(clean.encode("utf-8")),
            "redactions": findings,
            "artifact": str(artifact.relative_to(chain.root)),
            "artifact_hash": artifact_hash,
            "provider_dispatch": False,
            "direct_route_confirmed": target == "lead" or confirm_direct,
        }
        return {
            **payload,
            "status": "accepted",
            "sequence": accepted["sequence"],
            "receipt_hash": accepted["receipt_hash"],
        }

    def inject_artifact(
        self,
        chain: ReceiptWriter,
        content: bytes,
        *,
        media_type: str,
        source_name: str,
        target_role: str | None = None,
        confirm_direct: bool = False,
        command_id: str | None = None,
    ) -> Mapping[str, Any]:
        """Extract, sanitize, encrypt, and durably acknowledge a supported file."""
        command_id = command_id or "cmd-" + secrets.token_hex(12)
        existing = self._existing_command(chain, command_id)
        if existing is not None:
            return existing
        target = target_role or "lead"
        safe_media_type: str | None = None
        safe_source_name: str | None = None
        try:
            normalized_media_type = normalize_media_type(media_type)
            safe_media_type = (
                normalized_media_type
                if is_bounded_media_type(normalized_media_type)
                else None
            )
            media_type = normalized_media_type
            if safe_media_type is None:
                raise ArtifactExtractionError("artifact_media_type_unsupported")
            if target != "lead" and target not in self._PLANNED_ROLES:
                raise OrchestrationBlocked("context_target_invalid")
            if target != "lead" and not confirm_direct:
                raise OrchestrationBlocked("context_direct_confirmation_required")
            extracted = extract_supported_artifact(
                content,
                media_type=normalized_media_type,
                source_name=source_name,
            )
            safe_source_name = extracted.source_name
            safe_name, name_findings = self.registry.scan(extracted.source_name)
            if safe_name != extracted.source_name or name_findings:
                raise ArtifactExtractionError("artifact_source_name_sensitive")
            clean, findings = self.registry.scan(extracted.text)
        except (ArtifactExtractionError, OrchestrationBlocked, RedactionBlocked) as exc:
            chain.append(
                "command_rejected",
                {
                    "command_id": command_id,
                    "command_type": "artifact",
                    "target_role": target,
                    "media_type": safe_media_type,
                    "source_name": safe_source_name,
                    "content_bytes": len(content),
                    "finding": str(exc),
                    "earliest_eligible_attempt": None,
                    "provider_dispatch": False,
                },
                writer_role="operator_gateway",
                evidence_basis="submitted",
            )
            raise
        context_id = "ctx-" + secrets.token_hex(8)
        artifact = chain.write_artifact(context_id, clean)
        payload: dict[str, Any] = {
            "command_id": command_id,
            "command_type": "artifact",
            "context_id": context_id,
            "target_role": target,
            "route": "lead_replan" if target == "lead" else "direct_lane",
            "artifact": str(artifact.relative_to(chain.root)),
            "artifact_hash": chain.hash_file(artifact),
            "media_type": extracted.media_type,
            "source_name": extracted.source_name,
            "content_bytes": len(clean.encode("utf-8")),
            "redactions": findings,
            "extraction": extracted.evidence(),
            "earliest_eligible_attempt": {
                "kind": "attempt_created_after_acknowledgement"
            },
            "provider_dispatch": False,
            "direct_route_confirmed": target == "lead" or confirm_direct,
        }
        accepted = chain.append(
            "command_accepted",
            payload,
            writer_role="operator_gateway",
            evidence_basis="submitted",
        )
        return {
            **payload,
            "status": "accepted",
            "accepted_sequence": accepted["sequence"],
            "sequence": accepted["sequence"],
            "receipt_hash": accepted["receipt_hash"],
        }

    def resolve_action(
        self,
        chain: ReceiptWriter,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]:
        """Resolve one open action and seal its linked workflow closure."""
        try:
            return chain.resolve_action(
                action_id=action_id,
                resolution=resolution,
                resolver_identity=resolver_identity,
            )
        except ValueError as exc:
            raise OrchestrationBlocked(str(exc)) from exc

    @staticmethod
    def _existing_command(
        chain: ReceiptWriter, command_id: str
    ) -> Mapping[str, Any] | None:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", command_id) is None:
            raise OrchestrationBlocked("command_id_invalid")
        if not (chain.root / "terminal-manifest.json").exists():
            return None
        for receipt in chain.covered_receipts():
            payload = receipt.get("payload")
            if not isinstance(payload, Mapping) or payload.get("command_id") != command_id:
                continue
            transition = receipt.get("transition")
            if transition == "command_rejected":
                raise OrchestrationBlocked(str(payload.get("finding", "command_rejected")))
            if transition == "command_accepted":
                return {
                    **dict(payload),
                    "status": "accepted",
                    "accepted_sequence": receipt["sequence"],
                    "sequence": receipt["sequence"],
                    "receipt_hash": receipt["receipt_hash"],
                    "idempotent_replay": True,
                }
        return None

    def execute(
        self,
        *,
        goal: str,
        profile: ProfileSpec,
        mode: ExecutionMode,
        chain: ReceiptWriter,
    ) -> OrchestrationResult:
        planned = self._PLANNED_ROLES
        lane_catalog = self._lane_catalog(profile, mode=mode)
        initial_plan = initial_plan_body(
            profile_id=profile.profile_id,
            strategy_id=profile.strategy_id,
            planned_roles=planned,
            lane_catalog=lane_catalog,
        )
        self._attempt_ordinals = {}
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
                "plan_contract": PLAN_CONTRACT,
                "plan_hash": plan_hash(initial_plan),
                "lane_catalog": lane_catalog,
                "rate_table_version": self.rate_table.version,
                "rate_table_hash": self.rate_table.sha256,
            },
        )
        if mode is ExecutionMode.DRY_RUN:
            chain.terminalize(
                "run_decision",
                {
                    "decision": "completed",
                    "outcome": "dry_run_complete",
                    "provider_dispatch": False,
                },
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
        try:
            return self._execute_live(
                goal=goal,
                profile=profile,
                chain=chain,
                planned=planned,
                dispatched=dispatched,
                usage_rows=usage_rows,
            )
        except OrchestrationBlocked as exc:
            # A refusal that leaves no terminal receipt is indistinguishable
            # from a run that never happened. Record the decision, then let the
            # caller seal and re-raise.
            failed_attempt = any(
                receipt.get("transition") == "stage_failed"
                for receipt in chain.covered_receipts()
            )
            chain.terminalize(
                "run_decision",
                {
                    "decision": "failed" if dispatched or failed_attempt else "blocked",
                    "reason": str(exc),
                    "provider_dispatch": bool(dispatched),
                    "dispatched_roles": tuple(dispatched),
                },
            )
            raise

    def _execute_live(
        self,
        *,
        goal: str,
        profile: ProfileSpec,
        chain: ReceiptWriter,
        planned: tuple[str, ...],
        dispatched: list[str],
        usage_rows: list[dict[str, Any]],
    ) -> OrchestrationResult:
        outputs: dict[str, Mapping[str, Any]] = {}

        outputs["g1d"] = self._dispatch(
            role="g1d", goal=goal, context={}, profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        outputs["g1r"] = self._dispatch(
            role="g1r", goal=goal, context=outputs["g1d"], profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        if self._verdict(outputs["g1r"]) != "approve":
            return self._finish(
                chain, "design_rejected", planned, dispatched, None, usage_rows
            )

        outputs["builder"] = self._dispatch(
            role="builder", goal=goal, context=outputs["g1d"], profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )
        proposal: dict[str, Any] = {
            "source_role": "builder",
            "status": outputs["builder"].get("status", "unreported"),
        }
        audit = self._dispatch(
            role="g2a", goal=goal, context=outputs["builder"], profile=profile,
            chain=chain, dispatched=dispatched, usage_rows=usage_rows,
        )

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
            repair_attempt = self._allocate_attempt(
                repair_role,
                repair_cycle=repair_cycles + 1,
            )
            chain.append(
                "repair_routed",
                {
                    "target_role": repair_role,
                    "cycle": repair_cycles + 1,
                    "attempt_id": repair_attempt.attempt_id,
                    "attempt_ordinal": repair_attempt.ordinal,
                    "targeted_reaudit": True,
                },
            )
            repair = self._dispatch(
                role=repair_role, goal=goal, context=audit, profile=profile,
                chain=chain, dispatched=dispatched, usage_rows=usage_rows,
                attempt=repair_attempt,
            )
            repair_cycles += 1
            audit = self._dispatch(
                role="g2a", goal=goal, context=repair, profile=profile,
                chain=chain, dispatched=dispatched, usage_rows=usage_rows,
                repair_cycle=repair_cycles,
            )

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
        chain: ReceiptWriter,
        dispatched: list[str],
        usage_rows: list[dict[str, Any]],
        attempt: StageAttempt | None = None,
        repair_cycle: int = 0,
    ) -> Mapping[str, Any]:
        binding = profile.bindings.get(role)
        if binding is None:
            raise OrchestrationBlocked(f"profile_binding_missing:{role}")
        attempt = attempt or self._allocate_attempt(role, repair_cycle=repair_cycle)
        attempt_evidence = self._attempt_evidence(attempt)
        created = chain.append(
            "stage_attempt_created",
            {
                **attempt_evidence,
                "provider": binding.provider_id,
                "model": binding.model_id,
                "prompt_id": binding.prompt_id,
                "provider_dispatch": False,
            },
        )
        window = (
            self.entitlement_ledger.window(binding.provider_id)
            if self.entitlement_ledger is not None
            else None
        )
        try:
            budget = self._preflight_stage(role, binding, usage_rows, window)
        except OrchestrationBlocked as exc:
            settlement = window.settlement if window is not None else "metered"
            chain.append(
                "stage_blocked",
                {
                    **attempt_evidence,
                    "role": role,
                    "provider": binding.provider_id,
                    "model": binding.model_id,
                    "prompt_id": binding.prompt_id,
                    "reason": str(exc),
                    "basis": (
                        "plan_entitlement"
                        if settlement in {"plan_covered", "unknown"}
                        else "configured_worst_case"
                    ),
                    "usage": self._usage_record({}),
                    "cost_usd": "0",
                    "billed_usd": "0",
                    "metered_usd": "0",
                    "settlement": settlement,
                    "entitlement": window.as_receipt() if window is not None else None,
                    "provider_dispatch": False,
                },
            )
            raise
        try:
            clean_prompt = self._start_dispatch(
                role=role,
                goal=goal,
                context=context,
                binding=binding,
                budget=budget,
                attempt_evidence=attempt_evidence,
                attempt_created_sequence=int(created["sequence"]),
                chain=chain,
            )
        except Exception as error:
            reason = (
                str(error)
                if isinstance(error, OrchestrationBlocked)
                else f"unexpected_stage_failure:{role}:{type(error).__name__}"
            )
            chain.append(
                "stage_failed",
                {
                    **attempt_evidence,
                    "provider": binding.provider_id,
                    "model": binding.model_id,
                    "reason": reason,
                    "provider_dispatch": False,
                },
            )
            if isinstance(error, OrchestrationBlocked):
                raise
            raise OrchestrationBlocked(reason) from error
        assert self.dispatcher is not None
        dispatched.append(role)
        try:
            response = self.dispatcher.dispatch(
                role=role,
                provider=binding.provider_id,
                model=binding.model_id,
                prompt=clean_prompt,
            )
            provenance = response.provenance
            if (
                provenance.provider != binding.provider_id
                or provenance.model != binding.model_id
            ):
                raise OrchestrationBlocked(f"resolved_model_mismatch:{role}")
            if provenance.fallback_used:
                raise OrchestrationBlocked(f"unattested_fallback:{role}")
            body = dict(self._response_object(role, response.visible_text))
            self._validate_response_contract(role, body)
            artifact = chain.write_artifact(
                f"{chain.sequence + 1:02d}-{role}-output",
                response.visible_text,
            )
            artifact_hash = chain.hash_file(artifact)
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, OrchestrationBlocked)
                else f"unexpected_stage_failure:{role}:{type(exc).__name__}"
            )
            chain.append(
                "stage_failed",
                {
                    **attempt_evidence,
                    "provider": binding.provider_id,
                    "model": binding.model_id,
                    "reason": reason,
                    "provider_dispatch": True,
                },
            )
            if isinstance(exc, OrchestrationBlocked):
                raise
            raise OrchestrationBlocked(reason) from exc
        try:
            return self._complete_stage(
                role=role,
                binding=binding,
                budget=budget,
                response=response,
                body=body,
                artifact=artifact,
                artifact_hash=artifact_hash,
                attempt_evidence=attempt_evidence,
                chain=chain,
                usage_rows=usage_rows,
            )
        except Exception as exc:
            reason = (
                str(exc)
                if isinstance(exc, OrchestrationBlocked)
                else f"unexpected_stage_failure:{role}:{type(exc).__name__}"
            )
            chain.append(
                "stage_failed",
                {
                    **attempt_evidence,
                    "provider": binding.provider_id,
                    "model": binding.model_id,
                    "reason": reason,
                    "provider_dispatch": True,
                },
            )
            if isinstance(exc, OrchestrationBlocked):
                raise
            raise OrchestrationBlocked(reason) from exc

    def _complete_stage(
        self,
        *,
        role: str,
        binding: BindingSpec,
        budget: StageBudget,
        response: NormalizedResponse,
        body: dict[str, Any],
        artifact: Path,
        artifact_hash: str,
        attempt_evidence: Mapping[str, Any],
        chain: ReceiptWriter,
        usage_rows: list[dict[str, Any]],
    ) -> Mapping[str, Any]:
        provenance = response.provenance
        body["_torq_stage_evidence"] = {
            "role": role,
            "provider": provenance.provider,
            "model": provenance.model,
            "artifact_hash": artifact_hash,
        }
        usage = self._usage_record(response.usage)
        entitlement: PlanWindow | None
        if self.entitlement_ledger is not None and budget.settlement == "plan_covered":
            self.entitlement_ledger.reconcile(binding.provider_id, calls=1)
            entitlement = self.entitlement_ledger.window(binding.provider_id)
        else:
            entitlement = budget.window
        quote = self.rate_table.quote(binding.provider_id, binding.model_id, usage)
        billed_usd: str | None
        if self.entitlement_ledger is None:
            billed_usd = None
            cost_usd = str(budget.ceiling_usd)
            cost_basis = "configured_worst_case"
        elif budget.settlement == "plan_covered":
            billed_usd = "0"
            cost_usd = "0"
            cost_basis = "plan_entitlement"
        elif quote.metered_usd is not None:
            billed_usd = quote.metered_usd
            cost_usd = quote.metered_usd
            cost_basis = "sealed_token_counts"
        else:
            billed_usd = None
            cost_usd = str(budget.ceiling_usd)
            cost_basis = "configured_worst_case_rate_unknown"
        usage_rows.append(
            {
                "provider": binding.provider_id,
                "agent": role,
                "cost_usd": cost_usd,
                "billed_usd": billed_usd,
                "metered_usd": quote.metered_usd,
                "settlement": budget.settlement,
                "pricing_status": quote.pricing_status,
                "rate_table_version": quote.rate_table_version,
                "rate_table_hash": quote.rate_table_hash,
                "preflight_ceiling_usd": budget.ceiling_usd,
                "cost_basis": cost_basis,
                "usage": usage,
            }
        )
        rejected = role in {"g1r", "g2a"} and self._verdict(body) != "approve"
        terminal_transition = (
            "stage_rejected"
            if rejected
            else "stage_completed"
        )
        chain.append(
            terminal_transition,
            {
                **attempt_evidence,
                "provider": provenance.provider,
                "model": provenance.model,
                "fallback_used": provenance.fallback_used,
                "usage": usage,
                "cost_usd": cost_usd,
                "billed_usd": billed_usd,
                "metered_usd": quote.metered_usd,
                "settlement": budget.settlement,
                "pricing_status": quote.pricing_status,
                "rate_table_version": quote.rate_table_version,
                "rate_table_hash": quote.rate_table_hash,
                "cost_basis": cost_basis,
                "entitlement": (
                    entitlement.as_receipt() if entitlement is not None else None
                ),
                "artifact": str(artifact.relative_to(chain.root)),
                "artifact_hash": artifact_hash,
                "provider_dispatch": True,
                **(
                    {
                        "verdict": "reject",
                        "reason": (
                            "design_rejected" if role == "g1r" else "audit_rejected"
                        ),
                    }
                    if rejected
                    else {}
                ),
            },
        )
        return body

    def _start_dispatch(
        self,
        *,
        role: str,
        goal: str,
        context: Mapping[str, Any],
        binding: BindingSpec,
        budget: StageBudget,
        attempt_evidence: Mapping[str, Any],
        attempt_created_sequence: int,
        chain: ReceiptWriter,
    ) -> str:
        injected = self._consume_context(
            chain,
            role=role,
            attempt_evidence=attempt_evidence,
            attempt_created_sequence=attempt_created_sequence,
        )
        prompt_context: dict[str, Any] = dict(context)
        if injected:
            prompt_context["injected_context"] = [
                {"context_id": item["context_id"], "content": item["content"]}
                for item in injected
            ]
        prompt = self._prompt(role, goal, prompt_context, binding)
        clean_prompt, findings = self.registry.scan(prompt)
        if (
            self.entitlement_ledger is not None
            and budget.settlement == "plan_covered"
        ):
            try:
                self.entitlement_ledger.reserve(
                    binding.provider_id,
                    calls=budget.projected_calls,
                )
            except ValueError as exc:
                raise OrchestrationBlocked(f"plan_window_exceeded:{role}") from exc
        try:
            chain.append(
                "stage_dispatch_started",
                {
                    **attempt_evidence,
                    "provider": binding.provider_id,
                    "model": binding.model_id,
                    "prompt_id": binding.prompt_id,
                    "redactions": findings,
                    "context_ids": tuple(item["context_id"] for item in injected),
                    "command_ids": tuple(item["command_id"] for item in injected),
                    "settlement": budget.settlement,
                    "entitlement": (
                        budget.window.as_receipt() if budget.window is not None else None
                    ),
                    "provider_dispatch": True,
                },
            )
        except Exception:
            if self.entitlement_ledger is not None and budget.settlement == "plan_covered":
                try:
                    self.entitlement_ledger.cancel(
                        binding.provider_id,
                        calls=budget.projected_calls,
                    )
                except Exception as rollback_exc:
                    raise OrchestrationBlocked(
                        "entitlement_reservation_rollback_failed"
                    ) from rollback_exc
            raise
        return clean_prompt

    def _consume_context(
        self,
        chain: ReceiptWriter,
        *,
        role: str,
        attempt_evidence: Mapping[str, Any],
        attempt_created_sequence: int,
    ) -> list[dict[str, str]]:
        receipts = chain.covered_receipts()
        finalized = {
            str(receipt.get("payload", {}).get("command_id"))
            for receipt in receipts
            if receipt.get("transition") in {"context_injected", "command_unapplied"}
            and isinstance(receipt.get("payload"), Mapping)
        }
        accepted = [
            receipt
            for receipt in receipts
            if receipt.get("transition") == "command_accepted"
            and isinstance(receipt.get("payload"), Mapping)
            and str(receipt["payload"].get("command_id")) not in finalized
            and receipt["payload"].get("target_role") in {"lead", role}
            and int(receipt.get("sequence", 0)) < attempt_created_sequence
        ]
        selected: list[dict[str, str]] = []
        for receipt in accepted:
            payload = receipt["payload"]
            assert isinstance(payload, Mapping)
            artifact_relative = str(payload["artifact"])
            artifact = chain.root / artifact_relative
            if chain.hash_file(artifact) != payload["artifact_hash"]:
                raise OrchestrationBlocked("context_artifact_hash_mismatch")
            content = chain.read_artifact(artifact)
            context_id = str(payload["context_id"])
            command_id = str(payload["command_id"])
            if payload["target_role"] == "lead":
                self._append_replan(
                    chain,
                    command_id=command_id,
                    accepted_sequence=int(receipt["sequence"]),
                    attempt_evidence=attempt_evidence,
                    attempt_created_sequence=attempt_created_sequence,
                )
            chain.append(
                "context_injected",
                {
                    "command_id": command_id,
                    "command_type": payload["command_type"],
                    "accepted_sequence": receipt["sequence"],
                    "context_id": context_id,
                    "target_role": payload["target_role"],
                    "artifact": artifact_relative,
                    "artifact_hash": payload["artifact_hash"],
                    "media_type": payload["media_type"],
                    "route": payload["route"],
                    "source_name": payload.get("source_name"),
                    "content_bytes": payload["content_bytes"],
                    "redactions": payload.get("redactions", ()),
                    "extraction": payload.get("extraction"),
                    "direct_route_confirmed": payload["direct_route_confirmed"],
                    "effective_attempt": {
                        "role": role,
                        "attempt_id": attempt_evidence["attempt_id"],
                        "attempt_ordinal": attempt_evidence["attempt_ordinal"],
                        "attempt_created_sequence": attempt_created_sequence,
                    },
                    "provider_dispatch": False,
                },
                writer_role="orchestrator",
                evidence_basis="derived",
            )
            selected.append({
                "command_id": command_id,
                "context_id": context_id,
                "content": content,
            })
        return selected

    def _append_replan(
        self,
        chain: ReceiptWriter,
        *,
        command_id: str,
        accepted_sequence: int,
        attempt_evidence: Mapping[str, Any],
        attempt_created_sequence: int,
    ) -> None:
        receipts = chain.covered_receipts()
        planned = next(
            receipt
            for receipt in receipts
            if receipt.get("transition") == "run_planned"
        )
        planned_payload = planned["payload"]
        assert isinstance(planned_payload, Mapping)
        prior_replans = [
            receipt for receipt in receipts if receipt.get("transition") == "run_replanned"
        ]
        old_hash = (
            str(prior_replans[-1]["payload"]["new_plan_hash"])
            if prior_replans
            else str(planned_payload["plan_hash"])
        )
        previous_sequence = (
            int(prior_replans[-1]["sequence"]) if prior_replans else None
        )
        affected = ({
            "role": attempt_evidence["role"],
            "attempt_id": attempt_evidence["attempt_id"],
            "attempt_ordinal": attempt_evidence["attempt_ordinal"],
            "attempt_created_sequence": attempt_created_sequence,
        },)
        revision = len(prior_replans) + 1
        body = revision_body(
            plan_revision=revision,
            previous_plan_hash=old_hash,
            command_id=command_id,
            accepted_sequence=accepted_sequence,
            affected_future_attempts=affected,
        )
        chain.append(
            "run_replanned",
            {
                "plan_contract": PLAN_CONTRACT,
                "plan_revision": revision,
                "previous_replan_sequence": previous_sequence,
                "command_id": command_id,
                "accepted_sequence": accepted_sequence,
                "reason": "operator_context",
                "old_plan_hash": old_hash,
                "new_plan_hash": plan_hash(body),
                "affected_future_attempts": affected,
                "provider_dispatch": False,
            },
            writer_role="orchestrator",
            evidence_basis="derived",
        )

    def _allocate_attempt(self, role: str, *, repair_cycle: int) -> StageAttempt:
        ordinal = self._attempt_ordinals.get(role, 0) + 1
        self._attempt_ordinals[role] = ordinal
        return StageAttempt(
            attempt_id="attempt-" + secrets.token_hex(12),
            role=role,
            ordinal=ordinal,
            repair_cycle=repair_cycle,
        )

    @staticmethod
    def _attempt_evidence(attempt: StageAttempt) -> dict[str, Any]:
        return {
            "role": attempt.role,
            "attempt_id": attempt.attempt_id,
            "attempt_ordinal": attempt.ordinal,
            "repair_cycle": attempt.repair_cycle,
        }

    @staticmethod
    def _lane_catalog(
        profile: ProfileSpec,
        *,
        mode: ExecutionMode = ExecutionMode.LIVE,
    ) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        for index, role in enumerate(LANE_ORDER):
            binding = profile.bindings[role]
            catalog.append(
                {
                    "role": role,
                    "order": index,
                    "kind": "conditional" if role in CONDITIONAL_LANES else "core",
                    "required": (
                        mode is ExecutionMode.LIVE and role not in CONDITIONAL_LANES
                    ),
                    "provider": binding.provider_id,
                    "model": binding.model_id,
                    "prompt_id": binding.prompt_id,
                    "prompt_version": binding.prompt_version,
                    "contract_id": STAGE_RESPONSE_CONTRACT_ID,
                    "contract_version": STAGE_RESPONSE_CONTRACT_VERSION,
                }
            )
        return catalog

    def _validate_response_contract(
        self,
        role: str,
        body: Mapping[str, Any],
    ) -> None:
        if not stage_response_matches(role, body):
            raise OrchestrationBlocked(f"off_contract_stage:{role}:response_contract")

    @staticmethod
    def _usage_record(reported: Mapping[str, Any]) -> dict[str, int]:
        """Preserve the input/output split that pricing depends on.

        Output tokens bill several times higher than input on every provider in
        the profile, and reasoning tokens bill at the output rate. A single
        total cannot be priced, so the sealed receipt keeps the three counts
        separately. ``tokens`` is retained as a derived convenience total and
        must never be used as a pricing input.
        """
        input_tokens = int(reported.get("prompt_tokens", 0))
        output_tokens = int(reported.get("completion_tokens", 0))
        reasoning_tokens = int(reported.get("reasoning_tokens", 0))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
            "tokens": input_tokens + output_tokens + reasoning_tokens,
        }

    def _preflight_cost(
        self, role: str, usage_rows: list[dict[str, Any]]
    ) -> float:
        if role not in self.cost_ceiling_usd_by_role:
            raise OrchestrationBlocked(f"cost_ceiling_required:{role}")
        ceiling = float(self.cost_ceiling_usd_by_role[role])
        consumed = sum(
            (Decimal(str(row["cost_usd"])) for row in usage_rows),
            start=Decimal(0),
        )
        if consumed + Decimal(str(ceiling)) > Decimal(str(self.budget_usd)):
            raise OrchestrationBlocked(f"budget_preflight_blocked:{role}")
        return ceiling

    def _preflight_stage(
        self,
        role: str,
        binding: BindingSpec,
        usage_rows: list[dict[str, Any]],
        window: PlanWindow | None,
    ) -> StageBudget:
        # No ledger means the legacy metered-budget contract. This keeps the
        # public orchestrator compatible while production callers opt into the
        # explicit settlement map and fail closed for unknown providers.
        if self.entitlement_ledger is None or window is None:
            return StageBudget("metered", self._preflight_cost(role, usage_rows))
        if window.settlement == "unknown":
            raise OrchestrationBlocked(f"entitlement_unknown:{role}")
        if window.settlement == "metered":
            return StageBudget(
                "metered",
                self._preflight_cost(role, usage_rows),
                window,
            )
        projected = self.projected_calls_by_role.get(role, 1)
        preflight = getattr(self.entitlement_ledger, "preflight", None)
        if callable(preflight):
            try:
                preflight(binding.provider_id)
            except ValueError as exc:
                raise OrchestrationBlocked(str(exc)) from exc
        if window.used + window.reserved + projected > window.limit:
            raise OrchestrationBlocked(f"plan_window_exceeded:{role}")
        return StageBudget("plan_covered", 0.0, window, projected)

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
        chain: ReceiptWriter,
        status: str,
        planned: tuple[str, ...],
        dispatched: list[str],
        proposal: Mapping[str, Any] | None,
        usage_rows: list[dict[str, Any]],
        *,
        repair_cycles: int = 0,
    ) -> OrchestrationResult:
        if status in {"awaiting_approval", "human_escalation"}:
            action_id = "action-" + secrets.token_hex(12)
            action = chain.append(
                "action_opened",
                {
                    "action_id": action_id,
                    "type": (
                        "approval_required"
                        if status == "awaiting_approval"
                        else "human_escalation"
                    ),
                    "scope": "run",
                    "target": "operator",
                    "caused_by_sequence": chain.sequence,
                    "summary": (
                        "Approve or reject the governed build proposal."
                        if status == "awaiting_approval"
                        else "Review the audit escalation before closure."
                    ),
                    "allowed_resolutions": ["approved", "rejected"],
                    "outcome_map": {
                        "approved": "completed",
                        "rejected": "blocked",
                    },
                    "provider_dispatch": False,
                },
            )
            chain.append(
                "run_decision",
                {
                    "decision": "awaiting_approval",
                    "outcome": status,
                    "action_id": action_id,
                    "action_opened_sequence": action["sequence"],
                    "provider_dispatch": bool(dispatched),
                    "repair_cycles": repair_cycles,
                },
            )
        else:
            chain.terminalize(
                "run_decision",
                {
                    "decision": "blocked",
                    "outcome": status,
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
    "StageBudget",
]
