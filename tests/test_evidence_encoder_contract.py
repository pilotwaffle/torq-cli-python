"""Pin the canonical encoder's own guarantees, independent of its callers.

`ReceiptChain._sanitize` rejects non-finite payloads before `_canonical` is ever
reached, so every existing test that exercises strictness kills the sanitizer's
guard and leaves the signing encoder's guard untested. These tests hold the
encoder to its contract directly: the bytes that get signed are conforming JSON,
key-ordered, compact, and ASCII-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    _canonical,
    _canonical_for_verification,
)


def _chain(tmp_path: Path, run_id: str = "run-encoder") -> ReceiptChain:
    root = tmp_path / "evidence"
    return ReceiptChain(
        root,
        run_id,
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_signing_encoder_refuses_non_finite_floats(value: float) -> None:
    # `NaN` and `Infinity` are not JSON. Signing them produces bytes no
    # conforming parser can read back, so the signature is unverifiable by
    # anyone who did not write it.
    with pytest.raises(ValueError):
        _canonical({"score": value})


def test_signing_encoder_escapes_non_ascii() -> None:
    # The digest is taken over these bytes. Encoding-dependent output would make
    # the chain hash depend on the writer's locale.
    assert _canonical({"note": "café"}) == b'{"note":"caf\\u00e9"}'


def test_signing_encoder_is_key_ordered_and_compact() -> None:
    assert _canonical({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_verification_encoder_still_reproduces_legacy_non_finite_bytes() -> None:
    # Receipts signed before the encoder tightened must stay verifiable: the
    # verification path reproduces the exact bytes that were signed.
    assert _canonical_for_verification({"score": float("nan")}) == b'{"score":NaN}'


def test_unserializable_payload_is_a_governed_finding(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-unserializable")

    # A payload the encoder cannot represent is a refusal like any other, not a
    # `TypeError` every caller has to special-case.
    with pytest.raises(ValueError, match="receipt_payload_unserializable"):
        chain.append("run_attested", {"roles": {"g1d", "builder"}})

    assert chain.sequence == 0
    assert not chain.receipts_path.exists()
