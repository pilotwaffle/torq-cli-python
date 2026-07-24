from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.application.setup import SetupError, SetupService
from torq_cli.connectors import credential_sources
from torq_cli.connectors import native_credentials
from torq_cli.connectors.native_credentials import (
    ConfiguredNativeVault,
    NativeCredentialError,
    NativeCredentialStore,
)
from torq_cli.domain.credential_backend import BackendUnavailable
from torq_cli.domain.config_schema import parse_config_text, validate_config
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces import cli as cli_module
from torq_cli.interfaces.cli import main
from test_phase5_cli_experience import _answers


_REF = "credref_0123456789abcdef0123456789abcdef"


class _MemoryKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail = False

    def get_keyring(self) -> object:
        return object()

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.fail:
            raise RuntimeError("secret-bearing backend detail")
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self.fail:
            raise RuntimeError("secret-bearing backend detail")
        self.values[(service_name, username)] = password

    def delete_password(self, service_name: str, username: str) -> None:
        if self.fail:
            raise RuntimeError("secret-bearing backend detail")
        del self.values[(service_name, username)]


def _store(driver: _MemoryKeyring | None = None) -> NativeCredentialStore:
    return NativeCredentialStore(
        "windows",
        driver=driver or _MemoryKeyring(),
        verify_backend=False,
    )


def test_native_store_round_trip_revoke_and_provider_aliases() -> None:
    driver = _MemoryKeyring()
    store = _store(driver)

    store.store("kimi", _REF, "test-only-value")
    assert store.resolve("moonshot", _REF) == "test-only-value"
    assert store.contains("kimi", _REF) is True
    assert store.revoke("moonshot", _REF) is True
    assert store.contains("kimi", _REF) is False
    assert store.revoke("kimi", _REF) is False


def test_native_store_validates_inputs_and_collapses_backend_details() -> None:
    driver = _MemoryKeyring()
    store = _store(driver)

    for invalid in ("bad", "credref_" + "0" * 31, "credref_" + "G" * 32):
        with pytest.raises(NativeCredentialError, match="credential_ref_invalid"):
            store.store("deepseek", invalid, "value")
    for secret in ("", "contains\x00nul", "x" * 16_385):
        with pytest.raises(NativeCredentialError, match="credential_value_invalid"):
            store.store("deepseek", _REF, secret)
    with pytest.raises(NativeCredentialError, match="credential_provider_unsupported"):
        store.store("unknown", _REF, "value")

    driver.fail = True
    with pytest.raises(NativeCredentialError) as caught:
        store.store("deepseek", _REF, "must-not-leak")
    assert "secret-bearing" not in str(caught.value)
    assert "must-not-leak" not in str(caught.value)


def test_native_backend_identity_and_headless_fallback_fail_closed() -> None:
    with pytest.raises(BackendUnavailable, match="credential_backend_mismatch"):
        NativeCredentialStore("windows", driver=_MemoryKeyring())
    with pytest.raises(BackendUnavailable, match="attended_encrypted_file_not_implemented"):
        NativeCredentialStore(
            "linux",
            headless=True,
            driver=_MemoryKeyring(),
            verify_backend=False,
        )


def test_current_linux_session_facts_do_not_promote_headless_to_secret_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_credentials.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    with pytest.raises(BackendUnavailable, match="attended_encrypted_file_not_implemented"):
        native_credentials.native_store_for_current_platform()


def test_configured_vault_resolves_only_mapped_provider_reference() -> None:
    driver = _MemoryKeyring()
    store = _store(driver)
    store.store("deepseek", _REF, "test-only-value")
    vault = ConfiguredNativeVault(store, {"deepseek": _REF})

    assert vault.get("deepseek") == "test-only-value"
    assert vault.get("kimi") is None
    assert "test-only-value" not in repr(vault)


def test_setup_persists_only_native_refs_and_runtime_resolves_selected_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = _answers()
    answers["credential_refs"] = {
        "deepseek": _REF,
        "kimi": "credref_11111111111111111111111111111111",
        "zai": "credref_22222222222222222222222222222222",
    }
    target = tmp_path / "config.yaml"
    document = SetupService().configure(target, answers)
    rendered = target.read_text(encoding="utf-8")

    assert document["credential_source"] == {"kind": "platform_keychain"}
    assert "kind: platform_keychain" in rendered
    assert "credential_ref:" in rendered
    assert validate_config(parse_config_text(rendered), load_registry()) == ()

    driver = _MemoryKeyring()
    store = _store(driver)
    store.store("deepseek", _REF, "test-only-value")
    monkeypatch.setattr(credential_sources, "native_store_for_current_platform", lambda: store)
    environment = credential_sources.provider_environment_from_config(
        document,
        "deepseek",
        {"PATH": "safe", "KIMI_API_KEY": "must-not-pass"},
    )
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "test-only-value"
    assert "KIMI_API_KEY" not in environment

    missing = _answers()
    missing["credential_refs"] = {"deepseek": _REF}
    with pytest.raises(SetupError, match="provider_credential_ref_missing:kimi,zai"):
        SetupService().configure(tmp_path / "missing.yaml", missing)

    unknown = _answers()
    unknown["credential_refs"] = {
        "deepseek": _REF,
        "kimi": "credref_11111111111111111111111111111111",
        "zai": "credref_22222222222222222222222222222222",
        "unknown": "credref_33333333333333333333333333333333",
    }
    with pytest.raises(SetupError, match="credential_refs_invalid"):
        SetupService().configure(tmp_path / "unknown.yaml", unknown)


def test_native_config_rejects_conflicting_refs_for_one_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        credential_sources,
        "native_store_for_current_platform",
        lambda: _store(),
    )
    config = {
        "credential_source": {"kind": "platform_keychain"},
        "connectors": {
            "one": {"provider_id": "deepseek", "credential_ref": _REF},
            "two": {
                "provider_id": "deepseek",
                "credential_ref": "credref_11111111111111111111111111111111",
            },
        },
    }
    with pytest.raises(credential_sources.CredentialSourceError, match="credential_source_invalid"):
        credential_sources.provider_environment_from_config(config, "deepseek", {})


def test_native_auth_cli_never_prints_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    store = _store()
    monkeypatch.setattr(cli_module, "native_store_for_current_platform", lambda: store)
    monkeypatch.setattr(cli_module, "_read_attended_secret", lambda: "test-only-value")

    common = ["--provider", "deepseek", "--credential-ref", _REF]
    assert main(["auth", "store", *common]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "backend": "windows_credential_manager",
        "status": "stored",
    }
    assert main(["auth", "verify-access", *common]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "access_verified"
    assert main(["auth", "revoke", *common]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "revoked"
    assert "test-only-value" not in output


def test_secret_input_rejects_non_attended_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Pipe:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(cli_module.sys, "stdin", _Pipe())
    with pytest.raises(NativeCredentialError, match="attended_secret_input_required"):
        cli_module._read_attended_secret()
