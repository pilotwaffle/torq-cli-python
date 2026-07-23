"""Immutable Foundation result and registry model primitives."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


def deep_freeze(value: Any) -> Any:
    """Recursively freeze mappings and sequences used in public results."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(deep_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class ResolutionSnapshot:
    registry_id: str | None
    registry_version: str | None
    registry_resource_sha256: str | None
    config_path: str | None
    config_version: int | None
    profile_id: str | None
    profile_version: str | None
    resolution_stage: str


@dataclass(frozen=True)
class ResultEnvelope:
    schema_version: str
    command: str
    status: str
    snapshot: ResolutionSnapshot | None
    findings: tuple[Any, ...]
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(deep_freeze(item) for item in self.findings))
        object.__setattr__(self, "data", deep_freeze(self.data))
