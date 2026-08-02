from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.application.artifact_extraction import (
    MAX_ARTIFACT_BYTES,
    ArtifactExtractionError,
    extract_supported_artifact,
)
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.core.redaction import PatternRegistry, RedactionBlocked
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


@pytest.mark.parametrize(
    ("name", "media_type", "content", "extractor"),
    [
        ("note.txt", "text/plain", b"plain context", "strict-utf8"),
        ("note.md", "text/markdown", b"# constraint", "strict-utf8"),
        (
            "note.json",
            "application/json; charset=utf-8",
            b'{"z":2,"a":"value"}',
            "strict-json-utf8",
        ),
    ],
)
def test_supported_artifacts_use_the_pinned_extractor_contract(
    name: str,
    media_type: str,
    content: bytes,
    extractor: str,
) -> None:
    result = extract_supported_artifact(
        content,
        source_name=name,
        media_type=media_type,
    )

    assert result.extractor == extractor
    assert result.evidence()["contract_version"] == "1.1.0"
    if name.endswith(".json"):
        assert result.text == '{"a":"value","z":2}'


@pytest.mark.parametrize(
    ("content", "name", "media_type", "finding"),
    [
        (b"", "note.txt", "text/plain", "artifact_empty"),
        (b"x", "../note.txt", "text/plain", "artifact_source_name_invalid"),
        (b"x", "C:note.txt", "text/plain", "artifact_source_name_invalid"),
        (b"x", "note.txt ", "text/plain", "artifact_source_name_invalid"),
        (b"x", "txt", "text/plain", "artifact_extension_mismatch"),
        (b"x", "md", "text/markdown", "artifact_extension_mismatch"),
        (b"{}", "json", "application/json", "artifact_extension_mismatch"),
        (b"x", "note.json.txt", "application/json", "artifact_extension_mismatch"),
        (b"x", "note.html", "text/html", "artifact_media_type_unsupported"),
        (b"%PDF-body", "note.txt", "text/plain", "artifact_binary_signature_denied"),
        (b"PK\x03\x04body", "note.txt", "text/plain", "artifact_binary_signature_denied"),
        (b"\xef\xbb\xbftext", "note.txt", "text/plain", "artifact_bom_forbidden"),
        (b"bad\x00text", "note.txt", "text/plain", "artifact_binary_content_unsupported"),
        (b"\xff", "note.txt", "text/plain", "artifact_utf8_required"),
        (b'{"a":1,"a":2}', "note.json", "application/json", "artifact_json_duplicate_key"),
        (b'{"a":NaN}', "note.json", "application/json", "artifact_json_non_finite"),
        (b'{"a":', "note.json", "application/json", "artifact_structure_invalid"),
    ],
)
def test_artifact_contract_fails_closed(
    content: bytes,
    name: str,
    media_type: str,
    finding: str,
) -> None:
    with pytest.raises(ArtifactExtractionError, match=finding):
        extract_supported_artifact(content, source_name=name, media_type=media_type)


def test_artifact_size_and_json_complexity_are_bounded() -> None:
    assert extract_supported_artifact(
        b"x" * MAX_ARTIFACT_BYTES,
        source_name="limit.txt",
        media_type="text/plain",
    ).source_bytes == MAX_ARTIFACT_BYTES
    with pytest.raises(ArtifactExtractionError, match="artifact_too_large"):
        extract_supported_artifact(
            b"x" * (MAX_ARTIFACT_BYTES + 1),
            source_name="limit.txt",
            media_type="text/plain",
        )
    value = "0"
    for _ in range(65):
        value = "[" + value + "]"
    with pytest.raises(ArtifactExtractionError, match="artifact_json_complexity_exceeded"):
        extract_supported_artifact(
            value.encode(),
            source_name="deep.json",
            media_type="application/json",
        )


def test_provider_environment_assignments_and_key_shapes_are_governed() -> None:
    registry = PatternRegistry.default()
    clean, findings = registry.scan(
        "QWEN_TOKEN_PLAN_API_KEY=abcdefghijklmnopqrstuv\n"
        "ANTHROPIC_AUTH_TOKEN=zyxwvutsrqponmlkjihgfe"
    )
    assert "abcdefghijklmnopqrstuv" not in clean
    assert "zyxwvutsrqponmlkjihgfe" not in clean
    assert findings == ("KEY_ASSIGNMENT",)
    clean, findings = registry.scan(
        "api-key='abcdefghijklmnop'\n"
        "secret-key=ponmlkjihgfedcba\n"
        "apikey=abcdefgh12345678"
    )
    assert "abcdefghijklmnop" not in clean
    assert "ponmlkjihgfedcba" not in clean
    assert "abcdefgh12345678" not in clean
    assert findings == ("KEY_ASSIGNMENT",)
    for value in (
        "sk-proj-" + "A" * 24,
        "sk-ant-" + "B" * 24,
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "ASIA" + "C" * 16,
    ):
        with pytest.raises(RedactionBlocked):
            registry.scan(value)


def test_rejected_file_is_receipted_without_writing_an_artifact(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    chain = ReceiptChain(
        evidence,
        "run-file-rejected",
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )

    with pytest.raises(ArtifactExtractionError, match="artifact_binary_signature_denied"):
        GovernedOrchestrator().inject_artifact(
            chain,
            b"%PDF-not-supported",
            media_type="text/plain",
            source_name="spoof.txt",
        )

    rows = [json.loads(line) for line in chain.receipts_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["transition"] == "command_rejected"
    assert rows[0]["payload"]["finding"] == "artifact_binary_signature_denied"
    assert not (chain.root / "artifacts").exists()


def test_invalid_inline_source_name_is_durably_rejected(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    chain = ReceiptChain(
        evidence,
        "run-inline-source-rejected",
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )

    with pytest.raises(ArtifactExtractionError, match="artifact_source_name_invalid"):
        GovernedOrchestrator().inject_context(
            chain,
            "safe content",
            source_name="../note.txt",
        )

    rows = [json.loads(line) for line in chain.receipts_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["transition"] == "command_rejected"
    assert rows[0]["payload"]["finding"] == "artifact_source_name_invalid"
    assert rows[0]["payload"]["source_name"] is None


def test_artifact_storage_rejects_escape_names_and_collisions(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence"
    chain = ReceiptChain(
        evidence,
        "run-artifact-name",
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )

    with pytest.raises(ValueError, match="artifact_name_invalid"):
        chain.write_artifact("../escape", "content")
    chain.write_artifact("safe-name", "content")
    with pytest.raises(ValueError, match="artifact_name_collision"):
        chain.write_artifact("safe-name", "replacement")
