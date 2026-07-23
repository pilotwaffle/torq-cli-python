"""Receipt-reconstructible usage and budget summaries."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def summarize_usage(receipts: Sequence[dict[str, Any]], *, budget_usd: float) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    consumed = 0.0
    for receipt in receipts:
        cost = float(receipt.get("cost_usd", 0.0))
        consumed += cost
        usage = receipt.get("usage", "unreported")
        for key, name in (("providers", str(receipt["provider"])), ("agents", str(receipt["agent"]))):
            target = providers if key == "providers" else agents
            row = target.setdefault(name, {"cost_usd": 0.0, "usage": {"tokens": 0}})
            row["cost_usd"] = round(float(row["cost_usd"]) + cost, 10)
            if usage == "unreported":
                row["usage"] = "unreported"
            elif row["usage"] != "unreported":
                row["usage"]["tokens"] += int(usage.get("tokens", 0))
    return {
        "providers": providers,
        "agents": agents,
        "budget": {"consumed_usd": round(consumed, 10), "remaining_usd": round(max(0.0, budget_usd - consumed), 10)},
    }

