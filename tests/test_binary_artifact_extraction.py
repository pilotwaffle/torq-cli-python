from __future__ import annotations

import base64
import hashlib
import json

import pytest

from torq_cli.application.artifact_extraction import (
    ArtifactExtractionError,
    extract_supported_artifact,
)


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
