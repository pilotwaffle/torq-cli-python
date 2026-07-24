"""Closed v1 configuration validation with syntax-only credential references."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, NoReturn

import yaml
from yaml.events import AliasEvent, DocumentStartEvent, DocumentEndEvent, MappingEndEvent, MappingStartEvent, ScalarEvent, SequenceEndEvent, SequenceStartEvent, StreamEndEvent, StreamStartEvent
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from .findings import Finding
from .findings import FindingCatalog
from .registry_schema import Registry


_CRED_REF = re.compile(r"credref_[0-9a-f]{32}")
_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_FORBIDDEN_KEYS = {
    "api_key", "apikey", "secret", "secret_key", "access_token", "auth_token", "token",
    "password", "private_key", "credential", "credentials", "client_secret", "bearer_token",
    "openai_api_key", "anthropic_api_key", "deepseek_api_key", "kimi_api_key", "glm_api_key",
    "zai_api_key",
}
_YAML_MAP_TAG = "tag:yaml.org,2002:map"
_YAML_STRING_TAG = "tag:yaml.org,2002:str"
_YAML_SCALAR_TAGS = frozenset({
    _YAML_STRING_TAG,
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
})
_MAX_CONFIG_EVENTS = 1_024
_MAX_CONFIG_DEPTH = 8
_UTF8_BOM = b"\xef\xbb\xbf"


class ConfigSyntaxError(ValueError):
    """Opaque local parser failure for all policy or YAML syntax violations."""


@dataclass(frozen=True)
class Config:
    profile_id: str
    profile_version: str
    binding_overrides: Mapping[str, Mapping[str, Any]]
    connectors: Mapping[str, Mapping[str, Any]]
    policy: Mapping[str, Any]


def _normalize_key(key: object) -> str:
    return unicodedata.normalize("NFC", str(key)).lower().replace("-", "_")


def _parser_fail() -> NoReturn:
    raise ConfigSyntaxError("config syntax is invalid")


def _preflight_events(text: str) -> None:
    try:
        events = list(yaml.parse(text))
    except yaml.YAMLError:
        _parser_fail()
    meaningful = [event for event in events if not isinstance(event, (StreamStartEvent, StreamEndEvent))]
    if len(meaningful) > _MAX_CONFIG_EVENTS:
        _parser_fail()
    documents = 0
    depth = 0
    for event in meaningful:
        if isinstance(event, DocumentStartEvent):
            documents += 1
        if isinstance(event, AliasEvent):
            _parser_fail()
        anchor = getattr(event, "anchor", None)
        if anchor is not None:
            _parser_fail()
        tag = getattr(event, "tag", None)
        if tag is not None:
            _parser_fail()
        if isinstance(event, ScalarEvent) and event.value == "<<" and event.style is None:
            _parser_fail()
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            depth += 1
            if depth > _MAX_CONFIG_DEPTH:
                _parser_fail()
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth -= 1
            if depth < 0:
                _parser_fail()
        elif isinstance(event, DocumentEndEvent) and depth != 0:
            _parser_fail()
    if documents != 1 or depth != 0:
        _parser_fail()


def _preflight_node(node: Node) -> None:
    if isinstance(node, SequenceNode):
        _parser_fail()
    if isinstance(node, ScalarNode):
        if node.tag not in _YAML_SCALAR_TAGS:
            _parser_fail()
        return
    if not isinstance(node, MappingNode) or node.tag != _YAML_MAP_TAG:
        _parser_fail()
    identities: set[str] = set()
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag != _YAML_STRING_TAG:
            _parser_fail()
        identity = unicodedata.normalize("NFC", key.value)
        if identity in identities:
            _parser_fail()
        identities.add(identity)
        _preflight_node(value)


def _construct_preflighted(text: str) -> Mapping[str, Any]:
    try:
        value = yaml.load(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        _parser_fail()
    if not isinstance(value, Mapping):
        _parser_fail()
    return value


def parse_config_bytes(payload: bytes) -> Mapping[str, Any]:
    if len(payload) > 65_536 or payload.startswith(_UTF8_BOM):
        _parser_fail()
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _parser_fail()
    return parse_config_text(text)


def reject_unknown_keys(mapping: Mapping[str, Any], allowed: set[str], path: str) -> list[Finding]:
    findings: list[Finding] = []
    for key in mapping:
        if isinstance(key, str) and _normalize_key(key) in _FORBIDDEN_KEYS:
            findings.append(FindingCatalog.make("raw_credential_field_forbidden", path=path))
        elif key not in allowed:
            key_text = str(key)
            child_path = f"/{key_text}" if path == "/" else f"{path}/{key_text}"
            findings.append(FindingCatalog.make("config_schema_invalid", path=child_path))
    return findings


def validate_credential_ref(value: object, path: str) -> Finding | None:
    if not isinstance(value, str) or _CRED_REF.fullmatch(value) is None:
        return FindingCatalog.make("credential_ref_invalid", path=path)
    return None


def validate_binding_override(role_id: str, override: Mapping[str, Any], registry: Registry) -> list[Finding]:
    forbidden = {"provider_id", "model_id", "prompt_id", "prompt_version", "effort_id", "policy_id", "strategy_id", "authority", "independence"}
    findings = reject_unknown_keys(
        override,
        {"enabled", "connector_id"} | forbidden,
        f"/binding_overrides/{role_id}",
    )
    if any(key in forbidden for key in override):
        findings.append(FindingCatalog.make("binding_override_forbidden", path=f"/binding_overrides/{role_id}"))
    if "enabled" in override and not isinstance(override["enabled"], bool):
        findings.append(FindingCatalog.make("config_schema_invalid", path=f"/binding_overrides/{role_id}/enabled"))
    if "connector_id" in override and (
        not isinstance(override["connector_id"], str)
        or _CONNECTOR_ID.fullmatch(override["connector_id"]) is None
    ):
        findings.append(FindingCatalog.make("config_schema_invalid", path=f"/binding_overrides/{role_id}/connector_id"))
    profile = registry.profiles.get("torq-v5-6-live")
    if profile is not None and override.get("enabled", True) is False:
        required = set(registry.strategies[profile.strategy_id])
        required.update(rule.reviewed_role for rule in profile.independence_rules)
        required.update(rule.reviewer_role for rule in profile.independence_rules)
        if role_id in required:
            findings.append(FindingCatalog.make("required_role_disabled", path=f"/binding_overrides/{role_id}/enabled", context={"role_id": role_id}))
    return findings


def _validate_binding_override_shape(role_id: str, override: Mapping[str, Any]) -> list[Finding]:
    forbidden = {"provider_id", "model_id", "prompt_id", "prompt_version", "effort_id", "policy_id", "strategy_id", "authority", "independence"}
    findings = reject_unknown_keys(
        override,
        {"enabled", "connector_id"} | forbidden,
        f"/binding_overrides/{role_id}",
    )
    if not override:
        findings.append(FindingCatalog.make("config_schema_invalid", path=f"/binding_overrides/{role_id}"))
    if "enabled" in override and not isinstance(override["enabled"], bool):
        findings.append(FindingCatalog.make("config_schema_invalid", path=f"/binding_overrides/{role_id}/enabled"))
    if "connector_id" in override and (
        not isinstance(override["connector_id"], str)
        or _CONNECTOR_ID.fullmatch(override["connector_id"]) is None
    ):
        findings.append(FindingCatalog.make("config_schema_invalid", path=f"/binding_overrides/{role_id}/connector_id"))
    return findings


def validate_config_version(config: Mapping[str, Any]) -> list[Finding]:
    if "config_version" not in config:
        return [
            FindingCatalog.make("config_schema_invalid", path="/config_version"),
            FindingCatalog.make("config_version_missing", path="/"),
        ]
    version = config["config_version"]
    if not isinstance(version, int) or isinstance(version, bool):
        return [FindingCatalog.make("config_schema_invalid", path="/config_version")]
    if version < 1:
        return [FindingCatalog.make("migration_required", path="/config_version")]
    if version > 1:
        return [FindingCatalog.make("config_version_unsupported", path="/config_version")]
    return []


def _sort(findings: list[Finding]) -> tuple[Finding, ...]:
    return tuple(sorted(findings, key=lambda item: (item.stage, item.path, item.id)))


def _unique_sorted(findings: list[Finding]) -> tuple[Finding, ...]:
    unique: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.id, finding.path)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return _sort(unique)


def validate_config_shape(config: Mapping[str, Any], registry: Registry) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    if not isinstance(config, Mapping):
        return (FindingCatalog.make("config_schema_invalid", path="/"),)
    findings.extend(reject_unknown_keys(
        config,
        {"config_version", "profile", "binding_overrides", "connectors", "policy", "credential_source"},
        "/",
    ))
    findings.extend(validate_config_version(config))
    for required in ("profile", "binding_overrides", "connectors", "policy"):
        if required not in config:
            findings.append(FindingCatalog.make("config_schema_invalid", path=f"/{required}"))
    profile_raw = config.get("profile")
    if not isinstance(profile_raw, Mapping):
        findings.append(FindingCatalog.make("config_schema_invalid", path="/profile"))
    else:
        findings.extend(reject_unknown_keys(profile_raw, {"id", "version"}, "/profile"))
        if not isinstance(profile_raw.get("id"), str):
            findings.append(FindingCatalog.make("config_schema_invalid", path="/profile/id"))
        if not isinstance(profile_raw.get("version"), str):
            findings.append(FindingCatalog.make("config_schema_invalid", path="/profile/version"))
    overrides_raw = config.get("binding_overrides")
    overrides = overrides_raw
    if not isinstance(overrides, Mapping):
        findings.append(FindingCatalog.make("config_schema_invalid", path="/binding_overrides"))
        overrides = {}
    for role_id, override in overrides.items():
        if not isinstance(role_id, str) or role_id not in registry.roles or not isinstance(override, Mapping):
            findings.append(FindingCatalog.make("config_schema_invalid", path="/binding_overrides"))
        else:
            findings.extend(_validate_binding_override_shape(str(role_id), override))
    connectors_raw = config.get("connectors")
    connectors = connectors_raw
    if not isinstance(connectors, Mapping):
        findings.append(FindingCatalog.make("config_schema_invalid", path="/connectors"))
        connectors = {}
    for connector_id, connector in connectors.items():
        path = f"/connectors/{connector_id}"
        if not isinstance(connector_id, str) or _CONNECTOR_ID.fullmatch(str(connector_id)) is None or not isinstance(connector, Mapping):
            findings.append(FindingCatalog.make("config_schema_invalid", path=path))
            continue
        findings.extend(reject_unknown_keys(connector, {"provider_id", "surface", "enabled", "credential_ref"}, path))
        if "credential_ref" in connector:
            finding = validate_credential_ref(connector["credential_ref"], f"{path}/credential_ref")
            if finding is not None:
                findings.append(finding)
        if not isinstance(connector.get("provider_id"), str) or not isinstance(connector.get("surface"), str) or not isinstance(connector.get("enabled"), bool):
            findings.append(FindingCatalog.make("config_schema_invalid", path=path))
        elif connector["provider_id"] not in {"anthropic", "deepseek", "openai", "moonshot", "zai"} or connector["surface"] not in {"agent_sdk", "codex_sdk", "acp", "direct_api"}:
            findings.append(FindingCatalog.make("config_schema_invalid", path=path))
    credential_source = config.get("credential_source")
    if credential_source is not None:
        if not isinstance(credential_source, Mapping):
            findings.append(FindingCatalog.make("config_schema_invalid", path="/credential_source"))
        else:
            findings.extend(reject_unknown_keys(
                credential_source,
                {"kind", "path"},
                "/credential_source",
            ))
            source_path = credential_source.get("path")
            if credential_source.get("kind") != "external_env":
                findings.append(FindingCatalog.make("config_schema_invalid", path="/credential_source/kind"))
            if (
                not isinstance(source_path, str)
                or not source_path
                or not (source_path.startswith("/") or _WINDOWS_ABSOLUTE.match(source_path))
            ):
                findings.append(FindingCatalog.make("config_schema_invalid", path="/credential_source/path"))
    policy = config.get("policy")
    if not isinstance(policy, Mapping):
        findings.append(FindingCatalog.make("config_schema_invalid", path="/policy"))
    else:
        findings.extend(reject_unknown_keys(policy, {"independence_mode", "unattestable_action", "loop_budget", "resource_limits"}, "/policy"))
        if policy.get("independence_mode") not in {"profile_minimum", "vendor_strict"} or policy.get("unattestable_action") != "deny":
            findings.append(FindingCatalog.make("config_schema_invalid", path="/policy"))
        if not isinstance(policy.get("loop_budget"), int) or isinstance(policy.get("loop_budget"), bool) or not 1 <= policy.get("loop_budget", 0) <= 10:
            findings.append(FindingCatalog.make("config_schema_invalid", path="/policy/loop_budget"))
        limits = policy.get("resource_limits")
        if not isinstance(limits, Mapping):
            findings.append(FindingCatalog.make("config_schema_invalid", path="/policy/resource_limits"))
        else:
            findings.extend(reject_unknown_keys(limits, {"max_runtime_seconds", "max_cost_cents", "max_file_count", "max_changed_lines"}, "/policy/resource_limits"))
            ranges = {"max_runtime_seconds": (1, 86400), "max_cost_cents": (0, 100000000), "max_file_count": (1, 100000), "max_changed_lines": (1, 1000000)}
            for name, (minimum, maximum) in ranges.items():
                value = limits.get(name)
                if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
                    findings.append(FindingCatalog.make("config_schema_invalid", path=f"/policy/resource_limits/{name}"))
    return _unique_sorted(findings)


def resolve_profile(config: Mapping[str, Any], registry: Registry) -> tuple[Finding, ...]:
    profile = config.get("profile") if isinstance(config, Mapping) else None
    if not isinstance(profile, Mapping):
        return ()
    profile_id = profile.get("id")
    profile_version = profile.get("version")
    if not isinstance(profile_id, str) or not isinstance(profile_version, str):
        return ()
    findings: list[Finding] = []
    if profile_id not in registry.profiles:
        findings.append(FindingCatalog.make("profile_unknown", path="/profile/id"))
    elif profile_version != registry.profiles[profile_id].profile_version:
        findings.append(FindingCatalog.make("profile_version_unknown", path="/profile/version"))
    return _unique_sorted(findings)


def validate_eligibility(config: Mapping[str, Any], registry: Registry) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    overrides_raw = config.get("binding_overrides") if isinstance(config, Mapping) else None
    overrides = overrides_raw if isinstance(overrides_raw, Mapping) else {}
    for role_id, override in overrides.items():
        if isinstance(role_id, str) and role_id in registry.roles and isinstance(override, Mapping):
            findings.extend(validate_binding_override(role_id, override, registry))
    connectors_raw = config.get("connectors") if isinstance(config, Mapping) else None
    connectors = connectors_raw if isinstance(connectors_raw, Mapping) else {}
    profile_raw = config.get("profile") if isinstance(config, Mapping) else None
    profile_id = profile_raw.get("id") if isinstance(profile_raw, Mapping) else None
    profile = registry.profiles.get(profile_id) if isinstance(profile_id, str) else None
    if profile is not None and isinstance(overrides_raw, Mapping) and isinstance(connectors_raw, Mapping):
        for role_id, override in overrides.items():
            if not isinstance(role_id, str) or role_id not in profile.bindings:
                continue
            if not isinstance(override, Mapping) or "connector_id" not in override:
                continue
            connector_id = override["connector_id"]
            if not isinstance(connector_id, str):
                continue
            connector = connectors.get(connector_id)
            path = f"/binding_overrides/{role_id}/connector_id"
            if not isinstance(connector, Mapping) or connector.get("enabled") is not True:
                findings.append(FindingCatalog.make("connector_unknown", path=path, context={"role_id": str(role_id)}))
                continue
            binding = profile.bindings[str(role_id)]
            if connector.get("provider_id") != binding.provider_id:
                findings.append(FindingCatalog.make("connector_provider_mismatch", path=path, context={"role_id": str(role_id)}))
            if connector.get("surface") != binding.connector_surface:
                findings.append(FindingCatalog.make("connector_surface_mismatch", path=path, context={"role_id": str(role_id)}))
    return _unique_sorted(findings)


def validate_config(config: Mapping[str, Any], registry: Registry) -> tuple[Finding, ...]:
    """Compatibility facade combining the three explicit validation stages."""
    findings = [
        *validate_config_shape(config, registry),
        *resolve_profile(config, registry),
        *validate_eligibility(config, registry),
    ]
    return _unique_sorted(findings)


def parse_config_text(text: str) -> Mapping[str, Any]:
    if not isinstance(text, str) or text.startswith("\ufeff"):
        _parser_fail()
    try:
        payload = text.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _parser_fail()
    if len(payload) > 65_536 or payload.startswith(_UTF8_BOM):
        _parser_fail()
    _preflight_events(text)
    try:
        nodes = list(yaml.compose_all(text))
    except yaml.YAMLError:
        _parser_fail()
    if len(nodes) != 1 or not isinstance(nodes[0], MappingNode):
        _parser_fail()
    _preflight_node(nodes[0])
    return _construct_preflighted(text)
