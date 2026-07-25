"""Capability-scoped serialization boundary for receipt writers."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from torq_cli.domain.evidence_transitions import transition_rule
from torq_cli.safety.receipts import ReceiptChain


@dataclass(frozen=True)
class BrokerCapability:
    token: str
    run_id: str
    writer_role: str
    process_id: int
    expires_at: float


@dataclass
class _CapabilityState:
    capability: BrokerCapability
    consumed: bool = False


class EvidenceBroker:
    """Own a chain and expose only process-bound, single-use append grants.

    Client writers receive opaque capabilities, never signing or encryption
    keys. The broker stamps the certified role from its capability registry and
    serializes every commit through one lock.
    """

    def __init__(
        self,
        chain: ReceiptChain,
        *,
        clock: Callable[[], float] = time.monotonic,
        allowed_roles: frozenset[str] = frozenset({"orchestrator"}),
    ) -> None:
        self.__chain = chain
        self._clock = clock
        self._capabilities: dict[str, _CapabilityState] = {}
        self._lock = RLock()
        self._allowed_roles = allowed_roles

    @property
    def run_id(self) -> str:
        return self.__chain.run_id

    @property
    def run_root(self) -> Path:
        return self.__chain.root

    @property
    def sequence(self) -> int:
        return self.__chain.sequence

    def issue(
        self,
        writer_role: str,
        *,
        ttl_seconds: float = 30.0,
    ) -> BrokerCapability:
        if writer_role not in {
            "orchestrator",
            "supervisor",
            "operator_gateway",
            "recovery",
        }:
            raise ValueError("broker_writer_role_invalid")
        if writer_role not in self._allowed_roles:
            raise PermissionError("broker_writer_role_unbound")
        if ttl_seconds <= 0 or ttl_seconds > 300:
            raise ValueError("broker_capability_ttl_invalid")
        capability = BrokerCapability(
            token=secrets.token_urlsafe(32),
            run_id=self.run_id,
            writer_role=writer_role,
            process_id=os.getpid(),
            expires_at=self._clock() + ttl_seconds,
        )
        with self._lock:
            self._capabilities[capability.token] = _CapabilityState(capability)
        return capability

    def append(
        self,
        token: str,
        transition: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        caller = os.getpid()
        with self._lock:
            state = self._capabilities.get(token)
            if state is None:
                raise PermissionError("broker_capability_unknown")
            capability = state.capability
            if state.consumed:
                raise PermissionError("broker_capability_replayed")
            if self._clock() >= capability.expires_at:
                state.consumed = True
                raise PermissionError("broker_capability_expired")
            if capability.run_id != self.run_id or capability.process_id != caller:
                raise PermissionError("broker_capability_binding_invalid")
            decision = payload.get("decision") if transition == "run_decision" else None
            rule = transition_rule(capability.writer_role, transition, decision)
            if rule is None and not (
                capability.writer_role == "orchestrator"
            ):
                raise PermissionError("broker_transition_unauthorized")
            state.consumed = True
            return self.__chain.append(
                transition,
                payload,
                writer_role=capability.writer_role,
                evidence_basis=(rule.evidence_basis if rule is not None else None),
            )

    def seal(self) -> Path:
        with self._lock:
            return self.__chain.seal()

    def write_artifact(
        self,
        token: str,
        name: str,
        content: str,
    ) -> Path:
        caller = os.getpid()
        with self._lock:
            state = self._capabilities.get(token)
            if state is None:
                raise PermissionError("broker_capability_unknown")
            capability = state.capability
            if state.consumed:
                raise PermissionError("broker_capability_replayed")
            if self._clock() >= capability.expires_at:
                state.consumed = True
                raise PermissionError("broker_capability_expired")
            if capability.run_id != self.run_id or capability.process_id != caller:
                raise PermissionError("broker_capability_binding_invalid")
            if capability.writer_role not in {"orchestrator", "operator_gateway"}:
                raise PermissionError("broker_artifact_unauthorized")
            state.consumed = True
            return self.__chain.write_artifact(name, content)

    def read_artifact(self, path: Path) -> str:
        with self._lock:
            return self.__chain.read_artifact(path)

    def terminalize(
        self,
        token: str,
        transition: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        caller = os.getpid()
        with self._lock:
            state = self._capabilities.get(token)
            if state is None:
                raise PermissionError("broker_capability_unknown")
            capability = state.capability
            if state.consumed:
                raise PermissionError("broker_capability_replayed")
            if self._clock() >= capability.expires_at:
                state.consumed = True
                raise PermissionError("broker_capability_expired")
            if capability.run_id != self.run_id or capability.process_id != caller:
                raise PermissionError("broker_capability_binding_invalid")
            decision = payload.get("decision") if transition == "run_decision" else None
            rule = transition_rule(capability.writer_role, transition, decision)
            if rule is None:
                raise PermissionError("broker_transition_unauthorized")
            state.consumed = True
            return self.__chain.terminalize(
                transition,
                payload,
                writer_role=capability.writer_role,
                evidence_basis=rule.evidence_basis,
            )

    def resolve_action(
        self,
        token: str,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]:
        """Consume one operator capability for the whole action transaction."""
        caller = os.getpid()
        with self._lock:
            state = self._capabilities.get(token)
            if state is None:
                raise PermissionError("broker_capability_unknown")
            capability = state.capability
            if state.consumed:
                raise PermissionError("broker_capability_replayed")
            if self._clock() >= capability.expires_at:
                state.consumed = True
                raise PermissionError("broker_capability_expired")
            if capability.run_id != self.run_id or capability.process_id != caller:
                raise PermissionError("broker_capability_binding_invalid")
            if capability.writer_role != "operator_gateway":
                raise PermissionError("broker_transition_unauthorized")
            state.consumed = True
            return self.__chain.resolve_action(
                action_id=action_id,
                resolution=resolution,
                resolver_identity=resolver_identity,
            )

    def covered_receipts(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            return self.__chain.covered_receipts()


class BrokeredReceiptChain:
    """ReceiptChain-compatible client facade with no key-bearing attributes."""

    def __init__(
        self,
        broker: EvidenceBroker,
        *,
        writer_role: str = "orchestrator",
    ) -> None:
        self.__broker = broker
        self.__writer_role = writer_role

    @property
    def run_id(self) -> str:
        return self.__broker.run_id

    @property
    def root(self) -> Path:
        return self.__broker.run_root

    @property
    def receipts_path(self) -> Path:
        return self.root / "receipts.jsonl"

    @property
    def sequence(self) -> int:
        return self.__broker.sequence

    @staticmethod
    def hash_file(path: Path) -> str:
        return ReceiptChain.hash_file(path)

    def append(
        self,
        transition: str,
        payload: Mapping[str, Any],
        *,
        writer_role: str = "orchestrator",
        evidence_basis: str | None = None,
    ) -> dict[str, Any]:
        if writer_role != self.__writer_role:
            raise PermissionError("broker_writer_role_unbound")
        decision = payload.get("decision") if transition == "run_decision" else None
        rule = transition_rule(writer_role, transition, decision)
        if (
            evidence_basis is not None
            and rule is not None
            and evidence_basis != rule.evidence_basis
        ):
            raise ValueError("receipt_writer_unauthorized")
        capability = self.__broker.issue(writer_role)
        return self.__broker.append(capability.token, transition, payload)

    def write_artifact(self, name: str, content: str) -> Path:
        capability = self.__broker.issue(self.__writer_role)
        return self.__broker.write_artifact(capability.token, name, content)

    def read_artifact(self, path: Path) -> str:
        if self.__writer_role != "orchestrator":
            raise PermissionError("broker_artifact_read_unauthorized")
        return self.__broker.read_artifact(path)

    def covered_receipts(self) -> tuple[dict[str, Any], ...]:
        if self.__writer_role != "orchestrator":
            raise PermissionError("broker_receipt_read_unauthorized")
        return self.__broker.covered_receipts()

    def terminalize(
        self,
        transition: str,
        payload: Mapping[str, Any],
        *,
        writer_role: str = "orchestrator",
        evidence_basis: str | None = None,
    ) -> dict[str, Any]:
        if writer_role != self.__writer_role:
            raise PermissionError("broker_writer_role_unbound")
        decision = payload.get("decision") if transition == "run_decision" else None
        rule = transition_rule(writer_role, transition, decision)
        if rule is None:
            raise PermissionError("broker_transition_unauthorized")
        if evidence_basis is not None and evidence_basis != rule.evidence_basis:
            raise ValueError("receipt_writer_unauthorized")
        capability = self.__broker.issue(writer_role)
        return self.__broker.terminalize(capability.token, transition, payload)

    def seal(self) -> Path:
        if self.__writer_role != "orchestrator":
            raise PermissionError("broker_seal_unauthorized")
        value = self.__broker.seal()
        if not isinstance(value, Path):
            raise TypeError("broker_manifest_path_invalid")
        return value

    def resolve_action(
        self,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]:
        if self.__writer_role != "operator_gateway":
            raise PermissionError("broker_writer_role_unbound")
        capability = self.__broker.issue("operator_gateway")
        return self.__broker.resolve_action(
            capability.token,
            action_id=action_id,
            resolution=resolution,
            resolver_identity=resolver_identity,
        )


__all__ = ["BrokerCapability", "BrokeredReceiptChain", "EvidenceBroker"]
