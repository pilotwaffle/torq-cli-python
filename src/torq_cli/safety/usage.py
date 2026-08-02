"""Receipt-reconstructible usage and budget summaries."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

_TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "tokens")


def _empty_usage() -> dict[str, int]:
    return dict.fromkeys(_TOKEN_FIELDS, 0)


def _money(value: object) -> Decimal:
    if value is None:
        return Decimal(0)
    if isinstance(value, bool):
        raise ValueError("usage_amount_invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("usage_amount_invalid") from exc
    if not amount.is_finite():
        raise ValueError("usage_amount_invalid")
    return amount


def _money_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def summarize_usage(receipts: Sequence[dict[str, Any]], *, budget_usd: float) -> dict[str, Any]:
    providers: dict[str, dict[str, Any]] = {}
    agents: dict[str, dict[str, Any]] = {}
    consumed = Decimal(0)
    billed = Decimal(0)
    billed_known = True
    metered = Decimal(0)
    plan_roles: set[str] = set()
    metered_roles: set[str] = set()
    unpriced_roles: set[str] = set()
    rate_versions: set[str] = set()
    for receipt in receipts:
        raw_cost = receipt.get("cost_usd", 0)
        cost = _money(raw_cost)
        consumed += cost
        raw_billed = receipt.get("billed_usd", raw_cost)
        if (
            "billed_usd" not in receipt
            and str(receipt.get("cost_basis", "")).startswith("configured_worst_case")
        ):
            raw_billed = None
        if raw_billed is not None:
            billed += _money(raw_billed)
        else:
            billed_known = False
        raw_metered = receipt.get("metered_usd")
        if raw_metered is not None:
            metered += _money(raw_metered)
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
            row = target.setdefault(name, {"cost_usd": Decimal(0), "usage": _empty_usage()})
            row["cost_usd"] += cost
            if usage == "unreported":
                row["usage"] = "unreported"
            elif row["usage"] != "unreported":
                for field in _TOKEN_FIELDS:
                    row["usage"][field] += int(usage.get(field, 0))
    for rows in (providers, agents):
        for row in rows.values():
            row["cost_usd"] = _money_text(row["cost_usd"])
    budget = _money(budget_usd)
    return {
        "providers": providers,
        "agents": agents,
        "budget": {
            "consumed_usd": _money_text(consumed),
            "remaining_usd": _money_text(max(Decimal(0), budget - consumed)),
        },
        "settlement": {
            "billed_usd": _money_text(billed) if billed_known else None,
            "billed_status": "complete" if billed_known else "incomplete",
            "metered_equivalent_usd": _money_text(metered),
            "plan_covered_roles": sorted(plan_roles),
            "metered_roles": sorted(metered_roles),
            "unpriced_roles": sorted(unpriced_roles),
            "rate_table_version": (
                next(iter(rate_versions)) if len(rate_versions) == 1 else None
            ),
        },
    }
