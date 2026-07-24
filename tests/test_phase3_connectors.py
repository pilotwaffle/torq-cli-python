from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.connectors import (
    AuthState,
    Connector,
    ConnectorError,
    FailureClass,
    LaneHealth,
    MemoryVault,
    MockSurface,
    all_connector_specs,
)
from torq_cli.connectors.status import auth_status, inspect_harness
from torq_cli.connectors.smoke import LiveSmokeRunner


def _payload(model: str, *, usage: object = "unreported") -> dict:
    payload: dict = {"model": model, "provider": "mock", "choices": [{"message": {"content": "ok"}}]}
    if usage != "unreported":
        payload["usage"] = usage
    return payload


def test_all_six_connectors_conform_with_optional_unreported_usage(tmp_path: Path) -> None:
    specs = all_connector_specs()
    assert set(specs) == {"claude", "codex", "grok", "kimi", "zai", "deepseek"}
    assert specs["claude"].required_models == (
        "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-fable-5b"
    )
    agents = {"kimi": "refine_bug", "zai": "refine_ui", "deepseek": "builder"}
    for name, spec in specs.items():
        model = spec.required_models[0]
        connector = Connector(spec, (MockSurface(spec.primary_surface, _payload(model)),), MemoryVault({name: "opaque"}), work_root=tmp_path)
        result = connector.call(model=model, prompt="fixture", agent=agents.get(name, "agent-1"))
        assert result.provenance.model == model
        assert result.visible_text == "ok"


def test_auth_states_grants_and_session_isolation_fail_closed(tmp_path: Path) -> None:
    spec = all_connector_specs()["claude"]
    for state in (AuthState.LOGGED_OUT, AuthState.AMBIGUOUS):
        connector = Connector(spec, (MockSurface("agent_sdk", _payload("fable-5"), auth=state),), MemoryVault(), work_root=tmp_path)
        assert connector.health().state is LaneHealth.UNAVAILABLE
    opus_only = Connector(spec, (MockSurface("agent_sdk", _payload("opus-4.8"), grants={"opus-4.8"}),), MemoryVault(), work_root=tmp_path)
    with pytest.raises(ConnectorError, match="model_not_granted:fable-5"):
        opus_only.call(model="fable-5", prompt="x", agent="g1d")
    ready = Connector(spec, (MockSurface("agent_sdk", _payload("fable-5"), grants={"fable-5", "opus-4.8"}),), MemoryVault(), work_root=tmp_path)
    one = ready.open_session("g1d")
    two = ready.open_session("g1r")
    assert one.session_id != two.session_id and one.workdir != two.workdir


def test_codex_primary_fallback_resume_cancel_and_missing_auth(tmp_path: Path) -> None:
    spec = all_connector_specs()["codex"]
    primary = MockSurface("sdk", _payload("gpt-5.5-thinking"), auth=AuthState.LOGGED_OUT)
    fallback = MockSurface("cli_json", _payload("gpt-5.5-thinking"), grants=set(spec.required_models))
    connector = Connector(spec, (primary, fallback), MemoryVault(), work_root=tmp_path)
    session = connector.open_session("g2a")
    connector.call(model="gpt-5.5-thinking", prompt="x", agent="g2a", session_id=session.session_id)
    assert connector.resume(session.session_id).session_id == session.session_id
    connector.cancel(session.session_id)
    assert connector.resume(session.session_id).cancelled is True
    unavailable = Connector(spec, (MockSurface("sdk", {}, auth=AuthState.LOGGED_OUT), MockSurface("cli_json", {}, auth=AuthState.LOGGED_OUT)), MemoryVault(), work_root=tmp_path)
    assert unavailable.health().state is LaneHealth.UNAVAILABLE


def test_direct_connectors_use_vault_classify_429_and_enforce_eligibility(tmp_path: Path) -> None:
    kimi_spec = all_connector_specs()["kimi"]
    rate = MockSurface("direct_api", {}, grants={"kimi-k3"}, failure=ConnectorError("rate", FailureClass.RATE_LIMIT, retryable=True))
    kimi = Connector(kimi_spec, (rate,), MemoryVault({"kimi": "opaque"}), work_root=tmp_path, degradation_threshold=2)
    for _ in range(2):
        with pytest.raises(ConnectorError):
            kimi.call(model="kimi-k3", prompt="x", agent="refine_bug")
    assert kimi.health().state is LaneHealth.DEGRADED
    zai_spec = all_connector_specs()["zai"]
    zai = Connector(zai_spec, (MockSurface("direct_api", _payload("glm-5.2"), grants={"glm-5.2"}),), MemoryVault({"zai": "opaque"}), work_root=tmp_path)
    with pytest.raises(ConnectorError, match="eligibility:ru_only"):
        zai.call(model="glm-5.2", prompt="x", agent="builder")


def test_malformed_and_midstream_failures_have_distinct_taxonomy(tmp_path: Path) -> None:
    spec = all_connector_specs()["deepseek"]
    malformed = Connector(spec, (MockSurface("direct_api", {"model": "deepseek-v4-pro"}),), MemoryVault({"deepseek": "x"}), work_root=tmp_path)
    with pytest.raises(ConnectorError) as exc:
        malformed.call(model="deepseek-v4-pro", prompt="x", agent="builder")
    assert exc.value.failure_class is FailureClass.MALFORMED_RESPONSE
    dropped = Connector(spec, (MockSurface("direct_api", {}, grants={"deepseek-v4-pro"}, failure=ConnectorError("drop", FailureClass.MIDSTREAM_DROP, retryable=True)),), MemoryVault({"deepseek": "x"}), work_root=tmp_path)
    with pytest.raises(ConnectorError) as exc2:
        dropped.call(model="deepseek-v4-pro", prompt="x", agent="builder")
    assert exc2.value.failure_class is FailureClass.MIDSTREAM_DROP


def test_status_and_attestation_report_mixed_states_without_secrets(tmp_path: Path) -> None:
    specs = all_connector_specs()
    connectors = {
        "claude": Connector(specs["claude"], (MockSurface("agent_sdk", _payload("fable-5"), grants=set(specs["claude"].required_models)),), MemoryVault(), work_root=tmp_path),
        "codex": Connector(specs["codex"], (), MemoryVault(), work_root=tmp_path),
        "grok": Connector(specs["grok"], (MockSurface("acp", {}, auth=AuthState.LOGGED_OUT),), MemoryVault(), work_root=tmp_path),
        "kimi": Connector(specs["kimi"], (), MemoryVault({"kimi": "secret-never-print"}), work_root=tmp_path, blocked_reason="operator_policy_hold"),
    }
    report = auth_status(connectors, {"g1d": ("claude", "fable-5"), "g2a": ("codex", "gpt-5.5-thinking")})
    rendered = json.dumps(report)
    assert "secret-never-print" not in rendered
    assert report["profiles"]["selected"]["state"] == "blocked"
    inspection = inspect_harness({"g1d": ("claude", "fable-5")}, {"g1d": {"provider": "claude", "model": None}})
    assert inspection["agents"]["g1d"]["status"] == "unattestable"


def test_live_smoke_runner_is_manual_and_writes_dated_independent_report(tmp_path: Path) -> None:
    runner = LiveSmokeRunner(tmp_path)
    assert runner.ci_allowed is False
    report = runner.run_independently({"claude": lambda: {"model": "fable-5", "usage": "unreported"}}, date="2026-07-23")
    assert report.name == "live-smoke-2026-07-23.json"
    assert json.loads(report.read_text(encoding="utf-8"))["providers"]["claude"]["model"] == "fable-5"
