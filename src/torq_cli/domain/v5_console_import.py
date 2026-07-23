"""Hardened normalization of the raw TORQ Console V5 YAML shape."""

from __future__ import annotations

import datetime as dt
import unicodedata
from typing import Any, Mapping, NoReturn

import yaml
from yaml.events import (
    AliasEvent,
    DocumentEndEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
    StreamEndEvent,
    StreamStartEvent,
)
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


EXPECTED_ROLES = ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")
MAX_BYTES = 65_536
MAX_EVENTS = 1_024
MAX_DEPTH = 8
UTF8_BOM = b"\xef\xbb\xbf"

_ROOT_KEYS = {
    "version", "created", "supersedes", "agents", "state_machine",
    "rejection_routing", "cost_guardrails", "success_criteria",
}
_AGENT_REQUIRED_KEYS = {"role", "model", "cli", "prompt", "reads", "writes"}
_AGENT_KEYS = _AGENT_REQUIRED_KEYS | {"endpoint", "notes"}
_STATE_KEYS = {"states"}
_ROUTING_KEYS = {"architecture_issue", "bug_or_race", "ui_polish", "spec_is_wrong", "ambiguous"}
_COST_KEYS = {"max_cost_per_prd_usd", "alert_threshold_usd"}
_MAP_TAG = "tag:yaml.org,2002:map"
_SEQ_TAG = "tag:yaml.org,2002:seq"
_STR_TAG = "tag:yaml.org,2002:str"
_SCALAR_TAGS = frozenset({
    _STR_TAG,
    "tag:yaml.org,2002:int",
    "tag:yaml.org,2002:bool",
    "tag:yaml.org,2002:null",
    "tag:yaml.org,2002:float",
    "tag:yaml.org,2002:timestamp",
})
_FORBIDDEN_KEYS = {
    "api_key", "apikey", "secret", "secret_key", "access_token", "auth_token", "token",
    "password", "private_key", "credential", "credentials", "client_secret", "bearer_token",
    "openai_api_key", "anthropic_api_key", "deepseek_api_key", "kimi_api_key", "glm_api_key",
    "zai_api_key",
}


class ConsoleConfigSyntaxError(ValueError):
    """Opaque raw Console parser failure."""


def _fail() -> NoReturn:
    raise ConsoleConfigSyntaxError("raw Console config syntax is invalid")


def _preflight_events(text: str) -> None:
    try:
        events = list(yaml.parse(text))
    except yaml.YAMLError:
        _fail()
    meaningful = [event for event in events if not isinstance(event, (StreamStartEvent, StreamEndEvent))]
    if len(meaningful) > MAX_EVENTS:
        _fail()
    documents = 0
    depth = 0
    for event in meaningful:
        if isinstance(event, DocumentStartEvent):
            documents += 1
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None) is not None:
            _fail()
        if getattr(event, "tag", None) is not None:
            _fail()
        if isinstance(event, ScalarEvent) and event.value == "<<" and event.style is None:
            _fail()
        if isinstance(event, (MappingStartEvent, SequenceStartEvent)):
            depth += 1
            if depth > MAX_DEPTH:
                _fail()
        elif isinstance(event, (MappingEndEvent, SequenceEndEvent)):
            depth -= 1
            if depth < 0:
                _fail()
        elif isinstance(event, DocumentEndEvent) and depth != 0:
            _fail()
    if documents != 1 or depth != 0:
        _fail()


def _preflight_node(node: Node) -> None:
    if isinstance(node, ScalarNode):
        if node.tag not in _SCALAR_TAGS:
            _fail()
        return
    if isinstance(node, SequenceNode):
        if node.tag != _SEQ_TAG:
            _fail()
        for item in node.value:
            _preflight_node(item)
        return
    if not isinstance(node, MappingNode) or node.tag != _MAP_TAG:
        _fail()
    identities: set[str] = set()
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.tag != _STR_TAG:
            _fail()
        identity = unicodedata.normalize("NFC", key.value)
        if identity in identities:
            _fail()
        identities.add(identity)
        _preflight_node(value)


