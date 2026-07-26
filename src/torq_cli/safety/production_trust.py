"""Fail-closed production trust readiness contract.

Local ACLs make accidental disclosure harder, but they cannot make a private
key non-exportable and a local anchor cannot detect a same-volume rollback.
This module keeps those properties distinct and requires active adapter probes
before a deployment may describe the evidence path as production hardened.
"""

from __future__ import annotations

import hashlib
import secrets
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


class ProductionSigner(Protocol):
    """Adapter boundary for a non-exportable signing identity."""

    def capability(self) -> SignerCapability: ...

    def sign_readiness_challenge(self, challenge: bytes) -> bytes: ...

    def verify_readiness_signature(self, challenge: bytes, signature: bytes) -> bool: ...


class ReceiptAnchor(Protocol):
    """Adapter boundary for an independently stored append-only anchor."""

    def capability(self) -> AnchorCapability: ...

    def submit_readiness_probe(self, payload_digest: str) -> AnchorEvidence: ...

    def verify_readiness_probe(self, evidence: AnchorEvidence) -> bool: ...


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


def evaluate_production_trust(
    signer: ProductionSigner | None = None,
    anchor: ReceiptAnchor | None = None,
) -> ProductionTrustReadiness:
    """Actively probe production adapters and return a stable fail-closed result.

    Passing capability metadata alone is insufficient. A ready result requires
    a fresh signer challenge and a remote anchor inclusion/checkpoint proof.
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

    if (
        not signer_capability.backend
        or not signer_capability.algorithm
        or not signer_capability.key_id
    ):
        findings.append("production_signer_capability_invalid")
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

    if (
        signer is not None
        and signer_metadata_ready
        and signer_capability.receipt_path_integrated
        and signer_capability.algorithm in {"ed25519", "ecdsa-p256-sha256"}
    ):
        challenge = secrets.token_bytes(32)
        try:
            signature = signer.sign_readiness_challenge(challenge)
            signer_probe_ok = (
                isinstance(signature, bytes)
                and bool(signature)
                and signer.verify_readiness_signature(challenge, signature)
            )
        except Exception:
            signer_probe_ok = False
        if not signer_probe_ok:
            findings.append("production_signer_probe_failed")

    anchor_metadata_ready = (
        bool(anchor_capability.backend)
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

    if anchor is not None and anchor_metadata_ready and anchor_capability.receipt_path_integrated:
        digest = "sha256:" + hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        try:
            evidence = anchor.submit_readiness_probe(digest)
            anchor_probe_ok = (
                evidence.payload_digest == digest
                and bool(evidence.record_id)
                and bool(evidence.inclusion_proof)
                and bool(evidence.checkpoint)
                and anchor.verify_readiness_probe(evidence)
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
