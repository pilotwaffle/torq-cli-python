"""Explicit external credential sources and provider-scoped child environments."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from torq_cli.connectors.native_credentials import (
    ConfiguredNativeVault,
    native_store_for_current_platform,
)
from torq_cli.connectors.headless_credentials import (
    ConfiguredHeadlessVault,
    HeadlessEncryptedFileStore,
)


MAX_CREDENTIAL_SOURCE_BYTES = 65_536
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROVIDER_KEYS: Mapping[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "codex": ("OPENAI_API_KEY",),
    "qwen": ("QWEN_TOKEN_PLAN_API_KEY", "BAILIAN_CODING_PLAN_API_KEY"),
    # DeepSeek is billed to the Alibaba/Qwen Token Plan, not to a direct
    # DeepSeek account, so it draws on the Qwen entitlement and credential.
    # DEEPSEEK_API_KEY is deliberately absent: falling back to it would
    # silently switch the lane from plan-covered to metered settlement.
    "deepseek": ("QWEN_TOKEN_PLAN_API_KEY", "BAILIAN_CODING_PLAN_API_KEY"),
    "kimi": ("KIMI_CODE_API_KEY", "KIMI_API_KEY"),
    "zai": ("GLM_API_KEY", "ZAI_API_KEY"),
}
_PROVIDER_BASE_URL_KEYS: Mapping[str, tuple[str, ...]] = {
    "qwen": ("QWEN_TOKEN_PLAN_BASE_URL",),
    "deepseek": ("QWEN_TOKEN_PLAN_BASE_URL",),
}
# The Token Plan is regional. This is the documented default host; an operator
# entitled to another region names it in their credential source and it wins.
_TOKEN_PLAN_BASE_URL = "https://token-plan.ap-southeast-1.maas.aliyuncs.com/apps/anthropic"
_SAFE_CHILD_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "HOME", "USERPROFILE",
})
_CLAUDE_COMPAT = {
    "deepseek": (_TOKEN_PLAN_BASE_URL, "deepseek-v4-pro"),
    "kimi": ("https://api.kimi.com/coding/", "k3"),
    "qwen": (_TOKEN_PLAN_BASE_URL, "qwen3.8-max-preview"),
    "zai": ("https://api.z.ai/api/anthropic", ""),
}


def safe_child_environment(base_environment: Mapping[str, str]) -> dict[str, str]:
    """Retain only non-secret operating-system variables for a provider child."""
    return {
        key: value
        for key, value in base_environment.items()
        if key.upper() in _SAFE_CHILD_KEYS
    }


class CredentialSourceError(ValueError):
    """Fail-closed credential-source error with a secret-free reason."""


class CredentialVault(Protocol):
    def get(self, provider: str) -> str | None: ...

    def base_url(self, provider: str) -> str | None: ...


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_env(payload: bytes) -> dict[str, str]:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CredentialSourceError("credential_source_utf8_invalid") from exc
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or _KEY.fullmatch(key) is None:
            raise CredentialSourceError("credential_source_syntax_invalid")
        if key in parsed:
            raise CredentialSourceError("credential_source_duplicate_key")
        parsed[key] = _unquote(raw_value.strip())
    return parsed


class ExplicitEnvVault:
    """Read a bounded, explicit external env file without copying it into TORQ."""

    def __init__(self, source: Path) -> None:
        if not source.is_absolute():
            raise CredentialSourceError("credential_source_absolute_required")
        try:
            if source.is_symlink() or not source.is_file():
                raise CredentialSourceError("credential_source_regular_file_required")
            if source.stat().st_size > MAX_CREDENTIAL_SOURCE_BYTES:
                raise CredentialSourceError("credential_source_too_large")
            payload = source.read_bytes()
        except CredentialSourceError:
            raise
        except OSError as exc:
            raise CredentialSourceError("credential_source_unreadable") from exc
        if len(payload) > MAX_CREDENTIAL_SOURCE_BYTES:
            raise CredentialSourceError("credential_source_too_large")
        self._values = _parse_env(payload)

    def __repr__(self) -> str:
        return f"ExplicitEnvVault(configured={len(self.configured_providers())})"

    def get(self, provider: str) -> str | None:
        for key in _PROVIDER_KEYS.get(provider.casefold(), ()):
            value = self._values.get(key)
            if value:
                return value
        return None

    def configured_providers(self) -> frozenset[str]:
        return frozenset(provider for provider in _PROVIDER_KEYS if self.get(provider) is not None)

    def base_url(self, provider: str) -> str | None:
        for key in _PROVIDER_BASE_URL_KEYS.get(provider.casefold(), ()):
            value = self._values.get(key)
            if value:
                return value
        return None


def credential_vault_from_config(config: Mapping[str, object]) -> CredentialVault:
    """Resolve one explicit credential source without consulting ambient secrets."""
    source = config.get("credential_source")
    if not isinstance(source, Mapping):
        raise CredentialSourceError("credential_source_missing")
    if source.get("kind") == "external_env" and set(source) == {"kind", "path"}:
        path = source.get("path")
        if not isinstance(path, str):
            raise CredentialSourceError("credential_source_invalid")
        return ExplicitEnvVault(Path(path))
    kind = source.get("kind")
    if kind in {"platform_keychain", "headless_encrypted_file"}:
        expected_keys = {"kind"} if kind == "platform_keychain" else {"kind", "path"}
        if set(source) != expected_keys:
            raise CredentialSourceError("credential_source_invalid")
        connectors = config.get("connectors")
        if not isinstance(connectors, Mapping):
            raise CredentialSourceError("credential_source_invalid")
        references: dict[str, str] = {}
        for raw in connectors.values():
            if not isinstance(raw, Mapping):
                continue
            provider_id = raw.get("provider_id")
            credential_ref = raw.get("credential_ref")
            if isinstance(provider_id, str) and isinstance(credential_ref, str):
                if provider_id in references and references[provider_id] != credential_ref:
                    raise CredentialSourceError("credential_source_invalid")
                references[provider_id] = credential_ref
        if kind == "platform_keychain":
            return ConfiguredNativeVault(native_store_for_current_platform(), references)
        raw_path = source.get("path")
        if not isinstance(raw_path, str):
            raise CredentialSourceError("credential_source_invalid")
        return ConfiguredHeadlessVault(
            HeadlessEncryptedFileStore(Path(raw_path)), references
        )
    raise CredentialSourceError("credential_source_invalid")


def claude_compatible_environment(
    provider: str,
    vault: CredentialVault,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Build one provider-scoped Claude-compatible child environment."""
    normalized = provider.casefold()
    if normalized not in _CLAUDE_COMPAT:
        raise CredentialSourceError("provider_unsupported")
    credential = vault.get(normalized)
    if credential is None:
        raise CredentialSourceError("provider_credential_missing")
    base_url, model = _CLAUDE_COMPAT[normalized]
    if normalized in _PROVIDER_BASE_URL_KEYS:
        # One Token Plan, one regional host: both plan lanes follow whichever
        # region the operator's credential source declares. A source that
        # declares none (the native keychain) keeps the documented default; one
        # that declares something unusable fails closed rather than silently
        # falling back to a region the account may not be entitled to.
        reader = getattr(vault, "base_url", None)
        declared = reader(normalized) if callable(reader) else None
        if declared is not None:
            if not isinstance(declared, str) or not declared.startswith("https://"):
                raise CredentialSourceError("provider_base_url_invalid")
            base_url = declared
    child = safe_child_environment(base_environment)
    child.update({
        "ANTHROPIC_AUTH_TOKEN": credential,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_API_KEY": "",
    })
    if normalized == "deepseek":
        child.update({
            "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CODE_EFFORT_LEVEL": "",
            "MAX_THINKING_TOKENS": "",
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "262144",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "262144",
        })
    return child


def openai_compatible_environment(
    provider: str,
    vault: CredentialVault,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Build one OpenAI-compatible child environment without ambient secrets."""
    normalized = provider.casefold()
    if normalized != "codex":
        raise CredentialSourceError("provider_unsupported")
    credential = vault.get(normalized)
    if credential is None:
        raise CredentialSourceError("provider_credential_missing")
    child = safe_child_environment(base_environment)
    child.update({
        "OPENAI_API_KEY": credential,
        "OPENAI_BASE_URL": "https://api.openai.com/v1",
        "OPENAI_MODEL": "gpt-5.5",
    })
    return child


def provider_environment_from_config(
    config: Mapping[str, object],
    provider: str,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Resolve the saved source and build one production child environment."""
    vault = credential_vault_from_config(config)
    if provider.casefold() == "codex":
        return openai_compatible_environment(provider, vault, base_environment)
    return claude_compatible_environment(provider, vault, base_environment)


__all__ = [
    "CredentialSourceError",
    "CredentialVault",
    "ExplicitEnvVault",
    "MAX_CREDENTIAL_SOURCE_BYTES",
    "claude_compatible_environment",
    "credential_vault_from_config",
    "openai_compatible_environment",
    "provider_environment_from_config",
    "safe_child_environment",
]
