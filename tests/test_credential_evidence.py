from __future__ import annotations

import json

import pytest

from torq_cli.application.credential_evidence import exercise_native_credential


class _Store:
    backend = "test_backend"

    def __init__(self, *, mismatch: bool = False) -> None:
        self.value: str | None = None
        self.mismatch = mismatch

    def store(self, provider: str, credential_ref: str, secret: str) -> None:
        del provider, credential_ref
        self.value = secret

    def resolve(self, provider: str, credential_ref: str) -> str | None:
        del provider, credential_ref
        return "wrong" if self.mismatch and self.value is not None else self.value

    def revoke(self, provider: str, credential_ref: str) -> bool:
        del provider, credential_ref
        existed = self.value is not None
        self.value = None
        return existed


def test_native_credential_evidence_is_secret_free_and_revoked() -> None:
    store = _Store()
    report = exercise_native_credential(
        store,
        provider="deepseek",
        credential_ref="credref_" + "0" * 32,
        secret_factory=lambda: "ephemeral-test-secret",
    )

    assert report["operations"] == {
        "store": "passed",
        "resolve": "passed",
        "revoke": "passed",
        "absence_after_revoke": "passed",
    }
    assert report["secret_persisted"] is False
    assert store.value is None
    assert "ephemeral-test-secret" not in json.dumps(report)


def test_native_credential_evidence_revokes_after_mismatch() -> None:
    store = _Store(mismatch=True)
    with pytest.raises(RuntimeError, match="credential_round_trip_mismatch"):
        exercise_native_credential(
            store,
            provider="deepseek",
            credential_ref="credref_" + "0" * 32,
            secret_factory=lambda: "ephemeral-test-secret",
        )
    assert store.value is None
