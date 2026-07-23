"""Fail-closed provider connectors with injectable surfaces and vaults."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from torq_cli.core.engine import NormalizedResponse, ProviderRequest, normalize_response


class AuthState(str, Enum):
    LOGGED_IN = "logged_in"
    LOGGED_OUT = "logged_out"
    AMBIGUOUS = "ambiguous"


class LaneHealth(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class FailureClass(str, Enum):
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    MALFORMED_RESPONSE = "malformed_response"
    MIDSTREAM_DROP = "midstream_drop"
    MODEL_NOT_GRANTED = "model_not_granted"
    UNAVAILABLE = "unavailable"
    ELIGIBILITY = "eligibility"


class ConnectorError(RuntimeError):
    def __init__(self, message: str, failure_class: FailureClass = FailureClass.UNAVAILABLE, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retryable = retryable


class Vault(Protocol):
    def get(self, provider: str) -> str | None: ...


class MemoryVault:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = dict(values or {})

    def get(self, provider: str) -> str | None:
        return self._values.get(provider)


class Surface(Protocol):
    name: str
    auth: AuthState
    grants: frozenset[str]

    def invoke(self, *, model: str, prompt: str, session_id: str) -> Mapping[str, Any]: ...
    def cancel(self, session_id: str) -> None: ...


class MockSurface:
    def __init__(
        self,
        name: str,
        payload: Mapping[str, Any],
        *,
        auth: AuthState = AuthState.LOGGED_IN,
        grants: set[str] | None = None,
        failure: ConnectorError | None = None,
    ) -> None:
        self.name = name
        self.payload = dict(payload)
        self.auth = auth
        inferred = {str(payload["model"])} if "model" in payload else set()
        self.grants = frozenset(inferred if grants is None else grants)
        self.failure = failure
        self.cancelled: set[str] = set()

    def invoke(self, *, model: str, prompt: str, session_id: str) -> Mapping[str, Any]:
        del model, prompt, session_id
        if self.failure is not None:
            raise self.failure
        return self.payload

    def cancel(self, session_id: str) -> None:
        self.cancelled.add(session_id)


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    primary_surface: str
    fallback_surface: str | None
    required_models: tuple[str, ...]
    vault_required: bool = False
    eligible_agents: frozenset[str] | None = None
    eligibility_name: str | None = None


@dataclass
class Session:
    session_id: str
    agent: str
    workdir: Path
    surface: Surface
    cancelled: bool = False


@dataclass(frozen=True)
class Health:
    provider: str
    state: LaneHealth
    surface: str | None
    reason: str | None = None


def all_connector_specs() -> dict[str, ConnectorSpec]:
    return {
        "claude": ConnectorSpec("claude", "agent_sdk", None, ("fable-5", "opus-4.8")),
        "codex": ConnectorSpec("codex", "sdk", "cli_json", ("gpt-5.5-thinking", "gpt-5.6-sol-high", "gpt-5.6-terra-high")),
        "grok": ConnectorSpec("grok", "acp", "headless_cli", ("grok-build",)),
        "kimi": ConnectorSpec("kimi", "direct_api", None, ("kimi-k3", "kimi-k2.7-code"), True, frozenset({"refine_bug"})),
        "zai": ConnectorSpec("zai", "direct_api", None, ("glm-5.2",), True, frozenset({"refine_ui", "ru"}), "ru_only"),
        "deepseek": ConnectorSpec("deepseek", "mmh_adapter", None, ("deepseek-v4-pro",), True, frozenset({"builder"})),
    }


class Connector:
    def __init__(
        self,
        spec: ConnectorSpec,
        surfaces: Sequence[Surface],
        vault: Vault,
        *,
        work_root: Path,
        degradation_threshold: int = 3,
        blocked_reason: str | None = None,
    ) -> None:
        self.spec = spec
        self.surfaces = tuple(surfaces)
        self.vault = vault
        self.work_root = work_root
        self.degradation_threshold = degradation_threshold
        self.blocked_reason = blocked_reason
        self._rate_failures = 0
        self._sessions: dict[str, Session] = {}

    def _surface(self, model: str | None = None) -> Surface | None:
        for surface in self.surfaces:
            if surface.auth is not AuthState.LOGGED_IN:
                continue
            if model is None or model in surface.grants:
                return surface
        return None

    def health(self) -> Health:
        if self.blocked_reason:
            return Health(self.spec.name, LaneHealth.BLOCKED, None, self.blocked_reason)
        if self._rate_failures >= self.degradation_threshold:
            degraded_surface = self._surface()
            return Health(self.spec.name, LaneHealth.DEGRADED, degraded_surface.name if degraded_surface else None, "sustained_rate_limit")
        surface = self._surface()
        if surface is None:
            return Health(self.spec.name, LaneHealth.UNAVAILABLE, None, "no_authenticated_surface")
        if self.spec.vault_required and self.vault.get(self.spec.name) is None:
            return Health(self.spec.name, LaneHealth.UNAVAILABLE, surface.name, "vault_credential_missing")
        return Health(self.spec.name, LaneHealth.AVAILABLE, surface.name)

    def open_session(self, agent: str) -> Session:
        surface = self._surface()
        if surface is None:
            raise ConnectorError("STATUS: unavailable:no_authenticated_surface", FailureClass.AUTH)
        session_id = uuid.uuid4().hex
        workdir = self.work_root / self.spec.name / agent / session_id
        session = Session(session_id, agent, workdir, surface)
        self._sessions[session_id] = session
        return session

    def resume(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            raise ConnectorError("session_not_found")
        return self._sessions[session_id]

    def cancel(self, session_id: str) -> None:
        session = self.resume(session_id)
        session.surface.cancel(session_id)
        session.cancelled = True

    def call(self, *, model: str, prompt: str, agent: str, session_id: str | None = None) -> NormalizedResponse:
        if self.blocked_reason:
            raise ConnectorError(f"STATUS: blocked:{self.blocked_reason}")
        if self.spec.eligible_agents is not None and agent not in self.spec.eligible_agents:
            reason = self.spec.eligibility_name or "agent_not_eligible"
            raise ConnectorError(f"eligibility:{reason}", FailureClass.ELIGIBILITY)
        if self.spec.vault_required and self.vault.get(self.spec.name) is None:
            raise ConnectorError("STATUS: unavailable:vault_credential_missing", FailureClass.AUTH)
        authenticated = any(surface.auth is AuthState.LOGGED_IN for surface in self.surfaces)
        surface = self._surface(model)
        if surface is None:
            if authenticated:
                raise ConnectorError(f"model_not_granted:{model}", FailureClass.MODEL_NOT_GRANTED)
            raise ConnectorError("STATUS: unavailable:no_authenticated_surface", FailureClass.AUTH)
        session = self.resume(session_id) if session_id else self.open_session(agent)
        try:
            payload = surface.invoke(model=model, prompt=prompt, session_id=session.session_id)
        except ConnectorError as exc:
            if exc.failure_class is FailureClass.RATE_LIMIT:
                self._rate_failures += 1
            raise
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ConnectorError("malformed_response", FailureClass.MALFORMED_RESPONSE)
        usage = payload.get("usage", "unreported")
        if usage != "unreported" and not isinstance(usage, Mapping):
            raise ConnectorError("malformed_usage", FailureClass.MALFORMED_RESPONSE)
        return normalize_response(ProviderRequest(self.spec.name, model, ({"role": "user", "content": prompt},)), payload)


__all__ = [
    "AuthState", "Connector", "ConnectorError", "ConnectorSpec", "FailureClass",
    "LaneHealth", "MemoryVault", "MockSurface", "all_connector_specs",
]
