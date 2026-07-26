from __future__ import annotations

import time
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from torq_cli.interfaces.cli import main
from torq_cli.safety.production_trust import (
    AnchorCapability,
    AnchorEvidence,
    ProductionTrustVerifier,
    SignerCapability,
    evaluate_production_trust,
)


class ProbeSigner:
    def __init__(self, capability: SignerCapability | None = None) -> None:
        self.key = Ed25519PrivateKey.generate()
        self._capability = capability or SignerCapability(
            backend="test-platform-signer",
            algorithm="ed25519",
            key_id="test-key",
            key_origin="generated_in_backend",
            isolation="hardware",
            private_key_exportable=False,
            receipt_path_integrated=True,
        )

    def capability(self) -> SignerCapability:
        return self._capability

    def sign_readiness_challenge(self, challenge: bytes) -> bytes:
        return self.key.sign(challenge)


class ProbeAnchor:
    def __init__(self, capability: AnchorCapability | None = None) -> None:
        self._capability = capability or AnchorCapability(
            backend="test-transparency-log",
            scope="remote_transparency",
            append_only=True,
            independently_operated=True,
            inclusion_proof_supported=True,
            checkpoint_supported=True,
            receipt_path_integrated=True,
        )
        self.key = Ed25519PrivateKey.generate()
        self.last_digest: str | None = None

    def capability(self) -> AnchorCapability:
        return self._capability

    def submit_readiness_probe(self, payload_digest: str) -> AnchorEvidence:
        self.last_digest = payload_digest
        issued_at = int(time.time())
        checkpoint = self.key.sign(
            _checkpoint_message("test-record", payload_digest, b"proof", issued_at)
        )
        return AnchorEvidence(
            "test-record", payload_digest, b"proof", checkpoint, issued_at
        )


def _checkpoint_message(
    record_id: str, payload_digest: str, proof: bytes, issued_at: int
) -> bytes:
    return b"\x00".join(
        (
            b"TORQ-TEST-CHECKPOINT-V1",
            record_id.encode("ascii"),
            payload_digest.encode("ascii"),
            proof,
            str(issued_at).encode("ascii"),
        )
    )


class ProbeVerifier(ProductionTrustVerifier):
    def __init__(self, signer: ProbeSigner, anchor: ProbeAnchor) -> None:
        self.signer = signer
        self.anchor = anchor

    def verify_signer_capability(
        self, signer: ProbeSigner, capability: SignerCapability
    ) -> bool:
        return signer is self.signer and capability == self.signer.capability()

    def verify_signer_signature(
        self,
        capability: SignerCapability,
        challenge: bytes,
        signature: bytes,
    ) -> bool:
        try:
            self.signer.key.public_key().verify(signature, challenge)
        except InvalidSignature:
            return False
        return True

    def verify_anchor_capability(
        self, anchor: ProbeAnchor, capability: AnchorCapability
    ) -> bool:
        return anchor is self.anchor and capability == self.anchor.capability()

    def verify_anchor_evidence(
        self, expected_digest: str, evidence: AnchorEvidence
    ) -> bool:
        try:
            self.anchor.key.public_key().verify(
                evidence.checkpoint,
                _checkpoint_message(
                    evidence.record_id,
                    evidence.payload_digest,
                    evidence.inclusion_proof,
                    evidence.checkpoint_issued_at,
                ),
            )
        except InvalidSignature:
            return False
        return evidence.payload_digest == expected_digest


def test_default_readiness_fails_closed_and_names_both_residuals() -> None:
    report = evaluate_production_trust()
    assert report.status == "blocked"
    assert report.findings == (
        "production_signing_identity_exportable",
        "production_receipt_anchor_not_independent",
    )
    assert report.signer.private_key_exportable is True
    assert report.anchor.scope == "local_same_volume"


def test_capability_metadata_without_working_probes_cannot_report_ready() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    report = evaluate_production_trust(signer, anchor)
    assert report.status == "blocked"
    assert report.findings == ("production_trust_verifier_unavailable",)


def test_exportable_or_same_volume_adapters_cannot_report_ready() -> None:
    signer = ProbeSigner(
        replace(ProbeSigner().capability(), private_key_exportable=True)
    )
    anchor = ProbeAnchor(replace(ProbeAnchor().capability(), scope="local_same_volume"))
    report = evaluate_production_trust(signer, anchor)
    assert report.status == "blocked"
    assert "production_signing_identity_exportable" in report.findings
    assert "production_receipt_anchor_not_independent" in report.findings


def test_ready_requires_integrated_adapters_and_successful_active_probes() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    report = evaluate_production_trust(signer, anchor, ProbeVerifier(signer, anchor))
    assert report.status == "ready"
    assert report.findings == ()


def test_adapter_capability_exceptions_are_reduced_to_stable_blocked_findings() -> None:
    signer = ProbeSigner()
    signer.capability = lambda: (_ for _ in ()).throw(RuntimeError("vendor secret"))  # type: ignore[method-assign]
    anchor = ProbeAnchor()
    anchor.capability = lambda: (_ for _ in ()).throw(RuntimeError("service secret"))  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor, ProbeVerifier(signer, anchor))
    assert report.status == "blocked"
    assert report.findings[0] == "production_signer_capability_unavailable"
    assert report.findings[1] == "production_anchor_capability_unavailable"
    assert "vendor secret" not in str(report.to_dict())
    assert "service secret" not in str(report.to_dict())


