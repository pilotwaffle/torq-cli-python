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


MAX_CREDENTIAL_SOURCE_BYTES = 65_536
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROVIDER_KEYS: Mapping[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY",),
    "codex": ("OPENAI_API_KEY",),
    "grok": ("XAI_API_KEY", "GROK_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "kimi": ("KIMI_CODE_API_KEY", "KIMI_API_KEY"),
    "zai": ("GLM_API_KEY", "ZAI_API_KEY"),
}
_SAFE_CHILD_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    "LANG", "LC_ALL", "HOME", "USERPROFILE",
})
_CLAUDE_COMPAT = {
    "deepseek": ("https://api.deepseek.com/anthropic", "deepseek-v4-pro"),
    "kimi": ("https://api.moonshot.ai/anthropic/", "kimi-k3"),
    "zai": ("https://api.z.ai/api/anthropic", ""),
}


class CredentialSourceError(ValueError):
    """Fail-closed credential-source error with a secret-free reason."""


class CredentialVault(Protocol):
    def get(self, provider: str) -> str | None: ...


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
    child = {
        key: value
        for key, value in base_environment.items()
        if key.upper() in _SAFE_CHILD_KEYS
    }
    child.update({
        "ANTHROPIC_AUTH_TOKEN": credential,
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_API_KEY": "",
    })
    return child


def provider_environment_from_config(
    config: Mapping[str, object],
    provider: str,
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Resolve the saved source and build one production child environment."""
    source = config.get("credential_source")
    if not isinstance(source, Mapping):
        raise CredentialSourceError("credential_source_missing")
    if source.get("kind") == "external_env" and set(source) == {"kind", "path"}:
        path = source.get("path")
        if not isinstance(path, str):
            raise CredentialSourceError("credential_source_invalid")
        vault: CredentialVault = ExplicitEnvVault(Path(path))
    elif source.get("kind") == "platform_keychain" and set(source) == {"kind"}:
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
        vault = ConfiguredNativeVault(native_store_for_current_platform(), references)
    else:
        raise CredentialSourceError("credential_source_invalid")
    return claude_compatible_environment(provider, vault, base_environment)


__all__ = [
    "CredentialSourceError",
    "CredentialVault",
    "ExplicitEnvVault",
    "MAX_CREDENTIAL_SOURCE_BYTES",
    "claude_compatible_environment",
    "provider_environment_from_config",
]