def _parse(payload: bytes) -> Mapping[str, Any]:
    if len(payload) > MAX_BYTES:
        raise OverflowError()
    if payload.startswith(UTF8_BOM):
        _fail()
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail()
    _preflight_events(text)
    try:
        nodes = list(yaml.compose_all(text))
    except yaml.YAMLError:
        _fail()
    if len(nodes) != 1 or nodes[0] is None or not isinstance(nodes[0], MappingNode):
        _fail()
    _preflight_node(nodes[0])
    try:
        value = yaml.load(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        _fail()
    if not isinstance(value, Mapping):
        _fail()
    return value


def _normalized_key(key: object) -> str:
    return unicodedata.normalize("NFC", str(key)).lower().replace("-", "_")


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _normalized_key(key) in _FORBIDDEN_KEYS or _contains_secret_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _valid_supporting_shape(value: Mapping[str, Any]) -> bool:
    created = value.get("created")
    if not isinstance(created, (str, dt.date)) or not isinstance(value.get("supersedes"), str):
        return False
    state_machine = value.get("state_machine")
    routing = value.get("rejection_routing")
    costs = value.get("cost_guardrails")
    success = value.get("success_criteria")
    if not isinstance(state_machine, Mapping) or set(state_machine) != _STATE_KEYS:
        return False
    if not _string_list(state_machine.get("states")):
        return False
    if not isinstance(routing, Mapping) or set(routing) != _ROUTING_KEYS:
        return False
    if not all(isinstance(item, str) for item in routing.values()):
        return False
    if not isinstance(costs, Mapping) or set(costs) != _COST_KEYS:
        return False
    if not all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in costs.values()):
        return False
    if not isinstance(success, list) or not success:
        return False
    return all(
        isinstance(item, Mapping)
        and len(item) == 1
        and all(isinstance(key, str) and isinstance(content, str) for key, content in item.items())
        for item in success
    )


def _normalize_agents(agents: object) -> tuple[str | None, list[dict[str, Any]] | None]:
    if not isinstance(agents, Mapping) or set(agents) != set(EXPECTED_ROLES):
        return "legacy_config_role_missing", None
    normalized: list[dict[str, Any]] = []
    for role_id in EXPECTED_ROLES:
        agent = agents.get(role_id)
        if not isinstance(agent, Mapping) or not _AGENT_REQUIRED_KEYS.issubset(agent) or not set(agent) <= _AGENT_KEYS:
            return "legacy_config_schema_invalid", None
        if not all(isinstance(agent.get(field), str) for field in ("role", "model", "cli", "prompt")):
            return "legacy_config_schema_invalid", None
        if not _string_list(agent.get("reads")) or not _string_list(agent.get("writes")):
            return "legacy_config_schema_invalid", None
        endpoint = agent.get("endpoint")
        notes = agent.get("notes")
        if (endpoint is not None and not isinstance(endpoint, str)) or (notes is not None and not isinstance(notes, str)):
            return "legacy_config_schema_invalid", None
        normalized.append({
            "role_id": role_id,
            "cli": agent["cli"],
            "endpoint": endpoint,
            "model": agent["model"],
            "prompt": agent["prompt"],
        })
    return None, normalized


def parse_console_config(payload: bytes) -> tuple[str | None, Mapping[str, Any] | None]:
    """Parse raw Console YAML into the authenticated normalized comparison shape."""
    try:
        value = _parse(payload)
    except OverflowError:
        return "legacy_config_schema_invalid", None
    except ConsoleConfigSyntaxError:
        return "legacy_config_syntax_invalid", None
    if _contains_secret_key(value):
        return "legacy_config_secret_field_forbidden", None
    version = value.get("version")
    if set(value) != _ROOT_KEYS or not isinstance(version, int) or isinstance(version, bool) or version != 5:
        return "legacy_config_schema_invalid", None
    if not _valid_supporting_shape(value):
        return "legacy_config_schema_invalid", None
    finding, agents = _normalize_agents(value.get("agents"))
    if finding is not None:
        return finding, None
    assert agents is not None
    return None, {"schema": "torq-oracle-v5-config-v1", "agents": agents}
