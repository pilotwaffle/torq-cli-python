from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from torq_cli.application.artifact_extraction import (
    ArtifactExtractionError,
    MAX_EXTRACTED_BYTES,
    extract_supported_artifact,
)
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


@pytest.mark.parametrize(
    ("media_type", "source_name", "content", "extractor"),
    [
        (
            "image/png",
            "diagram.png",
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            + (32).to_bytes(4, "big")
            + (24).to_bytes(4, "big")
            + b"bounded-payload",
            "validated-png-envelope",
        ),
        (
            "image/jpeg",
            "photo.jpeg",
            b"\xff\xd8\xff\xe0bounded-payload\xff\xd9",
            "validated-jpeg-envelope",
        ),
        (
            "application/pdf",
            "brief.pdf",
            b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n",
            "validated-pdf-envelope",
        ),
    ],
)
def test_binary_context_is_signature_checked_and_preserved(
    media_type: str,
    source_name: str,
    content: bytes,
    extractor: str,
) -> None:
    extracted = extract_supported_artifact(
        content,
        media_type=media_type,
        source_name=source_name,
    )
    envelope = json.loads(extracted.text)

    assert extracted.extractor == extractor
    assert extracted.media_type == media_type
    assert base64.b64decode(envelope["content_base64"], validate=True) == content
    assert envelope["sha256"] == hashlib.sha256(content).hexdigest()
    assert envelope["source_name"] == source_name


@pytest.mark.parametrize(
    ("media_type", "source_name", "content"),
    [
        ("image/png", "fake.png", b"not-png"),
        ("image/jpeg", "fake.jpg", b"not-jpeg"),
        ("application/pdf", "fake.pdf", b"not-pdf"),
    ],
)
def test_binary_context_rejects_extension_only_spoofs(
    media_type: str,
    source_name: str,
    content: bytes,
) -> None:
    with pytest.raises(ArtifactExtractionError, match="artifact_signature_mismatch"):
        extract_supported_artifact(
            content,
            media_type=media_type,
            source_name=source_name,
        )


def _jpeg(size: int) -> bytes:
    assert size >= 5
    return b"\xff\xd8\xff" + (b"x" * (size - 5)) + b"\xff\xd9"


def test_binary_envelope_size_is_bounded_before_persistence() -> None:
    accepted = extract_supported_artifact(
        _jpeg(700 * 1024),
        media_type="image/jpeg",
        source_name="at-client-limit.jpg",
    )
    assert accepted.extracted_bytes <= MAX_EXTRACTED_BYTES

    with pytest.raises(ArtifactExtractionError, match="artifact_extracted_too_large"):
        extract_supported_artifact(
            _jpeg(800 * 1024),
            media_type="image/jpeg",
            source_name="over-envelope-limit.jpg",
        )


def test_oversized_binary_envelope_leaves_no_orphan_artifact(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    chain = ReceiptChain(
        evidence,
        "run-binary-envelope-refused",
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )

    with pytest.raises(ArtifactExtractionError, match="artifact_extracted_too_large"):
        GovernedOrchestrator().inject_artifact(
            chain,
            _jpeg(800 * 1024),
            media_type="image/jpeg",
            source_name="over-envelope-limit.jpg",
        )

    assert not (chain.root / "artifacts").exists()
    rows = [json.loads(line) for line in chain.receipts_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["transition"] == "command_rejected"
    assert rows[0]["payload"]["finding"] == "artifact_extracted_too_large"
