"""Immutable production capability decision for governed chat on Linux."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LinuxContainmentCapability:
    """Explain why user-systemd is not a production ownership boundary."""

    available: bool
    mechanism: str
    reason: str


_CAPABILITY = LinuxContainmentCapability(
    available=False,
    mechanism="user_systemd_experimental_not_strong",
    reason="distinct_identity_system_broker_required",
)


def linux_containment_capability() -> LinuxContainmentCapability:
    """Return the immutable fail-closed Linux production capability."""
    return _CAPABILITY
