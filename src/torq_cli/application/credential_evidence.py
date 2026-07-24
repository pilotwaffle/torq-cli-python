"""Secret-free native credential round-trip evidence."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any, Protocol


class CredentialRoundTripStore(Protocol):
    @property
    def backend(self) -> str: ...

    def store(self, provider: str, credential_ref: str, secret: str) -> None: ...

    def resolve(self, provider: str, credential_ref: str) -> str | None: ...

    def revoke(self, provider: str, credential_ref: str) -> bool: ...


def exercise_native_credential(
    store: CredentialRoundTripStore,
    *,
    provider: str,
    credential_ref: str,
    secret_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
) -> dict[str, Any]:
    """Store, resolve, revoke, and prove absence without returning the value."""
    secret = secret_factory()
    stored = False
    resolved = False
    revoked = False
    absent_after_revoke = False
    try:
        store.store(provider, credential_ref, secret)
        stored = True
        resolved = store.resolve(provider, credential_ref) == secret
        if not resolved:
            raise RuntimeError("credential_round_trip_mismatch")
    finally:
        if stored:
            revoked = store.revoke(provider, credential_ref)
            absent_after_revoke = store.resolve(provider, credential_ref) is None
        secret = ""
    if not revoked or not absent_after_revoke:
        raise RuntimeError("credential_revoke_verification_failed")
    return {
        "backend": store.backend,
        "operations": {
            "store": "passed",
            "resolve": "passed",
            "revoke": "passed",
            "absence_after_revoke": "passed",
        },
        "generated_ephemeral_value": True,
        "secret_persisted": False,
    }


__all__ = ["CredentialRoundTripStore", "exercise_native_credential"]
