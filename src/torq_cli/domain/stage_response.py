"""Versioned role-response contract shared by dispatch and orchestration."""

from __future__ import annotations

from collections.abc import Mapping


STAGE_RESPONSE_CONTRACT_ID = "torq-stage-response"
STAGE_RESPONSE_CONTRACT_VERSION = "1.0.0"
STAGE_RESPONSE_STRING_LIMIT = 120
_STRING: Mapping[str, object] = {"type": "string", "maxLength": 120}


def _closed(properties: Mapping[str, object], required: list[str]) -> Mapping[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _status(value: str) -> Mapping[str, object]:
    return _closed(
        {"status": {"type": "string", "const": value}, "proposal": _STRING},
        ["status", "proposal"],
    )


_DEFECT = _closed(
    {
        "defect_id": _STRING,
        "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"]},
        "class": {"type": "string", "enum": ["bug", "ui", "security", "other"]},
        "status": {"type": "string", "const": "open"},
    },
    ["defect_id", "severity", "class", "status"],
)

STAGE_RESPONSE_SCHEMAS: Mapping[str, Mapping[str, object]] = {
    "g1d": _status("design_complete"),
    "g1r": _closed(
        {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "rationale": _STRING,
        },
        ["verdict", "rationale"],
    ),
    "builder": _status("build_complete"),
    "g2a": _closed(
        {
            "verdict": {"type": "string", "enum": ["approve", "reject"]},
            "defects": {"type": "array", "maxItems": 50, "items": _DEFECT},
        },
        ["verdict", "defects"],
    ),
    "refine_bug": _status("repair_complete"),
    "refine_ui": _status("repair_complete"),
}


def stage_response_schema(role: str) -> Mapping[str, object] | None:
    return STAGE_RESPONSE_SCHEMAS.get(role)


def stage_response_matches(role: str, value: object) -> bool:
    schema = stage_response_schema(role)
    return schema is not None and _matches_schema(value, schema)


def _matches_schema(value: object, schema: Mapping[str, object]) -> bool:
    expected_type = schema.get("type")
    if expected_type == "string":
        if not isinstance(value, str):
            return False
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return False
        if "const" in schema and value != schema["const"]:
            return False
        allowed = schema.get("enum")
        return not isinstance(allowed, list) or value in allowed
    if expected_type == "array":
        if not isinstance(value, list):
            return False
        maximum = schema.get("maxItems")
        item_schema = schema.get("items")
        return (
            (not isinstance(maximum, int) or len(value) <= maximum)
            and isinstance(item_schema, Mapping)
            and all(_matches_schema(item, item_schema) for item in value)
        )
    if expected_type == "object":
        if not isinstance(value, Mapping):
            return False
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            return False
        if not set(required).issubset(value):
            return False
        if schema.get("additionalProperties") is False and not set(value).issubset(properties):
            return False
        return all(
            key in properties
            and isinstance(properties[key], Mapping)
            and _matches_schema(item, properties[key])
            for key, item in value.items()
        )
    return False


__all__ = [
    "STAGE_RESPONSE_CONTRACT_ID",
    "STAGE_RESPONSE_CONTRACT_VERSION",
    "STAGE_RESPONSE_SCHEMAS",
    "STAGE_RESPONSE_STRING_LIMIT",
    "stage_response_matches",
    "stage_response_schema",
]
