from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from torq_cli.application.live_runtime import LiveRuntime, build_live_runtime
from torq_cli.application.run_command import RunController, RunIdentity
from torq_cli.core.engine import NormalizedResponse, Provenance
from torq_cli.interfaces import cli
from torq_cli.safety.receipts import verify_receipt_store


_BINDINGS = {
    "g1d": ("anthropic", "claude-fable-5", "agent_sdk"),
    "g1r": ("anthropic", "claude-opus-4-8", "agent_sdk"),
    "builder": ("deepseek", "deepseek-v4-pro", "direct_api"),
    "g2a": ("openai", "gpt-5.5", "direct_api"),
    "refine_bug": ("moonshot", "k3", "direct_api"),
    "refine_ui": ("zai", "glm-5.2", "direct_api"),
}


def _write_source(
    path: Path,
    *,
    deepseek_direct_only: bool = False,
    base_url: str | None = None,
) -> Path:
    values = {
        "OPENAI_API_KEY": "openai-test",
        "KIMI_API_KEY": "kimi-test",
        "GLM_API_KEY": "glm-test",
    }
    values[
        "DEEPSEEK_API_KEY" if deepseek_direct_only else "QWEN_TOKEN_PLAN_API_KEY"
    ] = "deepseek-test"
    if base_url is not None:
        values["QWEN_TOKEN_PLAN_BASE_URL"] = base_url
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path.resolve()


def _write_config(path: Path, credential_file: Path) -> Path:
    overrides: dict[str, dict[str, object]] = {}
    connectors: dict[str, dict[str, object]] = {}
    for role, (provider, _model, surface) in _BINDINGS.items():
        connector = role.replace("_", "-") + "-main"
        overrides[role] = {"connector_id": connector, "enabled": True}
        connectors[connector] = {
            "provider_id": provider,
            "surface": surface,
            "enabled": True,
        }
    document = {
        "config_version": 1,
        "profile": {"id": "torq-v5-6-live", "version": "1.0.0"},
        "binding_overrides": overrides,
        "connectors": connectors,
        "credential_source": {"kind": "external_env", "path": str(credential_file)},
        "entitlement_accounts": {
            "subscriptions": {
                "providers": {
                    "anthropic": True,
                    "deepseek": True,
                    "moonshot": True,
                    "zai": True,
                },
                "settlement": "plan_covered",
                "used": 0,
                "limit": 100,
                "resets_at": "2099-01-01T00:00:00Z",
                "used_source": "receipt_derived",
                "limit_source": "operator_declared",
            },
            "openai-api": {
                "providers": {"openai": True},
                "settlement": "metered",
                "used": 0,
                "limit": 100,
                "resets_at": "2099-01-01T00:00:00Z",
                "used_source": "receipt_derived",
                "limit_source": "operator_declared",
            },
        },
        "policy": {
            "independence_mode": "profile_minimum",
            "unattestable_action": "deny",
            "loop_budget": 1,
            "resource_limits": {
                "max_runtime_seconds": 300,
                "max_cost_cents": 100,
                "max_file_count": 50,
                "max_changed_lines": 1000,
            },
        },
    }
    path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    return path


def _factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LiveRuntime:
    source = _write_source(tmp_path / "provider.env")
    config = _write_config(tmp_path / "config.yaml", source)
    monkeypatch.setattr("torq_cli.application.live_runtime.provider_binary_available", lambda *_a: True)
    return build_live_runtime(config, tmp_path / "runs", base_environment={"PATH": "safe"})


def test_factory_wires_exact_six_lane_profile_and_governed_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _factory(tmp_path, monkeypatch)

    assert {
        role: (binding.provider_id, binding.model_id, binding.connector_surface)
        for role, binding in runtime.profile.bindings.items()
    } == _BINDINGS
    assert runtime.config_version == 1
    assert runtime.orchestrator.loop_budget == 1
    assert runtime.orchestrator.budget_usd == 1.0
    assert runtime.orchestrator.cost_ceiling_usd_by_role == {"g2a": 1.0}
    assert runtime.orchestrator.entitlement_ledger is not None
    assert runtime.orchestrator.entitlement_ledger.window("deepseek").account == "subscriptions"


def test_factory_rejects_direct_deepseek_key_without_metered_fallback_before_run_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path / "provider.env", deepseek_direct_only=True)
    config = _write_config(tmp_path / "config.yaml", source)
    run_root = tmp_path / "runs"
    monkeypatch.setattr("torq_cli.application.live_runtime.provider_binary_available", lambda *_a: True)

    with pytest.raises(ValueError, match="provider_credential_missing:deepseek"):
        build_live_runtime(config, run_root, base_environment={"PATH": "safe"})
    assert not run_root.exists()


def test_factory_rejects_invalid_token_plan_region_before_run_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path / "provider.env", base_url="http://wrong-region.example")
    config = _write_config(tmp_path / "config.yaml", source)
    run_root = tmp_path / "runs"
    monkeypatch.setattr("torq_cli.application.live_runtime.provider_binary_available", lambda *_a: True)

    with pytest.raises(ValueError, match="provider_base_url_invalid"):
        build_live_runtime(config, run_root, base_environment={"PATH": "safe"})
    assert not run_root.exists()


