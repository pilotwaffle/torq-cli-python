"""Closed extraction contract for operator-supplied context artifacts."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

EXTRACTION_CONTRACT_VERSION = "1.0.0"
MAX_ARTIFACT_BYTES = 1_048_576

_SUPPORTED_MEDIA_TYPES = frozenset({
    "application/json",
    "text/markdown",
    "text/plain",
})
_EXTENSIONS = {
    "application/json": frozenset({".json"}),
    "text/markdown": frozenset({".md", ".markdown"}),
    "text/plain": frozenset({".txt"}),
}
_SOURCE_NAME = re.compile(r"^[^\x00-\x1f\x7f/:\\]{1,255}$")
_BINARY_SIGNATURES = (
    b"%PDF-",
    b"PK\x03\x04",
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"MZ",
    b"\x7fELF",
    b"\x1f\x8b",
)


class ArtifactExtractionError(ValueError):
    """A stable refusal emitted before an artifact can enter a prompt."""


@dataclass(frozen=True)
class ExtractedArtifact:
    text: str
    media_type: str
    source_name: str
    source_bytes: int
    extracted_bytes: int
    extractor: str

    def evidence(self) -> dict[str, str | int]:
        return {
            "contract_version": EXTRACTION_CONTRACT_VERSION,
            "extractor": self.extractor,
            "source_bytes": self.source_bytes,
            "extracted_bytes": self.extracted_bytes,
        }


def extract_supported_artifact(
    content: bytes,
    *,
    media_type: str,
    source_name: str,
) -> ExtractedArtifact:
    """Decode and validate one bounded, explicitly supported textual artifact."""
    if not content:
        raise ArtifactExtractionError("artifact_empty")
    if len(content) > MAX_ARTIFACT_BYTES:
        raise ArtifactExtractionError("artifact_too_large")
    normalized_name = validate_source_label(source_name)
    normalized_type = _normalize_media_type(media_type)
    if normalized_type not in _SUPPORTED_MEDIA_TYPES:
        raise ArtifactExtractionError("artifact_media_type_unsupported")
    if "." not in normalized_name:
        raise ArtifactExtractionError("artifact_extension_mismatch")
    extension = "." + normalized_name.rsplit(".", 1)[-1].casefold()
    if extension not in _EXTENSIONS[normalized_type]:
        raise ArtifactExtractionError("artifact_extension_mismatch")
    if content.startswith(_BINARY_SIGNATURES):
        raise ArtifactExtractionError("artifact_binary_signature_denied")
    if b"\x00" in content:
        raise ArtifactExtractionError("artifact_binary_content_unsupported")
    if content.startswith(b"\xef\xbb\xbf"):
        raise ArtifactExtractionError("artifact_bom_forbidden")
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ArtifactExtractionError("artifact_utf8_required") from exc
    if not text.strip():
        raise ArtifactExtractionError("artifact_empty")

    extractor = "strict-utf8"
    try:
        if normalized_type == "application/json":
            value = json.loads(
                text,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
            _bound_json_tree(value)
            text = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            extractor = "strict-json-utf8"
    except ArtifactExtractionError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactExtractionError("artifact_structure_invalid") from exc

    return ExtractedArtifact(
        text=text,
        media_type=normalized_type,
        source_name=normalized_name,
        source_bytes=len(content),
        extracted_bytes=len(text.encode("utf-8")),
        extractor=extractor,
    )


def _normalize_media_type(value: str) -> str:
    pieces = [piece.strip().casefold() for piece in value.split(";")]
    if len(pieces) == 1:
        return pieces[0]
    if len(pieces) == 2 and pieces[1] in {"charset=utf-8", 'charset="utf-8"'}:
        return pieces[0]
    raise ArtifactExtractionError("artifact_media_type_unsupported")


def validate_source_label(source_name: str) -> str:
    normalized_name = unicodedata.normalize("NFC", source_name)
    if (
        not _SOURCE_NAME.fullmatch(normalized_name)
        or normalized_name != source_name
        or normalized_name.endswith((".", " "))
        or normalized_name.startswith(".")
        or len(normalized_name.encode("utf-8")) > 255
        or any(unicodedata.category(character) == "Cf" for character in normalized_name)
    ):
        raise ArtifactExtractionError("artifact_source_name_invalid")
    return normalized_name


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ArtifactExtractionError("artifact_json_duplicate_key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    del value
    raise ArtifactExtractionError("artifact_json_non_finite")


def _bound_json_tree(value: object) -> None:
    pending: list[tuple[object, int]] = [(value, 1)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 10_000 or depth > 64:
            raise ArtifactExtractionError("artifact_json_complexity_exceeded")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


__all__ = [
    "ArtifactExtractionError",
    "ExtractedArtifact",
    "EXTRACTION_CONTRACT_VERSION",
    "MAX_ARTIFACT_BYTES",
    "extract_supported_artifact",
    "validate_source_label",
]
