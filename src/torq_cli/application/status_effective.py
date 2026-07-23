"""Effective configuration and lane-health assessment."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def effective_status(config: Mapping[str, Any], lanes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    rendered: dict[str, Any] = {}
    for role, lane in lanes.items():
        state = str(lane.get("state", "unavailable"))
        if state in {"unavailable", "blocked", "degraded"}:
            failures.append(f"{role}:lane_{state}")
        if lane.get("granted") is not True:
            failures.append(f"{role}:model_ungranted")
        if lane.get("eligible") is not True:
            failures.append(f"{role}:binding_ineligible")
        behavior = "normal"
        if state == "degraded" and role == "refine_bug":
            behavior = "HIGH defects will escalate to operator"
        rendered[role] = {**lane, "degradation_behavior": behavior}
    policy = config.get("policy", {})
    independence = policy.get("independence_mode") if isinstance(policy, Mapping) else None
    profile = config.get("profile")
    profile_id = profile.get("id") if isinstance(profile, Mapping) else profile
    profile_version = profile.get("version") if isinstance(profile, Mapping) else config.get("profile_version")
    return {
        "profile": profile_id,
        "profile_version": profile_version,
        "policy_version": config.get("policy_version"),
        "independence_mode": independence,
        "gate_independence": {"g1": "session", "g2": "vendor" if independence == "vendor_strict" else "model"},
        "lanes": rendered,
        "attestation": "matched" if not failures else "failed",
        "failures": sorted(failures),
        "exit_code": 0 if not failures else 3,
    }