@pytest.mark.parametrize(
    ("mutation", "finding"),
    [
        ("missing_config", "live_config_unreadable"),
        ("missing_binary", "live_provider_binary_missing:claude"),
    ],
)
def test_factory_hard_failures_leave_no_evidence_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    finding: str,
) -> None:
    source = _write_source(tmp_path / "provider.env")
    config = _write_config(tmp_path / "config.yaml", source)
    if mutation == "missing_config":
        config = tmp_path / "absent.yaml"
    else:
        monkeypatch.setattr("torq_cli.application.live_runtime.provider_binary_available", lambda *_a: False)
    run_root = tmp_path / "runs"

    with pytest.raises(ValueError, match=finding):
        build_live_runtime(config, run_root, base_environment={"PATH": "safe"})
    assert not run_root.exists()


def test_factory_config_version_mismatch_leaves_no_evidence_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path / "provider.env")
    config = _write_config(tmp_path / "config.yaml", source)
    run_root = tmp_path / "runs"
    monkeypatch.setattr("torq_cli.application.live_runtime.provider_binary_available", lambda *_a: True)

    with pytest.raises(ValueError, match="config_version_mismatch"):
        build_live_runtime(
            config,
            run_root,
            base_environment={"PATH": "safe"},
            expected_config_version=2,
        )
    assert not run_root.exists()


def test_factory_invalid_entitlement_window_leaves_no_evidence_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write_source(tmp_path / "provider.env")
    config = _write_config(tmp_path / "config.yaml", source)
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    document["entitlement_accounts"]["subscriptions"]["used"] = 101
    config.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    run_root = tmp_path / "runs"
    monkeypatch.setattr("torq_cli.application.live_runtime.provider_binary_available", lambda *_a: True)

    with pytest.raises(ValueError, match="live_config_invalid:config_schema_invalid"):
        build_live_runtime(config, run_root, base_environment={"PATH": "safe"})
    assert not run_root.exists()


class _SuccessfulDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def dispatch(self, *, role: str, provider: str, model: str, prompt: str) -> NormalizedResponse:
        self.calls.append((role, provider, model))
        payload: dict[str, Any]
        if role == "g1d":
            payload = {"status": "design_complete", "proposal": "safe design"}
        elif role == "g1r":
            payload = {"verdict": "approve", "rationale": "sound"}
        elif role == "builder":
            payload = {"status": "build_complete", "proposal": "safe proposal"}
        else:
            payload = {"verdict": "approve", "defects": []}
        return NormalizedResponse(
            json.dumps(payload),
            "",
            {"prompt_tokens": 1, "completion_tokens": 1, "reasoning_tokens": 0},
            Provenance(provider, model, False),
        )


def test_factory_runtime_preserves_profile_binding_and_verified_receipt_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _factory(tmp_path, monkeypatch)
    dispatcher = _SuccessfulDispatcher()
    runtime.orchestrator.dispatcher = dispatcher
    identity = RunIdentity("1.0.0", "3.1.3", "prompt-v1", "profile-bound-live", "sandbox", 1, "new")

    report = RunController(tmp_path / "runs", runtime.orchestrator).start(
        identity,
        {"profile": "torq-v5-6-live"},
        expected={"profile": "torq-v5-6-live"},
        live=True,
        live_opt_in=True,
        policy_opt_in=True,
        goal="produce a proposal only",
        profile=runtime.profile,
    )

    assert dispatcher.calls == [
        ("g1d", "anthropic", "claude-fable-5"),
        ("g1r", "anthropic", "claude-opus-4-8"),
        ("builder", "deepseek", "deepseek-v4-pro"),
        ("g2a", "openai", "gpt-5.5"),
    ]
    assert report["verdict"] == "awaiting_approval"
    verification = verify_receipt_store(Path(report["receipts"]))
    assert verification.status == "verified"


def test_installed_cli_requires_config_and_uses_factory_for_live_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity_path = tmp_path / "identity.json"
    expected_path = tmp_path / "expected.json"
    actual_path = tmp_path / "actual.json"
    identity_path.write_text(json.dumps({
        "profile_version": "1.0.0",
        "policy_version": "3.1.3",
        "prompt_binding": "prompt-v1",
        "model_resolution": "profile-bound-live",
        "sandbox_identity": "sandbox",
        "config_version": 1,
        "receipt_chain_hash": "new",
    }), encoding="utf-8")
    expected_path.write_text('{"profile":"torq-v5-6-live"}', encoding="utf-8")
    actual_path.write_text('{"profile":"torq-v5-6-live"}', encoding="utf-8")
    common = [
        "run", "--goal", "test", "--run-root", str(tmp_path / "runs"),
        "--identity", str(identity_path), "--expected", str(expected_path),
        "--actual", str(actual_path), "--live", "--allow-live", "--policy-allow-live",
    ]
    assert cli.main(common) == 3
    assert json.loads(capsys.readouterr().out)["finding"] == "live_config_required"
    assert not (tmp_path / "runs").exists()

    runtime = _factory(tmp_path, monkeypatch)
    dispatcher = _SuccessfulDispatcher()
    runtime.orchestrator.dispatcher = dispatcher
    monkeypatch.setattr(cli, "build_live_runtime", lambda *_a, **_k: runtime)
    assert cli.main([*common, "--config", str(tmp_path / "config.yaml")]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "awaiting_approval"
