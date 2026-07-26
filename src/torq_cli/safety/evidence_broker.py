"""Capability-scoped serialization boundary for receipt writers."""

from __future__ import annotations

import os
import multiprocessing
import secrets
import socket
import struct
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from multiprocessing.connection import Client, Connection, Listener
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any

from torq_cli.domain.evidence_transitions import (
    CURRENT_AUDIT_TRANSITIONS,
    transition_rule,
)
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


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
        _process_id: int | None = None,
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
            process_id=os.getpid() if _process_id is None else _process_id,
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
        *,
        _caller_pid: int | None = None,
    ) -> dict[str, Any]:
        caller = os.getpid() if _caller_pid is None else _caller_pid
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
                and transition in CURRENT_AUDIT_TRANSITIONS
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
        *,
        _caller_pid: int | None = None,
    ) -> Path:
        caller = os.getpid() if _caller_pid is None else _caller_pid
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
        *,
        expected_sequence: int | None = None,
        expected_manifest_generation: int | None = None,
        _caller_pid: int | None = None,
    ) -> dict[str, Any]:
        caller = os.getpid() if _caller_pid is None else _caller_pid
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
                expected_sequence=expected_sequence,
                expected_manifest_generation=expected_manifest_generation,
            )

    def resolve_action(
        self,
        token: str,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
        _caller_pid: int | None = None,
    ) -> Mapping[str, Any]:
        """Consume one operator capability for the whole action transaction."""
        caller = os.getpid() if _caller_pid is None else _caller_pid
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


@dataclass(frozen=True)
class BrokerEndpoint:
    """Authenticated local endpoint safe to hand to an unprivileged client."""

    address: str
    family: str
    authkey: bytes


def _connection_peer_pid(connection: Connection) -> int:
    """Return the kernel-authenticated PID at the local IPC endpoint."""
    handle = connection.fileno()
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_id = wintypes.ULONG()
        win_dll = getattr(ctypes, "WinDLL", None)
        get_last_error = getattr(ctypes, "get_last_error", None)
        if not callable(win_dll) or not callable(get_last_error):
            raise OSError("broker_peer_identity_unavailable")
        kernel32 = win_dll("kernel32", use_last_error=True)
        function = kernel32.GetNamedPipeClientProcessId
        function.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG))
        function.restype = wintypes.BOOL
        if not function(wintypes.HANDLE(handle), ctypes.byref(process_id)):
            raise OSError(get_last_error(), "broker_peer_identity_failed")
        return int(process_id.value)

    duplicate = socket.fromfd(
        handle,
        getattr(socket, "AF_UNIX"),
        socket.SOCK_STREAM,
    )
    try:
        if hasattr(socket, "SO_PEERCRED"):
            credentials = duplicate.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            process_id, _uid, _gid = struct.unpack("3i", credentials)
            return int(process_id)
        if hasattr(socket, "LOCAL_PEERPID") and hasattr(socket, "SOL_LOCAL"):
            value = duplicate.getsockopt(
                getattr(socket, "SOL_LOCAL"),
                getattr(socket, "LOCAL_PEERPID"),
                struct.calcsize("i"),
            )
            return int(struct.unpack("i", value)[0])
    finally:
        duplicate.close()
    raise OSError("broker_peer_identity_unavailable")


