"""Receipt-reconstructible usage and budget summaries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "tokens")


def _empty_usage() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_FIELDS, 0)


def summarize_usage(receipts: Sequence[dict[str, Any]], *, budget_usd: float) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    consumed = 0.0
    billed = 0.0
    metered = 0.0
    plan_roles: set[str] = set()
    metered_roles: set[str] = set()
    unpriced_roles: set[str] = set()
    rate_versions: set[str] = set()
    for receipt in receipts:
        raw_cost = receipt.get("cost_usd", 0.0)
        cost = float(raw_cost) if raw_cost is not None else 0.0
        consumed += cost
        raw_billed = receipt.get("billed_usd", raw_cost)
        if raw_billed is not None:
            billed += float(raw_billed)
        raw_metered = receipt.get("metered_usd")
        if raw_metered is not None:
            metered += float(raw_metered)
        role = str(receipt["agent"])
        settlement = str(receipt.get("settlement", "metered"))
        if settlement == "plan_covered":
            plan_roles.add(role)
        elif settlement == "metered":
            metered_roles.add(role)
        if receipt.get("pricing_status") == "rate_unknown":
            unpriced_roles.add(role)
        version = receipt.get("rate_table_version")
        if isinstance(version, str) and version:
            rate_versions.add(version)
        usage = receipt.get("usage", "unreported")
        for key, name in (("providers", str(receipt["provider"])), ("agents", str(receipt["agent"]))):
            target = providers if key == "providers" else agents
            row = target.setdefault(name, {"cost_usd": 0.0, "usage": _empty_usage()})
            row["cost_usd"] = round(float(row["cost_usd"]) + cost, 10)
            if usage == "unreported":
                row["usage"] = "unreported"
            elif row["usage"] != "unreported":
                for field in _TOKEN_FIELDS:
                    row["usage"][field] += int(usage.get(field, 0))
    return {
        "providers": providers,
        "agents": agents,
        "budget": {"consumed_usd": round(consumed, 10), "remaining_usd": round(max(0.0, budget_usd - consumed), 10)},
        "settlement": {
            "billed_usd": round(billed, 10),
            "metered_equivalent_usd": round(metered, 10),
            "plan_covered_roles": sorted(plan_roles),
            "metered_roles": sorted(metered_roles),
            "unpriced_roles": sorted(unpriced_roles),
            "rate_table_version": (
                next(iter(rate_versions)) if len(rate_versions) == 1 else None
            ),
        },
    }
