from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from torq_cli.adapters import live_provider as live_provider_module
from torq_cli.application.orchestrator import OrchestrationBlocked
from torq_cli.adapters.owned_stream import ProcessEvent
from torq_cli.adapters.process import ContainmentState, ExitObservation
from torq_cli.connectors.credential_sources import ExplicitEnvVault
from torq_cli.connectors.live_dispatch import LiveStageDispatcher
from torq_cli.domain import stage_response as stage_response_module
from torq_cli.safety.receipts import restrict_receipt_trust_anchor


def _vault(tmp_path: Path) -> ExplicitEnvVault:
    source = tmp_path / ".env"
    # DeepSeek bills to the Alibaba/Qwen Token Plan. A stale direct-account key
    # is left in place so the dispatcher is proven not to fall back to it.
    source.write_text(
        "QWEN_TOKEN_PLAN_API_KEY=test-only\n"
        "DEEPSEEK_API_KEY=must-not-be-used\n"
        "OPENAI_API_KEY=test-openai\n",
        encoding="utf-8",
    )
    restrict_receipt_trust_anchor(source)
    return ExplicitEnvVault(source)


class _FakeOwner:
    def __init__(self, output: str, *, returncode: int = 0, confirmed: bool = True) -> None:
        self._events = [ProcessEvent(1, "stdout", output.encode("utf-8"))]
        self._returncode = returncode
        self._confirmed = confirmed
        self.background_error: BaseException | None = None

    @property
    def output_closed(self) -> bool:
        return not self._events

    def next_event(self, *, timeout: float) -> ProcessEvent | None:
        del timeout
        return self._events.pop(0) if self._events else None

    def poll(self) -> int:
        return self._returncode

    def _observation(self) -> ExitObservation:
        return ExitObservation(
            self._returncode,
            self._confirmed,
            0 if self._confirmed else 1,
            False,
            True,
            ContainmentState.KNOWN_EMPTY if self._confirmed else ContainmentState.UNKNOWN,
        )

    def wait(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        return self._observation()

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        return self._observation()

    def close(self) -> ExitObservation:
        return self._observation()


class _AdversarialCleanupOwner(_FakeOwner):
    def __init__(self, *, close_confirmed: bool = True) -> None:
        super().__init__("", confirmed=close_confirmed)
        self.close_called = False

    def next_event(self, *, timeout: float) -> ProcessEvent | None:
        del timeout
        raise RuntimeError("event transport failed")

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        raise RuntimeError("stop failed")

    def close(self) -> ExitObservation:
        self.close_called = True
        return self._observation()


def _owner_factory(
    captured: dict[str, object],
    output: dict[str, Any] | str,
    *,
    returncode: int = 0,
    confirmed: bool = True,
):
    encoded = output if isinstance(output, str) else json.dumps(output)

    def factory(command: object, **kwargs: object) -> _FakeOwner:
        captured["command"] = command
        captured.update(kwargs)
        return _FakeOwner(encoded, returncode=returncode, confirmed=confirmed)

    return factory


def test_live_dispatcher_scopes_claude_compatible_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    del monkeypatch
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": "safe", "OPENAI_API_KEY": "must-not-pass"},
        owner_factory=_owner_factory(captured, {
            "result": '```json\n{"status":"build_complete","proposal":"safe"}\n```',
            "modelUsage": {"deepseek-v4-pro": {}},
            "usage": {"input_tokens": 2, "output_tokens": 3},
        }),
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
    assert environment["ANTHROPIC_BASE_URL"] == (
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"
    )
    assert "must-not-be-used" not in environment.values()
    assert "OPENAI_API_KEY" not in environment
    assert response.provenance.provider == "deepseek"
    assert response.provenance.model == "deepseek-v4-pro"
    assert response.usage["prompt_tokens"] == 2


def test_live_dispatcher_uses_preflight_pinned_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    del monkeypatch
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": str(tmp_path / "shadow")},
        claude_binary="C:/trusted/claude.exe",
        runtime_directory="C:/isolated/torq-runtime",
        owner_factory=_owner_factory(captured, {
            "structured_output": {"status": "build_complete", "proposal": "safe"},
            "modelUsage": {"deepseek-v4-pro": {}},
            "usage": {},
        }),
    )

    dispatcher.dispatch(
        role="builder",
        provider="deepseek",
        model="deepseek-v4-pro",
        prompt="build",
    )

    command = captured["command"]
    assert isinstance(command, tuple)
    assert command[0] == "C:/trusted/claude.exe"
    assert command[command.index("--setting-sources") + 1] == ""
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in command
    assert captured["cwd"] == "C:/isolated/torq-runtime"
    assert "build" not in command
    assert captured["input_data"] == (
        b"build\nMake an independent evidence-based decision and populate the requested "
        b"structured output. Do not use tools, access files, execute commands, or claim to "
        b"have changed the target."
    )


