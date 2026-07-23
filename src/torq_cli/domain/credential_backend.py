"""Credential-backend selection without credential access or environment discovery."""

from __future__ import annotations

from dataclasses import dataclass


class BackendUnavailable(RuntimeError):
    """Raised when the requested platform has no approved backend."""


@dataclass(frozen=True)
class BackendSelection:
    backend: str
    requires_attended_unlock: bool


def select_credential_backend(
    platform_name: str,
    *,
    headless: bool,
    secret_service_available: bool = False,
) -> BackendSelection:
    """Select an approved backend from explicit, already-attested platform facts."""
    normalized = platform_name.casefold()
    if normalized == "windows":
        return BackendSelection("windows_credential_manager", False)
    if normalized in {"darwin", "macos"}:
        return BackendSelection("macos_keychain", False)
    if normalized == "linux":
        if headless:
            return BackendSelection("encrypted_file", True)
        if secret_service_available:
            return BackendSelection("secret_service", False)
        raise BackendUnavailable("secret_service_unavailable")
    raise BackendUnavailable("platform_unsupported")
