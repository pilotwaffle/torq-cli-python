"""Subscription-entitlement windows shared across provider lanes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Protocol

_SETTLEMENTS = frozenset({"plan_covered", "metered", "unknown"})
_USED_SOURCES = frozenset({"receipt_derived", "provider_reported"})
_LIMIT_SOURCES = frozenset({"operator_declared", "provider_reported"})


@dataclass(frozen=True)
class PlanWindow:
    """One entitlement account; multiple providers may share its capacity."""

    account: str
    providers: tuple[str, ...]
    settlement: str
    used: int
    limit: int
    resets_at: str
    used_source: str = "receipt_derived"
    limit_source: str = "operator_declared"
    reserved: int = 0

    def __post_init__(self) -> None:
        if not self.account or not self.providers:
            raise ValueError("entitlement_account_invalid")
        if self.settlement not in _SETTLEMENTS:
            raise ValueError("entitlement_settlement_invalid")
        if (
            self.used < 0
            or self.reserved < 0
            or self.limit < 0
            or self.used + self.reserved > self.limit
        ):
            raise ValueError("entitlement_window_invalid")
        if self.used_source not in _USED_SOURCES:
            raise ValueError("entitlement_used_source_invalid")
        if self.limit_source not in _LIMIT_SOURCES:
            raise ValueError("entitlement_limit_source_invalid")
        if not self.resets_at:
            raise ValueError("entitlement_reset_required")

    def as_receipt(self) -> dict[str, object]:
        return {
            "account": self.account,
            "used": self.used,
            "reserved": self.reserved,
            "limit": self.limit,
            "resets_at": self.resets_at,
            "used_source": self.used_source,
            "limit_source": self.limit_source,
        }


class EntitlementLedger(Protocol):
    def window(self, provider: str) -> PlanWindow: ...

    def reserve(self, provider: str, *, calls: int) -> None: ...

    def cancel(self, provider: str, *, calls: int) -> None: ...

    def reconcile(self, provider: str, *, calls: int) -> None: ...


class InMemoryEntitlementLedger:
    """Deterministic account-keyed ledger suitable for config and replay."""

    def __init__(self, windows: Mapping[str, PlanWindow]) -> None:
        self._windows = dict(windows)
        self._provider_accounts: dict[str, str] = {}
        self._reservations: dict[str, list[int]] = {}
        for account, window in self._windows.items():
            if account != window.account:
                raise ValueError("entitlement_account_key_mismatch")
            for provider in window.providers:
                if provider in self._provider_accounts:
                    raise ValueError(f"entitlement_provider_ambiguous:{provider}")
                self._provider_accounts[provider] = account

    @property
    def windows(self) -> Mapping[str, PlanWindow]:
        return dict(self._windows)

    @classmethod
    def from_config(cls, raw: Mapping[str, object]) -> InMemoryEntitlementLedger:
        windows: dict[str, PlanWindow] = {}
        for account, value in raw.items():
            if not isinstance(account, str) or not isinstance(value, Mapping):
                raise ValueError("entitlement_config_invalid")
            providers_raw = value.get("providers")
            if not isinstance(providers_raw, Mapping):
                raise ValueError(f"entitlement_providers_invalid:{account}")
            providers = tuple(
                sorted(
                    str(provider)
                    for provider, enabled in providers_raw.items()
                    if enabled is True
                )
            )
            windows[account] = PlanWindow(
                account=account,
                providers=providers,
                settlement=str(value.get("settlement", "unknown")),
                used=int(value.get("used", 0)),
                limit=int(value.get("limit", 0)),
                resets_at=str(value.get("resets_at", "")),
                used_source=str(value.get("used_source", "receipt_derived")),
                limit_source=str(value.get("limit_source", "operator_declared")),
                reserved=int(value.get("reserved", 0)),
            )
        return cls(windows)

    def window(self, provider: str) -> PlanWindow:
        account = self._provider_accounts.get(provider)
        if account is None:
            return PlanWindow(
                account=f"unknown:{provider}",
                providers=(provider,),
                settlement="unknown",
                used=0,
                limit=0,
                resets_at="unknown",
            )
        return self._windows[account]

    def reserve(self, provider: str, *, calls: int) -> None:
        if calls < 0:
            raise ValueError("entitlement_calls_invalid")
        window = self.window(provider)
        if window.settlement != "plan_covered":
            return
        if window.used + calls > window.limit:
            raise ValueError("entitlement_window_exceeded")
        self._windows[window.account] = replace(window, used=window.used + calls)
        self._reservations.setdefault(provider, []).append(calls)

    def cancel(self, provider: str, *, calls: int) -> None:
        if calls <= 0:
            raise ValueError("entitlement_calls_invalid")
        window = self.window(provider)
        if window.settlement != "plan_covered":
            return
        pending = self._reservations.get(provider, [])
        try:
            index = pending.index(calls)
        except ValueError as exc:
            raise ValueError("entitlement_reservation_missing") from exc
        pending.pop(index)
        if not pending:
            self._reservations.pop(provider, None)
        if window.used < calls:
            raise ValueError("entitlement_cancellation_invalid")
        self._windows[window.account] = replace(window, used=window.used - calls)

    def reconcile(self, provider: str, *, calls: int) -> None:
        if calls < 0:
            raise ValueError("entitlement_calls_invalid")
        window = self.window(provider)
        if window.settlement != "plan_covered":
            return
        pending = self._reservations.get(provider, [])
        reserved = pending.pop(0) if pending else 0
        if not pending:
            self._reservations.pop(provider, None)
        corrected = window.used - reserved + calls
        if corrected < 0 or corrected > window.limit:
            raise ValueError("entitlement_reconcile_invalid")
        self._windows[window.account] = replace(window, used=corrected)


__all__ = [
    "EntitlementLedger",
    "InMemoryEntitlementLedger",
    "PlanWindow",
]
