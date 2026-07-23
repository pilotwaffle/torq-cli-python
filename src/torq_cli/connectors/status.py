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


def inspect_harness(
    expected: Mapping[str, tuple[str, str]],
    actual: Mapping[str, Mapping[str, str | None]],
) -> dict[str, Any]:
    agents: dict[str, Any] = {}
    for agent, (provider, model) in expected.items():
        observed = actual.get(agent, {})
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

