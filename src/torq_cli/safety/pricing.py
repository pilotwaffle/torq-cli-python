"""Pinned list-price loading and reproducible token pricing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from importlib.resources import files
import json
from typing import Any

import yaml


_MTOK = Decimal(1_000_000)
_PRECISION = Decimal("0.0000000001")


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


@dataclass(frozen=True)
class ModelRate:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


@dataclass(frozen=True)
class PriceQuote:
    metered_usd: str | None
    pricing_status: str
    rate_table_version: str
    rate_table_hash: str


class RateTable:
    def __init__(
        self,
        version: str,
        rates: Mapping[tuple[str, str], ModelRate],
    ) -> None:
        if not version:
            raise ValueError("rate_table_version_required")
        self.version = version
        self._rates = dict(rates)
        sealed = {
            "rate_table_version": version,
            "rates": {
                provider: {
                    model: {
                        "input_usd_per_mtok": format(rate.input_usd_per_mtok, "f"),
                        "output_usd_per_mtok": format(rate.output_usd_per_mtok, "f"),
                    }
                    for (candidate, model), rate in sorted(self._rates.items())
                    if candidate == provider
                }
                for provider in sorted({key[0] for key in self._rates})
            },
        }
        encoded = json.dumps(
            sealed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.sha256 = hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_document(cls, raw: Mapping[str, Any]) -> RateTable:
        version = str(raw.get("rate_table_version", ""))
        rates_raw = raw.get("rates")
        if not isinstance(rates_raw, Mapping):
            raise ValueError("rate_table_invalid")
        rates: dict[tuple[str, str], ModelRate] = {}
        for provider, models in rates_raw.items():
            if not isinstance(provider, str) or not isinstance(models, Mapping):
                raise ValueError("rate_table_invalid")
            for model, values in models.items():
                if not isinstance(model, str) or not isinstance(values, Mapping):
                    raise ValueError("rate_table_invalid")
                rates[(provider, model)] = ModelRate(
                    Decimal(str(values["input_usd_per_mtok"])),
                    Decimal(str(values["output_usd_per_mtok"])),
                )
        return cls(version, rates)

    def quote(
        self,
        provider: str,
        model: str,
        usage: Mapping[str, int],
    ) -> PriceQuote:
        rate = self._rates.get((provider, model))
        if rate is None:
            return PriceQuote(None, "rate_unknown", self.version, self.sha256)
        input_tokens = Decimal(int(usage.get("input_tokens", 0)))
        output_tokens = Decimal(int(usage.get("output_tokens", 0)))
        reasoning_tokens = Decimal(int(usage.get("reasoning_tokens", 0)))
        amount = (
            input_tokens * rate.input_usd_per_mtok
            + (output_tokens + reasoning_tokens) * rate.output_usd_per_mtok
        ) / _MTOK
        sealed = amount.quantize(_PRECISION, rounding=ROUND_HALF_UP)
        return PriceQuote(_decimal_text(sealed), "priced", self.version, self.sha256)


def load_default_rate_table() -> RateTable:
    resource = files("torq_cli").joinpath("data/list_prices.v1.yaml")
    raw = yaml.safe_load(resource.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("rate_table_invalid")
    return RateTable.from_document(raw)


__all__ = [
    "ModelRate",
    "PriceQuote",
    "RateTable",
    "load_default_rate_table",
]
