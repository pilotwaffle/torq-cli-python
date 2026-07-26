"""Fail-closed production trust readiness contract.

Local ACLs make accidental disclosure harder, but they cannot make a private
key non-exportable and a local anchor cannot detect a same-volume rollback.
This module keeps those properties distinct and requires active adapter probes
before a deployment may describe the evidence path as production hardened.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol


SignerIsolation = Literal["local_filesystem", "os_isolated", "hardware"]
AnchorScope = Literal["local_same_volume", "remote_transparency"]


@dataclass(frozen=True)
class SignerCapability:
    """Security properties asserted by a trusted platform adapter."""

    backend: str
    algorithm: str
    key_id: str
    key_origin: Literal["imported", "generated_in_backend"]
    isolation: SignerIsolation
    private_key_exportable: bool
    receipt_path_integrated: bool


@dataclass(frozen=True)
class AnchorCapability:
    """Security properties asserted by a trusted anchoring adapter."""

    backend: str
    scope: AnchorScope
    append_only: bool
    independently_operated: bool
    inclusion_proof_supported: bool
    checkpoint_supported: bool
    receipt_path_integrated: bool


@dataclass(frozen=True)
class AnchorEvidence:
    """Opaque evidence returned by one readiness probe."""

    record_id: str
    payload_digest: str
    inclusion_proof: bytes
    checkpoint: bytes
    checkpoint_issued_at: int


class ProductionSigner(Protocol):
    """Adapter boundary for a non-exportable signing identity."""

    def capability(self) -> SignerCapability: ...

    def sign_readiness_challenge(self, challenge: bytes) -> bytes: ...


class ReceiptAnchor(Protocol):
    """Adapter boundary for an independently stored append-only anchor."""

    def capability(self) -> AnchorCapability: ...

    def submit_readiness_probe(self, payload_digest: str) -> AnchorEvidence: ...


class ProductionTrustVerifier(Protocol):
    """Trust policy kept independent from both probed adapters.

    Concrete implementations pin the permitted platform backend/key identity
    and remote checkpoint trust root outside the run volume. The checkpoint
    verifier must authenticate every :class:`AnchorEvidence` field, including
    ``payload_digest`` and ``checkpoint_issued_at``.
    """

    def verify_signer_capability(
        self, signer: ProductionSigner, capability: SignerCapability
    ) -> bool: ...

    def verify_signer_signature(
        self,
        capability: SignerCapability,
        challenge: bytes,
        signature: bytes,
    ) -> bool: ...

    def verify_anchor_capability(
        self, anchor: ReceiptAnchor, capability: AnchorCapability
    ) -> bool: ...

    def verify_anchor_evidence(
        self, expected_digest: str, evidence: AnchorEvidence
    ) -> bool: ...


@dataclass(frozen=True)
class ProductionTrustReadiness:
    status: Literal["ready", "blocked"]
    findings: tuple[str, ...]
    signer: SignerCapability
    anchor: AnchorCapability

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "torq-production-trust-readiness-v1",
            "status": self.status,
            "findings": list(self.findings),
            "signer": asdict(self.signer),
            "anchor": asdict(self.anchor),
        }


_LOCAL_SIGNER = SignerCapability(
    backend="file-run-key-store",
    algorithm="ed25519",
    key_id="not-probed",
    key_origin="generated_in_backend",
    isolation="local_filesystem",
    private_key_exportable=True,
    receipt_path_integrated=True,
)
_LOCAL_ANCHOR = AnchorCapability(
    backend="same-volume-manifest-anchor",
    scope="local_same_volume",
    append_only=False,
    independently_operated=False,
    inclusion_proof_supported=False,
    checkpoint_supported=False,
    receipt_path_integrated=True,
)

_PUBLIC_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_SIGNER_CHALLENGE_DOMAIN = b"TORQ-PRODUCTION-TRUST-SIGNER-V1\x00"
_ANCHOR_PROBE_DOMAIN = b"TORQ-PRODUCTION-TRUST-ANCHOR-V1\x00"
_MAX_CHECKPOINT_AGE_SECONDS = 300
_MAX_CHECKPOINT_FUTURE_SKEW_SECONDS = 30
_MAX_PROOF_BYTES = 1_048_576
_MAX_SIGNATURE_BYTES = 4096


def _valid_public_label(value: object) -> bool:
    return isinstance(value, str) and bool(_PUBLIC_LABEL.fullmatch(value))


def _valid_signer_capability(capability: SignerCapability) -> bool:
    return (
        _valid_public_label(capability.backend)
        and _valid_public_label(capability.algorithm)
        and _valid_public_label(capability.key_id)
        and capability.key_origin in {"imported", "generated_in_backend"}
        and capability.isolation in {"local_filesystem", "os_isolated", "hardware"}
        and type(capability.private_key_exportable) is bool
        and type(capability.receipt_path_integrated) is bool
    )


def _valid_anchor_capability(capability: AnchorCapability) -> bool:
    return (
        _valid_public_label(capability.backend)
        and capability.scope in {"local_same_volume", "remote_transparency"}
        and type(capability.append_only) is bool
        and type(capability.independently_operated) is bool
        and type(capability.inclusion_proof_supported) is bool
        and type(capability.checkpoint_supported) is bool
        and type(capability.receipt_path_integrated) is bool
    )


def _signer_challenge(capability: SignerCapability) -> bytes:
    binding = json.dumps(
        asdict(capability),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return (
        _SIGNER_CHALLENGE_DOMAIN
        + hashlib.sha256(binding).digest()
        + secrets.token_bytes(32)
    )


def _fresh_anchor_digest() -> str:
    payload = _ANCHOR_PROBE_DOMAIN + secrets.token_bytes(32)
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _checkpoint_is_fresh(evidence: AnchorEvidence, now: int) -> bool:
    issued_at = evidence.checkpoint_issued_at
    if isinstance(issued_at, bool) or not isinstance(issued_at, int):
        return False
    return (
        now - _MAX_CHECKPOINT_AGE_SECONDS
        <= issued_at
        <= now + _MAX_CHECKPOINT_FUTURE_SKEW_SECONDS
    )


def _valid_anchor_evidence(evidence: object, expected_digest: str, now: int) -> bool:
    return (
        isinstance(evidence, AnchorEvidence)
        and _valid_public_label(evidence.record_id)
        and evidence.payload_digest == expected_digest
        and isinstance(evidence.inclusion_proof, bytes)
        and 0 < len(evidence.inclusion_proof) <= _MAX_PROOF_BYTES
        and isinstance(evidence.checkpoint, bytes)
        and 0 < len(evidence.checkpoint) <= _MAX_PROOF_BYTES
        and _checkpoint_is_fresh(evidence, now)
    )


def evaluate_production_trust(
    signer: ProductionSigner | None = None,
    anchor: ReceiptAnchor | None = None,
    verifier: ProductionTrustVerifier | None = None,
) -> ProductionTrustReadiness:
    """Actively probe production adapters and return a stable fail-closed result.

    Passing capability metadata alone is insufficient. A ready result requires
    a separately configured trust verifier, a domain-separated fresh signer
    challenge, and a fresh remote anchor inclusion/checkpoint proof.
    Adapter exceptions are deliberately reduced to stable findings so details
    from an HSM, keychain, or remote service do not escape at the CLI boundary.
    """

    findings: list[str] = []
    try:
        signer_candidate = _LOCAL_SIGNER if signer is None else signer.capability()
        if not isinstance(signer_candidate, SignerCapability):
            raise TypeError("invalid signer capability")
        signer_capability = signer_candidate
    except Exception:
        signer_capability = _LOCAL_SIGNER
        findings.append("production_signer_capability_unavailable")
    try:
        anchor_candidate = _LOCAL_ANCHOR if anchor is None else anchor.capability()
        if not isinstance(anchor_candidate, AnchorCapability):
            raise TypeError("invalid anchor capability")
        anchor_capability = anchor_candidate
    except Exception:
        anchor_capability = _LOCAL_ANCHOR
        findings.append("production_anchor_capability_unavailable")

    if not _valid_signer_capability(signer_capability):
        findings.append("production_signer_capability_invalid")
        signer_capability = _LOCAL_SIGNER
    if signer_capability.algorithm not in {"ed25519", "ecdsa-p256-sha256"}:
        findings.append("production_signer_algorithm_unsupported")
    signer_metadata_ready = not (
        signer_capability.private_key_exportable
        or signer_capability.key_origin != "generated_in_backend"
        or signer_capability.isolation not in {"os_isolated", "hardware"}
    )
    if not signer_metadata_ready:
        findings.append("production_signing_identity_exportable")
    if not signer_capability.receipt_path_integrated:
        findings.append("production_signer_not_integrated")

    if (signer is not None or anchor is not None) and verifier is None:
        findings.append("production_trust_verifier_unavailable")

    signer_trusted = False
    if signer is not None and verifier is not None:
        try:
            signer_trusted = (
                verifier.verify_signer_capability(signer, signer_capability) is True
            )
        except Exception:
            signer_trusted = False
        if not signer_trusted:
            findings.append("production_signer_identity_untrusted")

    if (
        signer is not None
        and verifier is not None
        and signer_trusted
        and signer_metadata_ready
        and signer_capability.receipt_path_integrated
        and signer_capability.algorithm in {"ed25519", "ecdsa-p256-sha256"}
    ):
        challenge = _signer_challenge(signer_capability)
        try:
            signature = signer.sign_readiness_challenge(challenge)
            signer_probe_ok = (
                isinstance(signature, bytes)
                and 0 < len(signature) <= _MAX_SIGNATURE_BYTES
                and verifier.verify_signer_signature(
                    signer_capability, challenge, signature
                )
                is True
            )
        except Exception:
            signer_probe_ok = False
        if not signer_probe_ok:
            findings.append("production_signer_probe_failed")

    if not _valid_anchor_capability(anchor_capability):
        findings.append("production_anchor_capability_invalid")
        anchor_capability = _LOCAL_ANCHOR

    anchor_metadata_ready = (
        _valid_public_label(anchor_capability.backend)
        and anchor_capability.scope == "remote_transparency"
        and anchor_capability.append_only
        and anchor_capability.independently_operated
        and anchor_capability.inclusion_proof_supported
        and anchor_capability.checkpoint_supported
    )
    if not anchor_metadata_ready:
        findings.append("production_receipt_anchor_not_independent")
    if not anchor_capability.receipt_path_integrated:
        findings.append("production_anchor_not_integrated")

    anchor_trusted = False
    if anchor is not None and verifier is not None:
        try:
            anchor_trusted = (
                verifier.verify_anchor_capability(anchor, anchor_capability) is True
            )
        except Exception:
            anchor_trusted = False
        if not anchor_trusted:
            findings.append("production_anchor_identity_untrusted")

    if (
        anchor is not None
        and verifier is not None
        and anchor_trusted
        and anchor_metadata_ready
        and anchor_capability.receipt_path_integrated
    ):
        digest = _fresh_anchor_digest()
        try:
            evidence = anchor.submit_readiness_probe(digest)
            anchor_probe_ok = (
                _valid_anchor_evidence(evidence, digest, int(time.time()))
                and verifier.verify_anchor_evidence(digest, evidence)
                is True
            )
        except Exception:
            anchor_probe_ok = False
        if not anchor_probe_ok:
            findings.append("production_anchor_probe_failed")

    unique_findings = tuple(dict.fromkeys(findings))
    return ProductionTrustReadiness(
        status="blocked" if unique_findings else "ready",
        findings=unique_findings,
        signer=signer_capability,
        anchor=anchor_capability,
    )
