from __future__ import annotations

from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from torq_cli.interfaces.cli import main
from torq_cli.safety.production_trust import (
    AnchorCapability,
    AnchorEvidence,
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

    def verify_readiness_signature(self, challenge: bytes, signature: bytes) -> bool:
        self.key.public_key().verify(signature, challenge)
        return True


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

    def capability(self) -> AnchorCapability:
        return self._capability

    def submit_readiness_probe(self, payload_digest: str) -> AnchorEvidence:
        return AnchorEvidence("test-record", payload_digest, b"proof", b"checkpoint")

    def verify_readiness_probe(self, evidence: AnchorEvidence) -> bool:
        return evidence.record_id == "test-record"


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
    signer.verify_readiness_signature = lambda challenge, signature: False  # type: ignore[method-assign]
    anchor = ProbeAnchor()
    anchor.verify_readiness_probe = lambda evidence: False  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor)
    assert report.status == "blocked"
    assert report.findings == (
        "production_signer_probe_failed",
        "production_anchor_probe_failed",
    )


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
    report = evaluate_production_trust(ProbeSigner(), ProbeAnchor())
    assert report.status == "ready"
    assert report.findings == ()


def test_adapter_capability_exceptions_are_reduced_to_stable_blocked_findings() -> None:
    signer = ProbeSigner()
    signer.capability = lambda: (_ for _ in ()).throw(RuntimeError("vendor secret"))  # type: ignore[method-assign]
    anchor = ProbeAnchor()
    anchor.capability = lambda: (_ for _ in ()).throw(RuntimeError("service secret"))  # type: ignore[method-assign]
    report = evaluate_production_trust(signer, anchor)
    assert report.status == "blocked"
    assert report.findings[0] == "production_signer_capability_unavailable"
    assert report.findings[1] == "production_anchor_capability_unavailable"
    assert "vendor secret" not in str(report.to_dict())
    assert "service secret" not in str(report.to_dict())


def test_unknown_signing_algorithm_cannot_report_ready() -> None:
    signer = ProbeSigner(replace(ProbeSigner().capability(), algorithm="md5-rsa"))
    report = evaluate_production_trust(signer, ProbeAnchor())
    assert report.status == "blocked"
    assert "production_signer_algorithm_unsupported" in report.findings


def test_cli_reports_current_local_residuals_and_exit_three(capsys) -> None:
    assert main(["trust", "readiness"]) == 3
    output = capsys.readouterr().out
    assert '"status": "blocked"' in output
    assert '"production_signing_identity_exportable"' in output
    assert '"production_receipt_anchor_not_independent"' in output
