from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.core.engine import (
    BudgetExceeded,
    BudgetLedger,
    ProviderError,
    ProviderRequest,
    RetryClass,
    RunStore,
    call_with_retry,
    normalize_response,
)
from torq_cli.core.graph import ExecutionEvidenceStore, ExecutionMode, GraphExecutor, compile_graph
from torq_cli.core.policy import Defect, G2APolicy
from torq_cli.core.redaction import PatternRegistry, RedactionBlocked, SafePersistence, SafeTransport


def test_engine_normalizes_mmh_shape_with_complete_provenance() -> None:
    response = normalize_response(
        ProviderRequest(provider="deepseek", model="v4", messages=()),
        {"model": "v4-pro", "provider": "deepseek", "choices": [{"message": {"content": "<think>x</think>ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}},
    )
    assert response.visible_text == "ok"
    assert response.reasoning_trace == "x"
    assert response.provenance.as_dict() == {"provider": "deepseek", "model": "v4-pro", "fallback_used": False}


def test_retry_classification_and_budget_guards() -> None:
    assert ProviderError("rate", status=429).retry_class is RetryClass.RETRYABLE
    assert ProviderError("bad", status=400).retry_class is RetryClass.FATAL
    ledger = BudgetLedger(limit_usd=1.0)
    ledger.preflight(0.75)
    ledger.charge(0.75)
    with pytest.raises(BudgetExceeded, match="budget_preflight_blocked"):
        ledger.preflight(0.30)


def test_retry_uses_bounded_backoff_without_real_sleep() -> None:
    calls: list[int] = []
    waits: list[float] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ProviderError("busy", status=503)
        return "ok"

    assert call_with_retry(operation, attempts=3, base_delay=0.5, sleep=waits.append) == "ok"
    assert waits == [0.5, 1.0]


def test_run_and_stage_metadata_round_trip(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.save("run-1", {"profile": "standard_v1"}, ({"stage": "g1d", "status": "done"},))
    assert store.load("run-1") == {
        "metadata": {"profile": "standard_v1"},
        "stages": [{"stage": "g1d", "status": "done"}],
    }


def test_graph_profiles_gate_stops_recompile_bounds_and_no_push_merge(tmp_path: Path) -> None:
    light = compile_graph("light_v1")
    standard = compile_graph("standard_v1")
    assert [n.role for n in light.nodes] == ["g1d", "builder", "g2a"]
    assert [n.role for n in standard.nodes] == ["g1d", "g1r", "builder", "g2a"]
    assert all(n.role not in {"push", "merge"} for n in standard.nodes)
    stopped = GraphExecutor(max_recompiles=1).execute(standard, gate=lambda role: role != "builder")
    assert stopped.status == "gate_stopped"
    assert stopped.stopped_at == "builder"
    with pytest.raises(ValueError, match="double opt-in"):
        GraphExecutor().execute(light, mode=ExecutionMode.LIVE, live_opt_in=False, policy_opt_in=True)
    bounded = GraphExecutor(max_recompiles=1).execute(light, recompile=lambda _: True)
    assert bounded.status == "recompile_limit"
    assert bounded.recompiles == 1
    evidence = ExecutionEvidenceStore(tmp_path).write("run-1", standard, stopped)
    assert {path.name for path in evidence} == {"manifest.json", "trace.json", "snapshot.json"}


def test_policy_v313_routes_and_enforces_guards() -> None:
    policy = G2APolicy()
    assert policy.version == "3.1.3"
    critical = policy.route(Defect("d1", "CRITICAL", "security", "open"))
    assert (critical.bucket, critical.lane, critical.subsystem_blocked) == ("A", "human_escalation", True)
    high = policy.route(Defect("d2", "HIGH", "ui", "open"))
    assert (high.bucket, high.lane) == ("B", "refine_ui")
    assert policy.queue_paused([Defect("d2", "HIGH", "ui", "open")])
    assert not policy.queue_paused([Defect("d2", "HIGH", "ui", "resolved")])
    with pytest.raises(ValueError, match="no_test_only_fix"):
        policy.validate_fix(changed_paths=("tests/test_x.py",))
    receipt = policy.receipt(run_id="r1", defects=())
    assert receipt["policy_version"] == "3.1.3"


def test_dual_redaction_masks_transport_and_persistence_and_blocks(tmp_path: Path) -> None:
    registry = PatternRegistry.default()
    transport_seen: list[str] = []
    transport = SafeTransport(registry, transport_seen.append)
    persistence = SafePersistence(registry)
    fake = "api_key='abcdefghijklmnop'"
    transport.send(fake)
    assert "abcdefghijklmnop" not in transport_seen[0]
    target = tmp_path / "receipt.json"
    persistence.write_text(target, json.dumps({"payload": fake}))
    assert "abcdefghijklmnop" not in target.read_text(encoding="utf-8")
    registry.add("CUSTOM_BLOCK", r"DO-NOT-SEND", block=True)
    with pytest.raises(RedactionBlocked) as exc:
        transport.send("prefix DO-NOT-SEND suffix")
    assert exc.value.findings == ("CUSTOM_BLOCK",)
    assert "DO-NOT-SEND" not in str(exc.value)
