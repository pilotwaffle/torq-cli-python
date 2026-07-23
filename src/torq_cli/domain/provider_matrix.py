"""Closed provider-surface evidence matrix consumed by connector decisions."""

from __future__ import annotations

from copy import deepcopy
from importlib import resources
from typing import Any, Mapping, cast

import yaml


PROVIDERS = ("claude", "codex", "grok", "kimi", "zai", "deepseek")
REQUIRED_SURFACES = (
    "authentication",
    "machine_readable_output",
    "session_resume",
    "cancellation",
    "tool_events",
    "usage",
    "resolved_model_identity",
    "working_directory_isolation",
    "rate_limit_behavior",
)
_DECISION_FLAGS = {
    "claude": "claude_agent_sdk_primary",
    "codex": "codex_sdk_primary",
    "grok": "grok_acp_primary",
    "kimi": "kimi_direct_api_primary",
    "zai": "zai_direct_api_primary",
    "deepseek": "deepseek_mmh_primary",
}
_CELL_STATUSES = {"verified", "unavailable", "blocked", "unattestable", "unreported"}


def load_provider_matrix() -> dict[str, Any]:
    resource = resources.files("torq_cli").joinpath("data/provider_surfaces.v1.yaml")
    value = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("provider matrix root is invalid")
    return cast(dict[str, Any], deepcopy(value))


def validate_provider_matrix(matrix: Mapping[str, Any]) -> tuple[str, ...]:
    errors: list[str] = []
    if set(matrix) != {"schema", "observed_at", "providers"} or matrix.get("schema") != "torq-provider-surface-matrix-v1":
        errors.append("root_invalid")
    providers = matrix.get("providers")
    if not isinstance(providers, Mapping) or set(providers) != set(PROVIDERS):
        return tuple(sorted(errors + ["provider_set_invalid"]))
    for name in PROVIDERS:
        provider = providers[name]
        if not isinstance(provider, Mapping) or set(provider) != {"surfaces", "tos", "decision"}:
            errors.append(f"{name}:shape_invalid")
            continue
        surfaces = provider["surfaces"]
        if not isinstance(surfaces, Mapping) or set(surfaces) != set(REQUIRED_SURFACES):
            errors.append(f"{name}:surface_set_invalid")
        else:
            for surface_name, cell in surfaces.items():
                if (
                    not isinstance(cell, Mapping)
                    or set(cell) != {"status", "evidence", "observed_at"}
                    or cell.get("status") not in _CELL_STATUSES
                    or not isinstance(cell.get("evidence"), str)
                    or not cell.get("evidence")
                    or cell.get("observed_at") != "2026-07-23"
                ):
                    errors.append(f"{name}:{surface_name}:cell_invalid")
        tos = provider["tos"]
        if (
            not isinstance(tos, Mapping)
            or set(tos) != {"posture", "citation", "checked_at"}
            or not all(isinstance(tos.get(key), str) and tos.get(key) for key in tos)
            or tos.get("checked_at") != "2026-07-23"
        ):
            errors.append(f"{name}:tos_invalid")
        decision = provider["decision"]
        required_flag = _DECISION_FLAGS[name]
        if (
            not isinstance(decision, Mapping)
            or set(decision) != {"primary", "fallback", "usage_expectation", required_flag}
            or decision.get(required_flag) is not True
            or not all(isinstance(decision.get(key), str) and decision.get(key) for key in ("primary", "fallback", "usage_expectation"))
        ):
            errors.append(f"{name}:decision_flags_invalid")
    return tuple(sorted(errors))
