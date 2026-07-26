"""Fail-closed capability contract for governed chat on macOS."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MacOSContainmentCapability:
    """The guarantees required before governed chat may launch on macOS."""

    available: bool
    distribution: str
    mechanism: str | None
    finding: str
    reason: str
    required_guarantees: tuple[str, ...]


MACOS_CONTAINMENT_CAPABILITY = MacOSContainmentCapability(
    available=False,
    distribution="ordinary_python_wheel",
    mechanism=None,
    finding="owned_process_strong_containment_unavailable",
    reason="macos_signed_containment_helper_required",
    required_guarantees=(
        "no_provider_execution_before_containment",
        "setsid_and_double_fork_cannot_escape",
        "coordinator_crash_triggers_tree_termination",
        "terminal_cancellation_requires_confirmed_empty",
    ),
)


def macos_containment_capability() -> MacOSContainmentCapability:
    """Return the pinned capability of the ordinary pip-installed runtime."""

    return MACOS_CONTAINMENT_CAPABILITY


def require_macos_strong_containment() -> None:
    """Refuse launch until an independently attested native owner is available."""

    capability = macos_containment_capability()
    if not capability.available:
        raise OSError(capability.finding)
