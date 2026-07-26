from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from torq_cli.application.orchestrator import (
    GovernedOrchestrator,
    OrchestrationBlocked,
)
from torq_cli.application.setup import SetupError, SetupService
from torq_cli.core.engine import NormalizedResponse, Provenance
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.domain.config_schema import validate_config
from torq_cli.safety.entitlements import InMemoryEntitlementLedger, PlanWindow
from torq_cli.safety.pricing import RateTable
from torq_cli.safety.receipts import MemoryRunKeyStore, ReceiptChain
from test_phase5_cli_experience import _answers


class _Dispatcher:
    def __init__(self, responses: Mapping[str, list[NormalizedResponse]]) -> None:
        self.responses = {role: list(values) for role, values in responses.items()}
        self.calls: list[str] = []

    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse:
        del provider, model, prompt
        self.calls.append(role)
        return self.responses[role].pop(0)


def _response(
    provider: str,
    model: str,
    body: Mapping[str, object],
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
    reasoning_tokens: int = 0,
) -> NormalizedResponse:
    return NormalizedResponse(
        json.dumps(body),
        "",
        {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens,
        },
        Provenance(provider, model, False),
    )


def _rates() -> RateTable:
    return RateTable.from_document(
        {
            "rate_table_version": "test-rates.v1",
            "rates": {
                "anthropic": {
                    "claude-fable-5": {
                        "input_usd_per_mtok": "10",
                        "output_usd_per_mtok": "50",
                    },
                    "claude-opus-4-8": {
                        "input_usd_per_mtok": "15",
                        "output_usd_per_mtok": "75",
                    },
                },
                "deepseek": {
                    "deepseek-v4-pro": {
                        "input_usd_per_mtok": "0.55",
                        "output_usd_per_mtok": "2.19",
                    }
                },
                "openai": {
                    "gpt-5.5": {
                        "input_usd_per_mtok": "2",
                        "output_usd_per_mtok": "8",
                    }
                },
            },
        }
    )


def _chain(tmp_path: Path, name: str = "accounting") -> ReceiptChain:
    profile = load_registry().profiles["torq-v5-6-live"]
    return ReceiptChain(
        tmp_path / "evidence",
        name,
        MemoryRunKeyStore(),
        profile_version=profile.profile_version,
        policy_version="3.1.3",
    )


def _ledger(*, anthropic_used: int = 0, anthropic_limit: int = 10) -> InMemoryEntitlementLedger:
    return InMemoryEntitlementLedger(
        {
            "anthropic-max": PlanWindow(
                "anthropic-max",
                ("anthropic",),
                "plan_covered",
                anthropic_used,
                anthropic_limit,
                "2026-07-25T00:00:00Z",
            ),
            "qwen-max": PlanWindow(
                "qwen-max",
                ("qwen", "deepseek"),
                "plan_covered",
                0,
                10,
                "2026-07-25T00:00:00Z",
            ),
            "openai-api": PlanWindow(
                "openai-api",
                ("openai",),
                "metered",
                0,
                1,
                "not_applicable",
            ),
        }
    )


def _entitlement_config() -> dict[str, object]:
    return {
        "anthropic-max": {
            "providers": {"anthropic": True},
            "settlement": "plan_covered",
            "used": 0,
            "limit": 200,
            "resets_at": "2026-07-25T00:00:00Z",
            "used_source": "receipt_derived",
            "limit_source": "operator_declared",
        },
        "qwen-max": {
            "providers": {"qwen": True, "deepseek": True},
            "settlement": "plan_covered",
            "used": 0,
            "limit": 200,
            "resets_at": "2026-07-25T00:00:00Z",
            "used_source": "receipt_derived",
            "limit_source": "operator_declared",
        },
        "moonshot-max": {
            "providers": {"moonshot": True},
            "settlement": "plan_covered",
            "used": 0,
            "limit": 200,
            "resets_at": "2026-07-25T00:00:00Z",
            "used_source": "receipt_derived",
            "limit_source": "operator_declared",
        },
        "zai-max": {
            "providers": {"zai": True},
            "settlement": "plan_covered",
            "used": 0,
            "limit": 200,
            "resets_at": "2026-07-25T00:00:00Z",
            "used_source": "receipt_derived",
            "limit_source": "operator_declared",
        },
        "openai-api": {
            "providers": {"openai": True},
            "settlement": "metered",
            "used": 0,
            "limit": 0,
            "resets_at": "not_applicable",
            "used_source": "receipt_derived",
            "limit_source": "operator_declared",
        },
    }


