from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from test_phase5_cli_experience import _answers

from torq_cli.adapters import process as process_module
from torq_cli.adapters.process import ManagedProcess
from torq_cli.connectors.credential_sources import (
    CredentialSourceError,
    ExplicitEnvVault,
    claude_compatible_environment,
    openai_compatible_environment,
    provider_environment_from_config,
)
from torq_cli.domain.config_schema import parse_config_text, validate_config
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces.cli import main
from torq_cli.safety.receipts import restrict_receipt_trust_anchor


def _credential_file(tmp_path: Path) -> Path:
    source = tmp_path / ".env"
    source.write_text(
        "\n".join(
            (
                "DEEPSEEK_API_KEY=deep-secret",
                "KIMI_API_KEY=old-kimi-secret",
                "KIMI_CODE_API_KEY='current-kimi-secret'",
                'GLM_API_KEY="glm-secret"',
                "OPENAI_API_KEY=openai-secret",
                "QWEN_TOKEN_PLAN_API_KEY=qwen-secret",
                "QWEN_TOKEN_PLAN_BASE_URL=https://token-plan.us-east-1.maas.aliyuncs.com/apps/anthropic",
            )
        ),
        encoding="utf-8",
    )
    restrict_receipt_trust_anchor(source)
    return source


def test_explicit_env_vault_maps_current_provider_names_without_leaking(tmp_path: Path) -> None:
    vault = ExplicitEnvVault(_credential_file(tmp_path))

    # DeepSeek bills to the Qwen Token Plan, so both lanes resolve one key.
    assert vault.get("deepseek") == "qwen-secret"
    assert vault.get("kimi") == "current-kimi-secret"
    assert vault.get("zai") == "glm-secret"
    assert vault.get("codex") == "openai-secret"
    assert vault.get("qwen") == "qwen-secret"
    assert vault.get("unknown") is None
    rendered = repr(vault)
    assert "secret" not in rendered
    assert vault.configured_providers() == frozenset({"codex", "deepseek", "kimi", "qwen", "zai"})


def test_source_is_explicit_bounded_regular_and_duplicate_keys_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CredentialSourceError, match="credential_source_absolute_required"):
        ExplicitEnvVault(Path(".env"))
    oversized = tmp_path / "oversized.env"
    oversized.write_bytes(b"X" * 65_537)
    restrict_receipt_trust_anchor(oversized)
    with pytest.raises(CredentialSourceError, match="credential_source_too_large"):
        ExplicitEnvVault(oversized)
    duplicate = tmp_path / "duplicate.env"
    duplicate.write_text("KIMI_API_KEY=one\nKIMI_API_KEY=two\n", encoding="utf-8")
    restrict_receipt_trust_anchor(duplicate)
    with pytest.raises(CredentialSourceError, match="credential_source_duplicate_key"):
        ExplicitEnvVault(duplicate)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_external_source_rejects_symlink(tmp_path: Path) -> None:
    source = _credential_file(tmp_path)
    link = tmp_path / "linked.env"
    link.symlink_to(source)

    with pytest.raises(CredentialSourceError, match="credential_source_(unreadable|regular_file_required)"):
        ExplicitEnvVault(link)


def test_external_source_permissions_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "unsafe.env"
    source.write_text("OPENAI_API_KEY=test-only\n", encoding="utf-8")
    monkeypatch.setattr(
        "torq_cli.connectors.credential_sources.signing_file_permissions_are_restricted",
        lambda _path: False,
    )

    with pytest.raises(CredentialSourceError, match="credential_source_permissions_unsafe"):
        ExplicitEnvVault(source)