def test_live_dispatcher_rejects_non_object_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path), {"PATH": "safe"},
        owner_factory=_owner_factory({}, {
            "result": "not json", "modelUsage": {"deepseek-v4-pro": {}},
        }),
    )
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
    del monkeypatch
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path), {"PATH": "safe"},
        owner_factory=_owner_factory({}, {"result": "{}", "modelUsage": {"other": {}}}),
    )
    with pytest.raises(OrchestrationBlocked, match="live_model_unattested:deepseek"):
        dispatcher.dispatch(
            role="builder",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt="proposal",
        )


def test_live_dispatcher_rejects_mixed_resolved_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path), {"PATH": "safe"},
        owner_factory=_owner_factory({}, {
            "result": '{"status":"build_complete","proposal":"safe"}',
            "modelUsage": {"deepseek-v4-pro": {}, "fallback-model": {}},
        }),
    )

    with pytest.raises(OrchestrationBlocked, match="live_model_unattested:deepseek"):
        dispatcher.dispatch(
            role="builder",
            provider="deepseek",
            model="deepseek-v4-pro",
            prompt="proposal",
        )


def test_openai_dispatcher_requires_exact_resolved_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://api.openai.com/v1/responses"

        def read(self, size: int = -1) -> bytes:
            encoded = json.dumps({
                "model": "gpt-5.5-preview",
                "output": [{"content": [{"text": '{"verdict":"approve","defects":[]}'}]}],
                "usage": {},
            }).encode("utf-8")
            return encoded if size < 0 else encoded[:size]

    class _Opener:
        def open(self, *_a: object, **_k: object) -> _Response:
            return _Response()

    del monkeypatch
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path), {"PATH": "safe"}, https_opener=_Opener()  # type: ignore[arg-type]
    )

    with pytest.raises(OrchestrationBlocked, match="live_model_unattested:openai"):
        dispatcher.dispatch(
            role="g2a",
            provider="openai",
            model="gpt-5.5",
            prompt="audit",
        )


def test_live_dispatcher_rejects_unknown_provider(tmp_path: Path) -> None:
    dispatcher = LiveStageDispatcher(_vault(tmp_path), {"PATH": "safe"})
    with pytest.raises(OrchestrationBlocked, match="live_provider_unsupported"):
        dispatcher.dispatch(role="g1d", provider="unknown", model="x", prompt="proposal")


def test_live_dispatcher_locally_rejects_off_schema_provider_object(tmp_path: Path) -> None:
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": "safe"},
        owner_factory=_owner_factory({}, {
            "structured_output": {
                "status": "build_complete", "proposal": "safe", "undeclared": "smuggled",
            },
            "modelUsage": {"deepseek-v4-pro": {}},
            "usage": {},
        }),
    )

    with pytest.raises(OrchestrationBlocked, match="live_provider_response_invalid:deepseek"):
        dispatcher.dispatch(
            role="builder", provider="deepseek", model="deepseek-v4-pro", prompt="build"
        )


