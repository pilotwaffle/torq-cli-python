"""Offline status data; it is never runtime-effective."""

from __future__ import annotations

from typing import Any


def offline_status_data(profile_id: str | None, profile_version: str | None) -> dict[str, Any]:
    return {
        "runtime_state": "offline_unattested",
        "runtime_effective": False,
        "provider_available": False,
        "usage": "unreported",
    }
