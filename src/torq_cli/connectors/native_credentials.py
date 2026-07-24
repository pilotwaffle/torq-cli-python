"""Native OS credential-store access behind opaque TORQ references."""

from __future__ import annotations

import importlib
import os
import platform
import re
from collections.abc import Mapping
from typing import Protocol, cast

from torq_cli.domain.credential_backend import BackendUnavailable, select_credential_backend


_CREDENTIAL_REF = re.compile(r"credref_[0-9a-f]{32}\Z")
_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "codex": "openai",
    "openai": "openai",
    "qwen": "qwen",
    "alibaba": "qwen",
    "kimi": "moonshot",
    "moonshot": "moonshot",
    "zai": "zai",
    "deepseek": "deepseek",
}
_EXPECTED_BACKEND_MODULE = {
    "windows_credential_manager": "keyring.backends.Windows",
    "macos_keychain": "keyring.backends.macOS",
    "secret_service": "keyring.backends.SecretService",
}
_MAX_SECRET_BYTES = 16_384


class NativeCredentialError(RuntimeError):
    """Fail-closed native credential error with a secret-free reason."""


class KeyringDriver(Protocol):
    def get_keyring(self) -> object: ...

    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...

    def delete_password(self, service_name: str, username: str) -> None: ...


def _load_keyring() -> KeyringDriver:
    try:
        module = importlib.import_module("keyring")
    except (ImportError, OSError) as exc:
        raise BackendUnavailable("credential_backend_dependency_unavailable") from exc
    return cast(KeyringDriver, module)


def _provider_id(provider: str) -> str:
    normalized = _PROVIDER_ALIASES.get(provider.casefold())
    if normalized is None:
        raise NativeCredentialError("credential_provider_unsupported")
    return normalized


def _validate_reference(credential_ref: str) -> None:
    if _CREDENTIAL_REF.fullmatch(credential_ref) is None:
        raise NativeCredentialError("credential_ref_invalid")


def _validate_secret(secret: str) -> None:
    try:
        encoded = secret.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise NativeCredentialError("credential_value_invalid") from exc
    if not encoded or len(encoded) > _MAX_SECRET_BYTES or "\x00" in secret:
        raise NativeCredentialError("credential_value_invalid")


class NativeCredentialStore:
    """Read and mutate one user's approved native OS credential backend."""

    def __init__(
        self,
        platform_name: str,
        *,
        headless: bool = False,
        secret_service_available: bool = False,
        driver: KeyringDriver | None = None,
        verify_backend: bool = True,
    ) -> None:
        self.selection = select_credential_backend(
            platform_name,
            headless=headless,
            secret_service_available=secret_service_available,
        )
        if self.selection.backend == "encrypted_file":
            raise BackendUnavailable("attended_encrypted_file_not_implemented")
        self._driver = driver or _load_keyring()
        if verify_backend:
            self._verify_backend()

    @property
    def backend(self) -> str:
        return self.selection.backend

    def store(self, provider: str, credential_ref: str, secret: str) -> None:
        provider_id = _provider_id(provider)
        _validate_reference(credential_ref)
        _validate_secret(secret)
        try:
            self._driver.set_password(self._service(provider_id), credential_ref, secret)
        except Exception as exc:
            raise NativeCredentialError("credential_store_failed") from exc

    def resolve(self, provider: str, credential_ref: str) -> str | None:
        provider_id = _provider_id(provider)
        _validate_reference(credential_ref)
        try:
            secret = self._driver.get_password(self._service(provider_id), credential_ref)
        except Exception as exc:
            raise NativeCredentialError("credential_resolve_failed") from exc
        if secret is None:
            return None
        _validate_secret(secret)
        return secret

    def contains(self, provider: str, credential_ref: str) -> bool:
        return self.resolve(provider, credential_ref) is not None

    def revoke(self, provider: str, credential_ref: str) -> bool:
        provider_id = _provider_id(provider)
        _validate_reference(credential_ref)
        if self.resolve(provider_id, credential_ref) is None:
            return False
        try:
            self._driver.delete_password(self._service(provider_id), credential_ref)
        except Exception as exc:
            raise NativeCredentialError("credential_revoke_failed") from exc
        return True

    def _verify_backend(self) -> None:
        expected = _EXPECTED_BACKEND_MODULE[self.selection.backend]
        try:
            backend = self._driver.get_keyring()
        except Exception as exc:
            raise BackendUnavailable("credential_backend_unavailable") from exc
        identity = f"{type(backend).__module__}.{type(backend).__qualname__}"
        priority = getattr(backend, "priority", 0)
        if expected not in identity or not isinstance(priority, (int, float)) or priority <= 0:
            raise BackendUnavailable("credential_backend_mismatch")

    @staticmethod
    def _service(provider_id: str) -> str:
        return f"torq-cli/{provider_id}"


class ConfiguredNativeVault:
    """Expose configured provider references through the connector Vault protocol."""

    def __init__(
        self,
        store: NativeCredentialStore,
        references: Mapping[str, str],
    ) -> None:
        self._store = store
        self._references = {
            _provider_id(provider): credential_ref
            for provider, credential_ref in references.items()
        }
        for credential_ref in self._references.values():
            _validate_reference(credential_ref)

    def get(self, provider: str) -> str | None:
        provider_id = _provider_id(provider)
        credential_ref = self._references.get(provider_id)
        if credential_ref is None:
            return None
        return self._store.resolve(provider_id, credential_ref)

    def __repr__(self) -> str:
        return "ConfiguredNativeVault()"


def native_store_for_current_platform() -> NativeCredentialStore:
    """Create the verified native backend for the current attended OS session."""
    platform_name = platform.system()
    is_linux = platform_name.casefold() == "linux"
    desktop_session = bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )
    return NativeCredentialStore(
        platform_name,
        headless=is_linux and not desktop_session,
        secret_service_available=is_linux and desktop_session,
    )


__all__ = [
    "ConfiguredNativeVault",
    "KeyringDriver",
    "NativeCredentialError",
    "NativeCredentialStore",
    "native_store_for_current_platform",
]