@pytest.mark.parametrize(
    ("value", "schema"),
    [
        ({}, {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}}),
        ({"x": "ok", "y": "extra"}, {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}, "additionalProperties": False}),
        (1, {"type": "string"}),
        ("wrong", {"type": "string", "const": "right"}),
        ("wrong", {"type": "string", "enum": ["right"]}),
        ("too-long", {"type": "string", "maxLength": 3}),
        (["a", "b"], {"type": "array", "maxItems": 1, "items": {"type": "string"}}),
        ([1], {"type": "array", "items": {"type": "string"}}),
    ],
)
def test_contract_matcher_rejects_each_closed_schema_violation(
    value: object, schema: dict[str, object]
) -> None:
    assert not stage_response_module._matches_schema(value, schema)


def test_live_dispatcher_bounds_owned_process_output(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": "safe"},
        owner_factory=_owner_factory(
            captured, "X" * (live_provider_module._MAX_PROVIDER_OUTPUT_BYTES + 1)
        ),
    )

    with pytest.raises(OrchestrationBlocked, match="live_provider_response_too_large:deepseek"):
        dispatcher.dispatch(
            role="builder", provider="deepseek", model="deepseek-v4-pro", prompt="build"
        )


def test_live_dispatcher_never_terminalizes_unconfirmed_process_tree(tmp_path: Path) -> None:
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": "safe"},
        owner_factory=_owner_factory(
            {},
            {
                "structured_output": {"status": "build_complete", "proposal": "safe"},
                "modelUsage": {"deepseek-v4-pro": {}},
                "usage": {},
            },
            confirmed=False,
        ),
    )

    with pytest.raises(
        OrchestrationBlocked, match="live_provider_termination_unconfirmed:deepseek"
    ):
        dispatcher.dispatch(
            role="builder", provider="deepseek", model="deepseek-v4-pro", prompt="build"
        )


def test_live_dispatcher_closes_owner_when_event_and_force_stop_fail(tmp_path: Path) -> None:
    owner = _AdversarialCleanupOwner()
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": "safe"},
        owner_factory=lambda *args, **kwargs: owner,
    )

    with pytest.raises(OrchestrationBlocked, match="live_provider_command_failed:deepseek"):
        dispatcher.dispatch(
            role="builder", provider="deepseek", model="deepseek-v4-pro", prompt="build"
        )
    assert owner.close_called


def test_live_dispatcher_fails_closed_when_cleanup_cannot_confirm_exit(tmp_path: Path) -> None:
    owner = _AdversarialCleanupOwner(close_confirmed=False)
    dispatcher = LiveStageDispatcher(
        _vault(tmp_path),
        {"PATH": "safe"},
        owner_factory=lambda *args, **kwargs: owner,
    )

    with pytest.raises(
        OrchestrationBlocked, match="live_provider_termination_unconfirmed:deepseek"
    ):
        dispatcher.dispatch(
            role="builder", provider="deepseek", model="deepseek-v4-pro", prompt="build"
        )
    assert owner.close_called


def test_direct_openai_opener_ignores_ambient_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.invalid:8080")

    opener = live_provider_module._direct_https_opener()
    proxy_handlers = [
        handler for handler in opener.handlers
        if isinstance(handler, live_provider_module.urllib.request.ProxyHandler)
    ]

    # ProxyHandler({}) deliberately contributes no proxy_open method, so the
    # resulting opener contains no proxy handler at all.
    assert proxy_handlers == []


def test_openai_dispatcher_bounds_response_body(tmp_path: Path) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def geturl(self) -> str:
            return "https://api.openai.com/v1/responses"

        def read(self, size: int = -1) -> bytes:
            del size
            return b"X" * (live_provider_module._MAX_PROVIDER_OUTPUT_BYTES + 1)

    class _Opener:
        def open(self, *_a: object, **_k: object) -> _Response:
            return _Response()

    dispatcher = LiveStageDispatcher(
        _vault(tmp_path), {"PATH": "safe"}, https_opener=_Opener()  # type: ignore[arg-type]
    )
    with pytest.raises(OrchestrationBlocked, match="live_provider_response_too_large:openai"):
        dispatcher.dispatch(role="g2a", provider="openai", model="gpt-5.5", prompt="audit")