@pytest.mark.parametrize(
    "declared",
    [
        "https://attacker.example/apps/anthropic",
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com.evil.test/apps/anthropic",
        "https://user@token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic",
        "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic?redirect=1",
        "https://token-plan.-.maas.aliyuncs.com/apps/anthropic",
        "https://token-plan.-us.maas.aliyuncs.com/apps/anthropic",
        "https://token-plan.us-.maas.aliyuncs.com/apps/anthropic",
        "https://token-plan.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.maas.aliyuncs.com/apps/anthropic",
    ],
)
def test_token_plan_override_is_restricted_to_canonical_alibaba_route(
    tmp_path: Path, declared: str
) -> None:
    source = tmp_path / "route.env"
    source.write_text(
        f"QWEN_TOKEN_PLAN_API_KEY=test-only\nQWEN_TOKEN_PLAN_BASE_URL={declared}\n",
        encoding="utf-8",
    )
    restrict_receipt_trust_anchor(source)
    vault = ExplicitEnvVault(source)

    with pytest.raises(CredentialSourceError, match="provider_base_url_invalid"):
        claude_compatible_environment("deepseek", vault, {"PATH": "safe"})


def test_provider_child_environment_contains_only_selected_secret(tmp_path: Path) -> None:
    vault = ExplicitEnvVault(_credential_file(tmp_path))
    base = {"PATH": "safe", "UNRELATED_API_KEY": "must-not-pass"}

    deepseek = claude_compatible_environment("deepseek", vault, base)
    assert deepseek["PATH"] == "safe"
    assert deepseek["ANTHROPIC_AUTH_TOKEN"] == "qwen-secret"
    # The declared Token Plan region governs both plan lanes, not just qwen.
    assert deepseek["ANTHROPIC_BASE_URL"] == (
        "https://token-plan.us-east-1.maas.aliyuncs.com/apps/anthropic"
    )
    assert deepseek["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    assert deepseek["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "deepseek-v4-pro"
    assert deepseek["CLAUDE_CODE_SUBAGENT_MODEL"] == "deepseek-v4-pro"
    assert deepseek["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "262144"
    assert deepseek["ANTHROPIC_API_KEY"] == ""
    assert "deep-secret" not in deepseek.values()
    kimi = claude_compatible_environment("kimi", vault, base)
    assert kimi["ANTHROPIC_AUTH_TOKEN"] == "current-kimi-secret"
    assert kimi["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert kimi["ANTHROPIC_MODEL"] == "k3"
    assert "deep-secret" not in kimi.values()
    zai = claude_compatible_environment("zai", vault, base)
    assert zai["ANTHROPIC_AUTH_TOKEN"] == "glm-secret"
    assert zai["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert zai["ANTHROPIC_MODEL"] == ""
    qwen = claude_compatible_environment("qwen", vault, base)
    assert qwen["ANTHROPIC_AUTH_TOKEN"] == "qwen-secret"
    assert qwen["ANTHROPIC_BASE_URL"] == (
        "https://token-plan.us-east-1.maas.aliyuncs.com/apps/anthropic"
    )
    assert qwen["ANTHROPIC_MODEL"] == "qwen3.8-max-preview"
    assert "openai-secret" not in qwen.values()
    with pytest.raises(CredentialSourceError, match="provider_unsupported"):
        claude_compatible_environment("codex", vault, base)
    codex = openai_compatible_environment("codex", vault, base)
    assert codex == {
        "PATH": "safe",
        "OPENAI_API_KEY": "openai-secret",
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-5.5",
    }
    assert "qwen-secret" not in codex.values()


def test_external_source_detects_path_identity_change_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _credential_file(tmp_path)
    real_samestat = __import__("os").path.samestat
    real_close = __import__("os").close
    comparisons = 0
    closed: list[int] = []

    def changing_samestat(left, right):
        nonlocal comparisons
        comparisons += 1
        return real_samestat(left, right) if comparisons == 1 else False

    def tracking_close(descriptor: int) -> None:
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr("torq_cli.connectors.credential_sources.os.path.samestat", changing_samestat)
    monkeypatch.setattr("torq_cli.connectors.credential_sources.os.close", tracking_close)

    with pytest.raises(CredentialSourceError, match="credential_source_changed"):
        ExplicitEnvVault(source)
    assert len(closed) == 1


def test_external_source_detects_permission_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _credential_file(tmp_path)
    checks = 0

    def changing_permissions(_path: Path) -> bool:
        nonlocal checks
        checks += 1
        return checks == 1

    monkeypatch.setattr(
        "torq_cli.connectors.credential_sources.signing_file_permissions_are_restricted",
        changing_permissions,
    )

    with pytest.raises(CredentialSourceError, match="credential_source_changed"):
        ExplicitEnvVault(source)


def test_auth_status_accepts_explicit_external_store_without_printing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _credential_file(tmp_path)
    code = main(["auth", "status", "--credential-file", str(source)])
    output = capsys.readouterr().out
    report = json.loads(output)

    assert code == 3  # Credentials are configured, but live model identity is not yet attested.
    for provider in ("codex", "deepseek", "kimi", "qwen", "zai"):
        assert report["providers"][provider]["credential_configured"] is True
    for provider in ("codex", "deepseek", "kimi", "qwen", "zai"):
        assert report["providers"][provider]["authentication"] == "configured"
        assert report["providers"][provider]["resolved_model_identity"] == "unattestable"
    assert "deep-secret" not in output
    assert "current-kimi-secret" not in output
    assert "glm-secret" not in output


def test_setup_records_only_external_source_path_and_checks_direct_provider_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _credential_file(tmp_path)
    answers = tmp_path / "answers.json"
    target = tmp_path / "config.yaml"
    answers.write_text(json.dumps(_answers()), encoding="utf-8")

    assert main([
        "setup", "--config", str(target), "--answers", str(answers),
        "--credential-file", str(source),
    ]) == 0
    capsys.readouterr()
    rendered = target.read_text(encoding="utf-8")
    assert "kind: external_env" in rendered
    assert str(source) in rendered
    assert "deep-secret" not in rendered
    assert "current-kimi-secret" not in rendered
    assert "glm-secret" not in rendered
    parsed = parse_config_text(rendered)
    assert validate_config(parsed, load_registry()) == ()
    environment = provider_environment_from_config(parsed, "deepseek", {"PATH": "safe"})
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "qwen-secret"
    assert environment["ANTHROPIC_BASE_URL"] == (
        "https://token-plan.us-east-1.maas.aliyuncs.com/apps/anthropic"
    )
    assert environment["ANTHROPIC_MODEL"] == "deepseek-v4-pro"
    codex_environment = provider_environment_from_config(parsed, "codex", {"PATH": "safe"})
    assert codex_environment["OPENAI_API_KEY"] == "openai-secret"
    assert codex_environment["OPENAI_MODEL"] == "gpt-5.5"

    incomplete = tmp_path / "incomplete.env"
    incomplete.write_text(
        "QWEN_TOKEN_PLAN_API_KEY=qwen\nKIMI_API_KEY=kimi\nGLM_API_KEY=glm\n",
        encoding="utf-8",
    )
    restrict_receipt_trust_anchor(incomplete)
    previous_config = target.read_bytes()
    assert main([
        "setup", "--config", str(target), "--answers", str(answers),
        "--credential-file", str(incomplete),
    ]) == 3
    assert json.loads(capsys.readouterr().out)["finding"] == (
        "provider_credential_missing:codex"
    )
    assert target.read_bytes() == previous_config


def test_managed_process_loads_saved_source_and_scopes_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _credential_file(tmp_path)
    captured: dict[str, object] = {}

    class FakeProcess:
        pass

    def fake_popen(command, **kwargs):
        captured.update({"command": command, **kwargs})
        return FakeProcess()

    monkeypatch.setattr(process_module.subprocess, "Popen", fake_popen)
    config = {"credential_source": {"kind": "external_env", "path": str(source)}}
    managed = ManagedProcess.for_provider_config(
        ("claude", "-p", "fixture"),
        cwd=str(tmp_path),
        provider="kimi",
        config=config,
        base_environment={"PATH": "safe", "DEEPSEEK_API_KEY": "must-not-pass"},
    )

    assert isinstance(managed.process, FakeProcess)
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "current-kimi-secret"
    assert environment["ANTHROPIC_BASE_URL"] == "https://api.kimi.com/coding/"
    assert "DEEPSEEK_API_KEY" not in environment
