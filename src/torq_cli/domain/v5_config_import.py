"""Closed, registry-authoritative projection of the normalized V5 fixture."""

from __future__ import annotations

import json
import unicodedata
from typing import Any, Mapping, cast

from .config_schema import resolve_profile, validate_config_shape, validate_eligibility
from .registry_schema import Registry


EXPECTED_ROLES = ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")
EXPECTED_AGENT_KEYS = {"role_id", "cli", "endpoint", "model", "prompt"}
MAX_DEPTH = 8

TARGET_CONFIG_UTF8 = (
    b'{"binding_overrides":{"builder":{"connector_id":"deepseek-direct-api","enabled":true},'
    b'"g1d":{"connector_id":"anthropic-agent-sdk","enabled":true},'
    b'"g1r":{"connector_id":"anthropic-agent-sdk","enabled":true},'
    b'"g2a":{"connector_id":"anthropic-agent-sdk","enabled":true},'
    b'"refine_bug":{"connector_id":"moonshot-direct-api","enabled":true},'
    b'"refine_ui":{"connector_id":"zai-direct-api","enabled":true}},'
    b'"config_version":1,"connectors":{"anthropic-agent-sdk":{"enabled":true,"provider_id":"anthropic","surface":"agent_sdk"},'
    b'"deepseek-direct-api":{"enabled":true,"provider_id":"deepseek","surface":"direct_api"},'
    b'"moonshot-direct-api":{"enabled":true,"provider_id":"moonshot","surface":"direct_api"},'
    b'"zai-direct-api":{"enabled":true,"provider_id":"zai","surface":"direct_api"}},'
    b'"policy":{"independence_mode":"profile_minimum","loop_budget":1,"resource_limits":{"max_changed_lines":100,"max_cost_cents":100,"max_file_count":10,"max_runtime_seconds":60},"unattestable_action":"deny"},'
    b'"profile":{"id":"torq-v5-repo-compat","version":"1.0.0"}}\n'
)


class DuplicateJsonKey(ValueError):
    pass


class TooDeep(ValueError):
    pass


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey()
        result[key] = value
    return result


def _depth(value: Any, level: int = 0) -> None:
    if level > MAX_DEPTH:
        raise TooDeep()
    if isinstance(value, Mapping):
        for item in value.values():
            _depth(item, level + 1)
    elif isinstance(value, list):
        for item in value:
            _depth(item, level + 1)


_FORBIDDEN_KEYS = {
    "apikey", "secret", "secretkey", "accesstoken", "authtoken", "token", "password",
    "privatekey", "credential", "credentials", "clientsecret", "bearertoken",
    "openaiapikey", "anthropicapikey", "deepseekapikey", "kimiapikey", "glmapikey", "zaiapikey",
}


def _secret_key(key: object) -> bool:
    normalized = unicodedata.normalize("NFKC", str(key)).casefold()
    normalized = "".join(
        character for character in normalized
        if unicodedata.category(character) not in {"Pd", "Pc", "Zs"}
    )
    return normalized in _FORBIDDEN_KEYS


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_secret_key(key) or _contains_secret_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def parse_legacy_config(raw: bytes) -> tuple[str | None, Mapping[str, Any] | None]:
    if len(raw) > 65_536:
        return "legacy_config_schema_invalid", None
    if raw.startswith(b"\xef\xbb\xbf"):
        return "legacy_config_syntax_invalid", None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey):
        return "legacy_config_syntax_invalid", None
    try:
        _depth(value)
    except TooDeep:
        return "legacy_config_schema_invalid", None
    if _contains_secret_key(value):
        return "legacy_config_secret_field_forbidden", None
    if not isinstance(value, Mapping) or set(value) != {"agents", "schema"}:
        return "legacy_config_schema_invalid", None
    if value.get("schema") != "torq-oracle-v5-config-v1" or not isinstance(value.get("agents"), list):
        return "legacy_config_schema_invalid", None
    agents = value["agents"]
    for agent in agents:
        if not isinstance(agent, Mapping) or set(agent) != EXPECTED_AGENT_KEYS:
            return "legacy_config_schema_invalid", None
        if (
            not isinstance(agent.get("role_id"), str)
            or not isinstance(agent.get("cli"), str)
            or (agent.get("endpoint") is not None and not isinstance(agent.get("endpoint"), str))
            or not isinstance(agent.get("model"), str)
            or not isinstance(agent.get("prompt"), str)
        ):
            return "legacy_config_schema_invalid", None
    if len(agents) != len(EXPECTED_ROLES):
        return "legacy_config_role_missing", None
    role_ids = [agent["role_id"] for agent in agents]
    if len(set(role_ids)) != len(role_ids):
        return "legacy_config_role_duplicate", None
    if set(role_ids) != set(EXPECTED_ROLES):
        return "legacy_config_role_missing", None
    return None, value


def validate_legacy_mapping(value: Mapping[str, Any], reference: Mapping[str, Any]) -> str | None:
    expected = {
        item["role_id"]: item for item in reference.get("agents", [])
        if isinstance(item, Mapping) and isinstance(item.get("role_id"), str)
    }
    actual = {item["role_id"]: item for item in value["agents"]}
    if any(actual.get(role) != expected.get(role) for role in EXPECTED_ROLES):
        return "legacy_config_mapping_unsupported"
    return None


def validate_projection(value: Mapping[str, Any], registry: Registry) -> bool:
    return not (
        validate_config_shape(value, registry)
        or resolve_profile(value, registry)
        or validate_eligibility(value, registry)
    )


def target_config() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(TARGET_CONFIG_UTF8.decode("utf-8")))


def reachability_case(finding_id: str) -> tuple[str]:
    """Provide a closed finding result for catalog reachability tests."""
    return (finding_id,)
