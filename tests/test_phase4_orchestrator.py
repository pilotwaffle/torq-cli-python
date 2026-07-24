from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from torq_cli.application.orchestrator import (
    ConnectorDispatcher,
    GovernedOrchestrator,
    OrchestrationBlocked,
)
from torq_cli.application.run_command import RunController, RunIdentity
from torq_cli.connectors import Connector, MemoryVault, MockSurface, all_connector_specs
from torq_cli.core.engine import NormalizedResponse, Provenance
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.safety.receipts import MemoryRunKeyStore, ReceiptChain, verify_receipt_store


def _payload(provider: str, model: str, body: Mapping[str, object]) -> dict[str, object]:
    return {
        "provider": provider,
        "model": model,
        "choices": [{"message": {"content": json.dumps(body, sort_keys=True)}}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 3},
    }


def _connector(
    name: str,
    surfaces: tuple[MockSurface, ...],
    root: Path,
) -> Connector:
    return Connector(
        all_connector_specs()[name],
        surfaces,
        MemoryVault({name: "opaque-test-handle"}),
        work_root=root,
    )


def test_dry_run_plans_governed_graph_without_provider_dispatch(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain = ReceiptChain(
        tmp_path / "evidence",
        "dry-run",
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )

    result = GovernedOrchestrator().execute(
        goal="Implement the requested change",
        profile=profile,
        mode=ExecutionMode.DRY_RUN,
        chain=chain,
    )
    chain.seal()

    assert result.status == "dry_run_complete"
    assert result.planned_roles == ("g1d", "g1r", "builder", "g2a")
    assert result.dispatched_roles == ()
    assert result.proposal is None
    assert verify_receipt_store(chain.root).status == "verified"
    transitions = [
        json.loads(line)["transition"]
        for line in (chain.root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert transitions == ["run_planned", "run_decision"]


def test_live_orchestrator_dispatches_profile_bound_connectors_and_awaits_approval(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    specs = all_connector_specs()
    connectors = {
        "claude": _connector(
            "claude",
            (
                MockSurface(
                    specs["claude"].primary_surface,
                    _payload("anthropic", "claude-fable-5", {"status": "design_complete"}),
                    grants={"claude-fable-5"},
                ),
                MockSurface(
                    specs["claude"].primary_surface,
                    _payload("anthropic", "claude-opus-4-8", {"verdict": "approve"}),
                    grants={"claude-opus-4-8"},
                ),
            ),
            tmp_path / "sessions",
        ),
        "deepseek": _connector(
            "deepseek",
            (
                MockSurface(
                    specs["deepseek"].primary_surface,
                    _payload("deepseek", "deepseek-v4-pro", {"status": "build_complete"}),
                    grants={"deepseek-v4-pro"},
                ),
            ),
            tmp_path / "sessions",
        ),
        "codex": _connector(
            "codex",
            (
                MockSurface(
                    specs["codex"].primary_surface,
                    _payload("openai", "gpt-5.5-thinking", {"verdict": "approve", "defects": []}),
                    grants={"gpt-5.5-thinking"},
                ),
            ),
            tmp_path / "sessions",
        ),
    }
    chain = ReceiptChain(
        tmp_path / "evidence",
        "live-run",
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )

    result = GovernedOrchestrator(ConnectorDispatcher(connectors)).execute(
        goal="Implement the requested change",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )
    chain.seal()

    assert result.status == "awaiting_approval"
    assert result.dispatched_roles == ("g1d", "g1r", "builder", "g2a")
    assert result.proposal is not None
    assert result.proposal["source_role"] == "builder"
    assert result.usage["agents"]["builder"]["usage"]["tokens"] == 5
    assert verify_receipt_store(chain.root).status == "verified"

    controller = RunController(
        tmp_path / "controller-evidence",
        GovernedOrchestrator(ConnectorDispatcher(connectors)),
    )
    identity = RunIdentity(
        profile.profile_version,
        "3.1.3",
        "registry-v1",
        "profile-bound",
        "sandbox-test",
        1,
        "prior-chain",
    )
    report = controller.start(
        identity,
        {"profile": profile.profile_id},
        expected={"profile": profile.profile_id},
        live=True,
        live_opt_in=True,
        policy_opt_in=True,
        goal="Implement the requested change",
        profile=profile,
    )
    assert report["verdict"] == "awaiting_approval"
    assert report["dispatched_roles"] == ("g1d", "g1r", "builder", "g2a")
    assert verify_receipt_store(Path(report["receipts"])).status == "verified"


class _ScriptedDispatcher:
    def __init__(self, script: Mapping[str, list[NormalizedResponse]]) -> None:
        self.script = {role: list(responses) for role, responses in script.items()}
        self.calls: list[str] = []

    def dispatch(self, *, role: str, provider: str, model: str, prompt: str) -> NormalizedResponse:
        del provider, model, prompt
        self.calls.append(role)
        return self.script[role].pop(0)


def _response(provider: str, model: str, body: Mapping[str, object]) -> NormalizedResponse:
    return NormalizedResponse(
        json.dumps(body, sort_keys=True),
        "",
        {"prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": 0},
        Provenance(provider, model, False),
    )


def test_high_bug_routes_to_refine_bug_and_targeted_reaudit(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _ScriptedDispatcher(
        {
            "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
            "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "approve"})],
            "builder": [_response("deepseek", "deepseek-v4-pro", {"status": "build_complete"})],
            "g2a": [
                _response(
                    "openai",
                    "gpt-5.5-thinking",
                    {"verdict": "reject", "defects": [{"severity": "HIGH", "class": "bug"}]},
                ),
                _response("openai", "gpt-5.5-thinking", {"verdict": "approve", "defects": []}),
            ],
            "refine_bug": [
                _response("moonshot", "kimi-k3", {"status": "repair_complete"})
            ],
        }
    )
    chain = ReceiptChain(
        tmp_path / "evidence",
        "repair-run",
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )

    result = GovernedOrchestrator(dispatcher, loop_budget=1).execute(
        goal="Fix the defect",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert result.status == "awaiting_approval"
    assert dispatcher.calls == ["g1d", "g1r", "builder", "g2a", "refine_bug", "g2a"]
    assert result.repair_cycles == 1
    assert [event["stage"] for event in result.timeline] == dispatcher.calls
    receipts = [
        json.loads(line)
        for line in (chain.root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(receipt["transition"] == "repair_routed" for receipt in receipts)
    assert sum(
        receipt["transition"] == "stage_completed"
        and receipt["payload"]["role"] == "g2a"
        for receipt in receipts
    ) == 2


def test_loop_exhaustion_and_off_contract_output_halt_fail_closed(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    common = {
        "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "approve"})],
        "builder": [_response("deepseek", "deepseek-v4-pro", {"status": "build_complete"})],
        "g2a": [
            _response(
                "openai",
                "gpt-5.5-thinking",
                {"verdict": "reject", "defects": [{"severity": "HIGH", "class": "bug"}]},
            )
        ],
    }
    exhausted_dispatcher = _ScriptedDispatcher(
        {
            "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
            **common,
        }
    )
    exhausted_chain = ReceiptChain(
        tmp_path / "exhausted",
        "run",
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )
    exhausted = GovernedOrchestrator(exhausted_dispatcher, loop_budget=0).execute(
        goal="Bound the repair loop",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=exhausted_chain,
    )
    assert exhausted.status == "repair_budget_exhausted"
    assert exhausted_dispatcher.calls == ["g1d", "g1r", "builder", "g2a"]

    off_contract = _ScriptedDispatcher(
        {
            "g1d": [_response("anthropic", "claude-fable-5", {"status": "looks_good"})]
        }
    )
    off_contract_chain = ReceiptChain(
        tmp_path / "off-contract",
        "run",
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )
    with pytest.raises(OrchestrationBlocked, match="off_contract_stage:g1d:status"):
        GovernedOrchestrator(off_contract).execute(
            goal="Reject ambiguous stage output",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=off_contract_chain,
        )


def test_live_orchestration_fails_closed_without_dispatcher_or_model_attestation(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain = ReceiptChain(
        tmp_path / "evidence",
        "blocked-run",
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )
    with pytest.raises(OrchestrationBlocked, match="live_dispatcher_required"):
        GovernedOrchestrator().execute(
            goal="Do not fake success",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=chain,
        )

    mismatch = _ScriptedDispatcher(
        {
            "g1d": [
                _response("anthropic", "different-model", {"status": "design_complete"})
            ]
        }
    )
    with pytest.raises(OrchestrationBlocked, match="resolved_model_mismatch:g1d"):
        GovernedOrchestrator(mismatch).execute(
            goal="Do not fake attestation",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=chain,
        )
