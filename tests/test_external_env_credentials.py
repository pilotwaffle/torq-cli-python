from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.adapters import process as process_module
from torq_cli.adapters.process import ManagedProcess
from torq_cli.connectors.credential_sources import (
    CredentialSourceError,
    ExplicitEnvVault,
    claude_compatible_environment,
    provider_environment_from_config,
)
from torq_cli.domain.config_schema import parse_config_text, validate_config
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces.cli import main
from test_phase5_cli_experience import _answers


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
            )
        ),
        encoding="utf-8",
    )
    return source


def test_explicit_env_vault_maps_current_provider_names_without_leaking(tmp_path: Path) -> None:
    vault = ExplicitEnvVault(_credential_file(tmp_path))

    assert vault.get("deepseek") == "deep-secret"
    assert vault.get("kimi") == "current-kimi-secret"
    assert vault.get("zai") == "glm-secret"
    assert vault.get("codex") == "openai-secret"
    assert vault.get("unknown") is None
    rendered = repr(vault)
    assert "secret" not in rendered
    assert vault.configured_providers() == frozenset({"codex", "deepseek", "kimi", "zai"})


def test_source_is_explicit_bounded_regular_and_duplicate_keys_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CredentialSourceError, match="credential_source_absolute_required"):
        ExplicitEnvVault(Path(".env"))
    oversized = tmp_path / "oversized.env"
    oversized.write_bytes(b"X" * 65_537)
    with pytest.raises(CredentialSourceError, match="credential_source_too_large"):
        ExplicitEnvVault(oversized)
    duplicate = tmp_path / "duplicate.env"
    duplicate.write_text("KIMI_API_KEY=one\nKIMI_API_KEY=two\n", encoding="utf-8")
    with pytest.raises(CredentialSourceError, match="credential_source_duplicate_key"):
        ExplicitEnvVault(duplicate)


def test_provider_child_environment_contains_only_selected_secret(tmp_path: Path) -> None:
    vault = ExplicitEnvVault(_credential_file(tmp_path))
    base = {"PATH": "safe", "UNRELATED_API_KEY": "must-not-pass"}

    deepseek = claude_compatible_environment("deepseek", vault, base)
    assert deepseek == {
        "PATH": "safe",
        "ANTHROPIC_AUTH_TOKEN": "deep-secret",
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_API_KEY": "",
    }
    kimi = claude_compatible_environment("kimi", vault, base)
    assert kimi["ANTHROPIC_AUTH_TOKEN"] == "current-kimi-secret"
    assert kimi["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic/"
    assert "deep-secret" not in kimi.values()
    zai = claude_compatible_environment("zai", vault, base)
    assert zai["ANTHROPIC_AUTH_TOKEN"] == "glm-secret"
    assert zai["ANTHROPIC_BASE_URL"] == "https://api.z.ai/api/anthropic"
    assert zai["ANTHROPIC_MODEL"] == ""
    with pytest.raises(CredentialSourceError, match="provider_unsupported"):
        claude_compatible_environment("codex", vault, base)


def test_auth_status_accepts_explicit_external_store_without_printing_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _credential_file(tmp_path)
    code = main(["auth", "status", "--credential-file", str(source)])
    output = capsys.readouterr().out
    report = json.loads(output)

    assert code == 3  # Credentials are configured, but live model identity is not yet attested.
    for provider in ("deepseek", "kimi", "zai"):
        assert report["providers"][provider]["credential_configured"] is True
    for provider in ("deepseek", "kimi", "zai"):
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
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "deep-secret"
    assert environment["ANTHROPIC_MODEL"] == "deepseek-v4-pro"

    incomplete = tmp_path / "incomplete.env"
    incomplete.write_text("DEEPSEEK_API_KEY=only-one\n", encoding="utf-8")
    assert main([
        "setup", "--config", str(target), "--answers", str(answers),
        "--credential-file", str(incomplete),
    ]) == 3
    assert json.loads(capsys.readouterr().out)["finding"] == "provider_credential_missing:kimi,zai"


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
    assert environment["ANTHROPIC_BASE_URL"] == "https://api.moonshot.ai/anthropic/"
    assert "DEEPSEEK_API_KEY" not in environment