class EvidenceBrokerServer:
    """Authenticated named-pipe/UDS server around a key-owning broker.

    The listener authenticates possession of a random per-server secret before
    deserializing requests.  Every capability is additionally bound to the
    client PID reported by the kernel, not a PID supplied in the message.
    """

    def __init__(self, broker: EvidenceBroker) -> None:
        self._broker = broker
        self._stopped = Event()
        authkey = secrets.token_bytes(32)
        if os.name == "nt":
            address = rf"\\.\pipe\torq-evidence-{uuid.uuid4().hex}"
            family = "AF_PIPE"
        else:
            address = str(broker.run_root / f".broker-{uuid.uuid4().hex}.sock")
            family = "AF_UNIX"
        self.endpoint = BrokerEndpoint(address, family, authkey)
        self._listener = Listener(address, family=family, authkey=authkey)
        if family == "AF_UNIX":
            os.chmod(address, 0o600)
        self._thread: Thread | None = None

    def start(self) -> BrokerEndpoint:
        if self._thread is not None:
            raise RuntimeError("broker_server_already_started")
        self._thread = Thread(
            target=self.serve_forever,
            name="torq-evidence-broker",
            daemon=True,
        )
        self._thread.start()
        return self.endpoint

    def serve_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                connection = self._listener.accept()
            except (OSError, EOFError):
                if self._stopped.is_set():
                    return
                raise
            try:
                peer_pid = _connection_peer_pid(connection)
                request = connection.recv()
                response = self._dispatch(request, peer_pid)
            except Exception as exc:  # authenticated RPC error envelope
                response = {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            try:
                connection.send(response)
            finally:
                connection.close()

    def _dispatch(self, request: object, peer_pid: int) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {"operation", "args"}:
            raise ValueError("broker_request_invalid")
        operation = request.get("operation")
        arguments = request.get("args")
        if not isinstance(operation, str) or not isinstance(arguments, dict):
            raise ValueError("broker_request_invalid")
        value: Any
        if operation == "issue":
            value = self._broker.issue(
                arguments["writer_role"],
                ttl_seconds=arguments.get("ttl_seconds", 30.0),
                _process_id=peer_pid,
            )
        elif operation == "append":
            value = self._broker.append(
                arguments["token"],
                arguments["transition"],
                arguments["payload"],
                _caller_pid=peer_pid,
            )
        elif operation == "terminalize":
            value = self._broker.terminalize(
                arguments["token"],
                arguments["transition"],
                arguments["payload"],
                expected_sequence=arguments.get("expected_sequence"),
                expected_manifest_generation=arguments.get(
                    "expected_manifest_generation"
                ),
                _caller_pid=peer_pid,
            )
        elif operation == "write_artifact":
            value = self._broker.write_artifact(
                arguments["token"],
                arguments["name"],
                arguments["content"],
                _caller_pid=peer_pid,
            )
        elif operation == "read_artifact":
            value = self._broker.read_artifact(Path(arguments["path"]))
        elif operation == "resolve_action":
            value = self._broker.resolve_action(
                arguments["token"],
                action_id=arguments["action_id"],
                resolution=arguments["resolution"],
                resolver_identity=arguments["resolver_identity"],
                _caller_pid=peer_pid,
            )
        elif operation == "seal":
            value = self._broker.seal()
        elif operation == "covered_receipts":
            value = self._broker.covered_receipts()
        elif operation == "metadata":
            value = {
                "run_id": self._broker.run_id,
                "run_root": str(self._broker.run_root),
                "sequence": self._broker.sequence,
            }
        elif operation == "shutdown":
            self._stopped.set()
            value = None
        else:
            raise ValueError("broker_operation_invalid")
        return {"ok": True, "value": value}

    def close(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self._listener.close()
        if self.endpoint.family == "AF_UNIX":
            Path(self.endpoint.address).unlink(missing_ok=True)


class RemoteEvidenceBroker:
    """EvidenceBroker-compatible authenticated IPC client."""

    def __init__(self, endpoint: BrokerEndpoint) -> None:
        self._endpoint = endpoint

    def _call(self, operation: str, **arguments: Any) -> Any:
        connection = Client(
            self._endpoint.address,
            family=self._endpoint.family,
            authkey=self._endpoint.authkey,
        )
        try:
            connection.send({"operation": operation, "args": arguments})
            response = connection.recv()
        finally:
            connection.close()
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise RuntimeError("broker_response_invalid")
        if response["ok"]:
            return response.get("value")
        error = str(response.get("error", "broker_remote_error"))
        error_type = response.get("error_type")
        if error_type == "PermissionError":
            raise PermissionError(error)
        if error_type == "ValueError":
            raise ValueError(error)
        raise RuntimeError(error)

    @property
    def run_id(self) -> str:
        return str(self._call("metadata")["run_id"])

    @property
    def run_root(self) -> Path:
        return Path(self._call("metadata")["run_root"])

    @property
    def sequence(self) -> int:
        return int(self._call("metadata")["sequence"])

    def issue(self, writer_role: str, *, ttl_seconds: float = 30.0) -> BrokerCapability:
        value = self._call(
            "issue", writer_role=writer_role, ttl_seconds=ttl_seconds
        )
        if not isinstance(value, BrokerCapability):
            raise TypeError("broker_capability_invalid")
        return value

    def append(self, token: str, transition: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return dict(
            self._call(
                "append", token=token, transition=transition, payload=dict(payload)
            )
        )

    def terminalize(
        self,
        token: str,
        transition: str,
        payload: Mapping[str, Any],
        *,
        expected_sequence: int | None = None,
        expected_manifest_generation: int | None = None,
    ) -> dict[str, Any]:
        return dict(
            self._call(
                "terminalize",
                token=token,
                transition=transition,
                payload=dict(payload),
                expected_sequence=expected_sequence,
                expected_manifest_generation=expected_manifest_generation,
            )
        )

    def write_artifact(self, token: str, name: str, content: str) -> Path:
        return Path(
            self._call(
                "write_artifact", token=token, name=name, content=content
            )
        )

    def read_artifact(self, path: Path) -> str:
        return str(self._call("read_artifact", path=str(path)))

    def resolve_action(
        self,
        token: str,
        *,
        action_id: str,
        resolution: str,
        resolver_identity: str,
    ) -> Mapping[str, Any]:
        return dict(
            self._call(
                "resolve_action",
                token=token,
                action_id=action_id,
                resolution=resolution,
                resolver_identity=resolver_identity,
            )
        )

    def seal(self) -> Path:
        return Path(self._call("seal"))

    def covered_receipts(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in self._call("covered_receipts"))

    def shutdown(self) -> None:
        self._call("shutdown")


def _broker_process_main(
    ready: Connection,
    evidence_root: str,
    run_id: str,
    profile_version: str,
    policy_version: str,
    allowed_roles: tuple[str, ...],
) -> None:
    """Spawn-safe entry point: construct all key-bearing objects in the child."""
    server: EvidenceBrokerServer | None = None
    try:
        root = Path(evidence_root)
        chain = ReceiptChain(
            root,
            run_id,
            # The process handoff contains configuration only.  Only this
            # broker child instantiates the keystore and opens key material.
            FileRunKeyStore(root),
            profile_version=profile_version,
            policy_version=policy_version,
        )
        broker = EvidenceBroker(chain, allowed_roles=frozenset(allowed_roles))
        server = EvidenceBrokerServer(broker)
        ready.send({"ok": True, "endpoint": server.endpoint})
        ready.close()
        server.serve_forever()
    except Exception as exc:
        try:
            ready.send(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        except (BrokenPipeError, EOFError, OSError):
            pass
        raise
    finally:
        ready.close()
        if server is not None:
            server.close()


class EvidenceBrokerProcess:
    """Lifecycle owner for a dedicated key-bearing broker process."""

    def __init__(self, process: Any, client: RemoteEvidenceBroker) -> None:
        self._process = process
        self.client = client
        self._closed = False

    @classmethod
    def start(
        cls,
        evidence_root: Path,
        run_id: str,
        *,
        profile_version: str,
        policy_version: str,
        allowed_roles: frozenset[str],
        startup_timeout: float = 20.0,
    ) -> EvidenceBrokerProcess:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_broker_process_main,
            args=(
                sender,
                str(evidence_root),
                run_id,
                profile_version,
                policy_version,
                tuple(sorted(allowed_roles)),
            ),
            name=f"torq-evidence-{run_id}",
            daemon=True,
        )
        process.start()
        sender.close()
        try:
            if not receiver.poll(startup_timeout):
                process.terminate()
                process.join(timeout=5)
                raise TimeoutError("broker_process_start_timeout")
            response = receiver.recv()
        finally:
            receiver.close()
        if not isinstance(response, dict) or response.get("ok") is not True:
            process.join(timeout=5)
            error = response.get("error") if isinstance(response, dict) else None
            raise RuntimeError(str(error or "broker_process_start_failed"))
        endpoint = response.get("endpoint")
        if not isinstance(endpoint, BrokerEndpoint):
            process.terminate()
            process.join(timeout=5)
            raise RuntimeError("broker_process_endpoint_invalid")
        return cls(process, RemoteEvidenceBroker(endpoint))

    @property
    def process_id(self) -> int:
        if self._process.pid is None:
            raise RuntimeError("broker_process_not_started")
        return int(self._process.pid)

    @property
    def is_alive(self) -> bool:
        return bool(self._process.is_alive())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._process.is_alive():
            try:
                self.client.shutdown()
            except (EOFError, OSError, RuntimeError):
                pass
            self._process.join(timeout=10)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5)

    def __enter__(self) -> EvidenceBrokerProcess:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class BrokeredReceiptChain:
    """ReceiptChain-compatible client facade with no key-bearing attributes."""

    def __init__(
        self,
        broker: EvidenceBroker | RemoteEvidenceBroker,
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
        expected_sequence: int | None = None,
        expected_manifest_generation: int | None = None,
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
        return self.__broker.terminalize(
            capability.token,
            transition,
            payload,
            expected_sequence=expected_sequence,
            expected_manifest_generation=expected_manifest_generation,
        )

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


__all__ = [
    "BrokerCapability",
    "BrokerEndpoint",
    "BrokeredReceiptChain",
    "EvidenceBroker",
    "EvidenceBrokerProcess",
    "EvidenceBrokerServer",
    "RemoteEvidenceBroker",
]