def test_setup_persists_closed_entitlement_accounts_and_requires_every_bound_provider(
    tmp_path: Path,
) -> None:
    answers = _answers()
    answers["entitlement_accounts"] = _entitlement_config()

    document = SetupService().configure(tmp_path / "torq.yaml", answers)

    assert validate_config(document, load_registry()) == ()
    ledger = InMemoryEntitlementLedger.from_config(document["entitlement_accounts"])
    assert ledger.window("deepseek").account == "qwen-max"
    assert ledger.window("qwen").account == "qwen-max"

    missing = _answers()
    accounts = _entitlement_config()
    del accounts["qwen-max"]
    missing["entitlement_accounts"] = accounts
    with pytest.raises(SetupError, match="entitlement_provider_missing:deepseek"):
        SetupService().configure(tmp_path / "missing.yaml", missing)


def test_shared_qwen_account_applies_one_window_to_deepseek_and_qwen() -> None:
    ledger = _ledger()

    ledger.reserve("deepseek", calls=2)

    assert ledger.window("qwen").used == 2
    assert ledger.window("deepseek").account == "qwen-max"
    ledger.reconcile("deepseek", calls=1)
    assert ledger.window("qwen").used == 1


def test_pricing_uses_split_tokens_and_prices_reasoning_as_output() -> None:
    table = _rates()

    quote = table.quote(
        "openai",
        "gpt-5.5",
        {"input_tokens": 1_000_000, "output_tokens": 100_000, "reasoning_tokens": 50_000},
    )

    assert quote.metered_usd == "3.2"
    assert quote.pricing_status == "priced"
    assert table.quote("moonshot", "k3", {}).pricing_status == "rate_unknown"
    assert table.quote("moonshot", "k3", {}).metered_usd is None