def test_unknown_signing_algorithm_cannot_report_ready() -> None:
    signer = ProbeSigner(replace(ProbeSigner().capability(), algorithm="md5-rsa"))
    anchor = ProbeAnchor()
    report = evaluate_production_trust(signer, anchor, ProbeVerifier(signer, anchor))
    assert report.status == "blocked"
    assert "production_signer_algorithm_unsupported" in report.findings


def test_signer_cannot_self_attest_with_declared_labels() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    signer.verify_signer_signature = lambda *args: True  # type: ignore[attr-defined]
    anchor.verify_anchor_evidence = lambda *args: True  # type: ignore[attr-defined]
    report = evaluate_production_trust(signer, anchor)
    assert report.status == "blocked"
    assert report.findings == ("production_trust_verifier_unavailable",)


def test_replayed_anchor_evidence_is_rejected() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    verifier = ProbeVerifier(signer, anchor)
    replay = anchor.submit_readiness_probe("sha256:" + "0" * 64)
    anchor.submit_readiness_probe = lambda digest: replay  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor, verifier)
    assert report.status == "blocked"
    assert "production_anchor_probe_failed" in report.findings


def test_stale_or_future_checkpoint_is_rejected() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    verifier = ProbeVerifier(signer, anchor)

    def stale_evidence(digest: str) -> AnchorEvidence:
        issued_at = int(time.time()) - 301
        proof = b"proof"
        signature = anchor.key.sign(
            _checkpoint_message("stale-record", digest, proof, issued_at)
        )
        return AnchorEvidence("stale-record", digest, proof, signature, issued_at)

    anchor.submit_readiness_probe = stale_evidence  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor, verifier)
    assert report.status == "blocked"
    assert "production_anchor_probe_failed" in report.findings


def test_signer_probe_is_domain_separated_and_capability_bound() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    observed: list[bytes] = []
    original = signer.sign_readiness_challenge

    def capture(challenge: bytes) -> bytes:
        observed.append(challenge)
        return original(challenge)

    signer.sign_readiness_challenge = capture  # type: ignore[method-assign]
    assert evaluate_production_trust(
        signer, anchor, ProbeVerifier(signer, anchor)
    ).status == "ready"
    assert observed[0].startswith(b"TORQ-PRODUCTION-TRUST-SIGNER-V1\x00")
    assert len(observed[0]) == len(b"TORQ-PRODUCTION-TRUST-SIGNER-V1\x00") + 64


@pytest.mark.parametrize("issued_at", [True, "0", 1.5])
def test_non_integer_checkpoint_timestamp_is_rejected(issued_at: object) -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    verifier = ProbeVerifier(signer, anchor)

    def malformed(digest: str) -> AnchorEvidence:
        return AnchorEvidence("record", digest, b"proof", b"signature", issued_at)  # type: ignore[arg-type]

    anchor.submit_readiness_probe = malformed  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor, verifier)
    assert report.status == "blocked"
    assert "production_anchor_probe_failed" in report.findings


def test_runtime_capability_types_are_strict_and_invalid_values_are_redacted() -> None:
    signer = ProbeSigner(
        replace(
            ProbeSigner().capability(),
            backend=b"secret",  # type: ignore[arg-type]
            private_key_exportable=0,  # type: ignore[arg-type]
        )
    )
    anchor = ProbeAnchor(
        replace(
            ProbeAnchor().capability(),
            append_only="yes",  # type: ignore[arg-type]
        )
    )
    report = evaluate_production_trust(signer, anchor, ProbeVerifier(signer, anchor))
    assert report.status == "blocked"
    assert "production_signer_capability_invalid" in report.findings
    assert "production_anchor_capability_invalid" in report.findings
    assert report.signer.backend == "file-run-key-store"
    assert report.anchor.backend == "same-volume-manifest-anchor"


def test_oversized_checkpoint_is_rejected_before_external_verification() -> None:
    signer = ProbeSigner()
    anchor = ProbeAnchor()
    verifier = ProbeVerifier(signer, anchor)
    verifier.verify_anchor_evidence = lambda *args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("unbounded evidence reached verifier")
    )

    def oversized(digest: str) -> AnchorEvidence:
        return AnchorEvidence(
            "record", digest, b"proof", b"x" * 1_048_577, int(time.time())
        )

    anchor.submit_readiness_probe = oversized  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor, verifier)
    assert report.status == "blocked"
    assert "production_anchor_probe_failed" in report.findings


def test_cli_reports_current_local_residuals_and_exit_three(capsys) -> None:
    assert main(["trust", "readiness"]) == 3
    output = capsys.readouterr().out
    assert '"status": "blocked"' in output
    assert '"production_signing_identity_exportable"' in output
    assert '"production_receipt_anchor_not_independent"' in output
