from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from torq_cli.application.orchestrator import OrchestrationBlocked
from torq_cli.connectors.credential_sources import ExplicitEnvVault
from torq_cli.connectors.live_dispatch import LiveStageDispatcher


def _vault(tmp_path: Path) -> ExplicitEnvVault:
    source = tmp_path / ".env"
    source.write_text("DEEPSEEK_API_KEY=test-only\nOPENAI_API_KEY=test-openai\n", encoding="utf-8")
    return ExplicitEnvVault(source)


def test_live_dispatcher_scopes_claude_compatible_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: object, **kwargs: object) -> SimpleNamespace:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "result": '```json\n{"status":"build_complete"}\n```',
                "modelUsage": {"deepseek-v4-pro": {}},
                "usage": {"input_tokens": 2, "output_tokens": 3},
            }),
        )

    monkeypatch.setattr("torq_cli.adapters.live_provider.subprocess.run", fake_run)
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path), {"PATH": "safe", "OPENAI_API_KEY": "must-not-pass"}
    )
    response = dispatcher.dispatch(
        role="builder",
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt="proposal",
    )

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "test-only"
    assert "OPENAI_API_KEY" not in environment
    assert response.provenance.provider == "deepseek"
    assert response.provenance.model == "deepseek-v4-pro"
    assert response.usage["prompt_tokens"] == 2


def test_live_dispatcher_rejects_non_object_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "torq_cli.adapters.live_provider.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "result": "not json",
                "modelUsage": {"deepseek-v4-pro": {}},
            }),
        ),
    )
    dispatcher = LiveStageDispatcher(_vault(tmp_path), {"PATH": "safe"})
    with pytest.raises(OrchestrationBlocked, match="live_provider_response_invalid"):
        dispatcher.dispatch(
            role="builder",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt="proposal",
        )


def test_live_dispatcher_fails_closed_on_unattested_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "torq_cli.adapters.live_provider.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": "{}", "modelUsage": {"other": {}}}),
        ),
    )
    dispatcher = LiveStageDispatcher(_vault(tmp_path), {"PATH": "safe"})
    with pytest.raises(OrchestrationBlocked, match="live_model_unattested:deepseek"):
        dispatcher.dispatch(
            role="builder",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt="proposal",
        )


def test_live_dispatcher_rejects_unknown_provider(tmp_path: Path) -> None:
    dispatcher = LiveStageDispatcher(_vault(tmp_path), {"PATH": "safe"})
    with pytest.raises(OrchestrationBlocked, match="live_provider_unsupported"):
        dispatcher.dispatch(role="g1d", provider="unknown", model="x", prompt="proposal")