def test_plan_lane_dispatches_without_a_cost_ceiling_and_seals_replayable_price(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _Dispatcher(
        {
            "g1d": [
                _response(
                    "anthropic",
                    "claude-fable-5",
                    {"status": "design_complete"},
                    input_tokens=400,
                    output_tokens=90,
                    reasoning_tokens=10,
                )
            ],
            "g1r": [
                _response(
                    "anthropic",
                    "claude-opus-4-8",
                    {"verdict": "reject"},
                )
            ],
        }
    )
    chain = _chain(tmp_path)
    orchestrator = GovernedOrchestrator(
        dispatcher,
        entitlement_ledger=_ledger(),
        rate_table=_rates(),
    )

    result = orchestrator.execute(
        goal="Price a plan-covered run",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    receipts = [
        json.loads(line)
        for line in (chain.root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    completed = [row["payload"] for row in receipts if row["transition"] == "stage_completed"]
    first = completed[0]
    assert result.status == "design_rejected"
    assert first["settlement"] == "plan_covered"
    assert first["billed_usd"] == "0"
    assert first["metered_usd"] == "0.009"
    assert first["rate_table_hash"] == _rates().sha256
    replay = _rates().quote("anthropic", "claude-fable-5", first["usage"])
    assert replay.metered_usd == first["metered_usd"]
    assert result.usage["settlement"]["plan_covered_roles"] == ["g1d", "g1r"]


def test_window_limit_blocks_before_dispatch_and_seals_window_provenance(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _Dispatcher({})
    chain = _chain(tmp_path, "blocked")
    orchestrator = GovernedOrchestrator(
        dispatcher,
        entitlement_ledger=_ledger(anthropic_used=1, anthropic_limit=1),
        rate_table=_rates(),
    )

    with pytest.raises(OrchestrationBlocked, match="plan_window_exceeded:g1d"):
        orchestrator.execute(
            goal="Do not dispatch",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=chain,
        )

    assert dispatcher.calls == []
    receipts = [
        json.loads(line)
        for line in (chain.root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    blocked = next(row["payload"] for row in receipts if row["transition"] == "stage_blocked")
    assert blocked["provider_dispatch"] is False
    assert blocked["settlement"] == "plan_covered"
    assert blocked["entitlement"]["used_source"] == "receipt_derived"
    assert blocked["entitlement"]["limit_source"] == "operator_declared"


def test_provider_absent_from_account_map_fails_closed(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _Dispatcher({})
    ledger = InMemoryEntitlementLedger(
        {
            "qwen-max": PlanWindow(
                "qwen-max",
                ("qwen", "deepseek"),
                "plan_covered",
                0,
                10,
                "2026-07-25T00:00:00Z",
            )
        }
    )

    with pytest.raises(OrchestrationBlocked, match="entitlement_unknown:g1d"):
        GovernedOrchestrator(
            dispatcher,
            entitlement_ledger=ledger,
            rate_table=_rates(),
        ).execute(
            goal="Fail closed",
            profile=profile,
            mode=ExecutionMode.LIVE,
            chain=_chain(tmp_path, "unknown"),
        )

    assert dispatcher.calls == []


def test_mixed_run_budgets_only_openai_and_reports_both_settlements(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _Dispatcher(
        {
            "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
            "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "approve"})],
            "builder": [_response("deepseek", "deepseek-v4-pro", {"status": "build_complete"})],
            "g2a": [_response("openai", "gpt-5.5", {"verdict": "approve", "defects": []})],
        }
    )
    chain = _chain(tmp_path, "mixed")
    result = GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role={"g2a": 0.25},
        entitlement_ledger=_ledger(),
        rate_table=_rates(),
    ).execute(
        goal="Run all governed stages",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert result.status == "awaiting_approval"
    assert result.usage["settlement"]["plan_covered_roles"] == ["builder", "g1d", "g1r"]
    assert result.usage["settlement"]["metered_roles"] == ["g2a"]
    assert Decimal(result.usage["budget"]["consumed_usd"]) < Decimal("0.25")
    assert Decimal(result.usage["settlement"]["billed_usd"]) > 0


def test_unpriced_metered_call_books_configured_worst_case_ceiling(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _Dispatcher(
        {
            "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
            "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "approve"})],
            "builder": [_response("deepseek", "deepseek-v4-pro", {"status": "build_complete"})],
            "g2a": [_response("openai", "gpt-5.5", {"verdict": "approve", "defects": []})],
        }
    )
    rates_without_openai = RateTable.from_document(
        {
            "rate_table_version": "test-rates-without-openai.v1",
            "rates": {
                "anthropic": {
                    "claude-fable-5": {
                        "input_usd_per_mtok": "10", "output_usd_per_mtok": "50",
                    },
                    "claude-opus-4-8": {
                        "input_usd_per_mtok": "15", "output_usd_per_mtok": "75",
                    },
                },
                "deepseek": {
                    "deepseek-v4-pro": {
                        "input_usd_per_mtok": "0.55", "output_usd_per_mtok": "2.19",
                    }
                },
            },
        }
    )
    chain = _chain(tmp_path, "unpriced-metered")

    result = GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role={"g2a": 0.25},
        entitlement_ledger=_ledger(),
        rate_table=rates_without_openai,
    ).execute(
        goal="Conservatively account for unknown price",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert result.usage["budget"]["consumed_usd"] == "0.25"
    receipts = [
        json.loads(line)["payload"]
        for line in (chain.root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    g2a = next(row for row in receipts if row.get("role") == "g2a" and "cost_basis" in row)
    assert g2a["cost_usd"] == "0.25"
    assert g2a["billed_usd"] is None
    assert g2a["metered_usd"] is None
    assert g2a["cost_basis"] == "configured_worst_case_rate_unknown"
