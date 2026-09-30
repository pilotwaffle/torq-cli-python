"""Secret-free connector health and runtime attestation reports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from torq_cli.connectors import Connector, LaneHealth


def auth_status(
    connectors: Mapping[str, Connector],
    bindings: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    providers = {name: {"state": connector.health().state.value, "surface": connector.health().surface, "reason": connector.health().reason} for name, connector in connectors.items()}
    gaps: list[str] = []
    for agent, (provider, model) in bindings.items():
        connector = connectors.get(provider)
        if connector is None or connector.health().state not in {LaneHealth.AVAILABLE, LaneHealth.DEGRADED}:
            gaps.append(f"{agent}:{provider}:{model}")
    return {
        "providers": providers,
        "profiles": {"selected": {"state": "blocked" if gaps else "available", "gaps": gaps}},
        "exit_code": 3 if gaps else 0,
    }


def parse_inspect_documents(
    expected_raw: Any,
    actual_raw: Any,
) -> tuple[dict[str, tuple[str, str]], Mapping[str, Any]]:
    """Validate harness inspect documents or raise ValueError with a finding."""
    if not isinstance(expected_raw, Mapping) or not isinstance(actual_raw, Mapping):
        raise ValueError("inspect_input_not_object")
    expected: dict[str, tuple[str, str]] = {}
    for agent, binding in expected_raw.items():
        key = str(agent)
        if not isinstance(binding, (list, tuple)) or len(binding) != 2:
            raise ValueError(f"inspect_expected_binding_invalid:{key}")
        expected[key] = (str(binding[0]), str(binding[1]))
    for agent, observed in actual_raw.items():
        if not isinstance(observed, Mapping):
            raise ValueError(f"inspect_actual_binding_invalid:{agent}")
    return expected, actual_raw


def inspect_harness(
    expected: Mapping[str, tuple[str, str]],
    actual: Mapping[str, Any],
) -> dict[str, Any]:
    agents: dict[str, Any] = {}
    for agent, (provider, model) in expected.items():
        observed = actual.get(agent, {})
        if not isinstance(observed, Mapping):
            observed_provider = None
            observed_model = None
        else:
            observed_provider = observed.get("provider")
            observed_model = observed.get("model")
        if observed_provider is None or observed_model is None:
            status = "unattestable"
        elif (observed_provider, observed_model) != (provider, model):
            status = "mismatch"
        else:
            status = "matched"
        agents[agent] = {"status": status, "expected_provider": provider, "expected_model": model, "actual_provider": observed_provider, "actual_model": observed_model}
    return {"agents": agents, "ok": all(row["status"] == "matched" for row in agents.values())}
