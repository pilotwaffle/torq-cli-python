from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.application.context import GovernedContextInjector
from torq_cli.application.fleet import FleetProjector
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.application.supervisor import RunSupervisor, SupervisorState
from torq_cli.core.engine import NormalizedResponse, Provenance
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces.fleet_http import (
    FleetSessionManager,
    _fleet_event_id,
    create_fleet_server,
)
from torq_cli.safety.evidence_broker import BrokeredReceiptChain, EvidenceBroker
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store


class _Dispatcher:
    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse:
        del prompt
        body: Mapping[str, object]
        if role == "g1d":
            body = {"status": "design_complete"}
        elif role == "builder":
            body = {"status": "build_complete"}
        elif role == "g2a":
            body = {"verdict": "approve", "defects": []}
        else:
            body = {"verdict": "approve"}
        return NormalizedResponse(
            visible_text=json.dumps(body),
            reasoning_trace="",
            usage={"prompt_tokens": 1, "completion_tokens": 1},
            provenance=Provenance(provider, model, False),
        )


def _chain(tmp_path: Path, run_id: str) -> ReceiptChain:
    root = tmp_path / "evidence"
    return ReceiptChain(
        root,
        run_id,
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _request(
    server: object,
    method: str,
    path: str,
    cookie: str,
    payload: Mapping[str, Any] | None = None,
) -> tuple[int, Mapping[str, str], dict[str, Any]]:
    host, port = server.server_address[:2]  # type: ignore[attr-defined]
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Cookie": cookie}
    if body is not None:
        headers.update(
            {
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Length": str(len(body)),
            }
        )
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    result = (
        response.status,
        dict(response.getheaders()),
        json.loads(raw) if raw else {},
    )
    connection.close()
    return result


def _serve(server: object) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)  # type: ignore[attr-defined]
    thread.start()
    return thread


def _close(server: object, thread: threading.Thread) -> None:
    server.shutdown()  # type: ignore[attr-defined]
    server.server_close()  # type: ignore[attr-defined]
    thread.join(timeout=2)


def test_sse_event_identity_ignores_sliding_session_expiry() -> None:
    envelope: dict[str, Any] = {
        "snapshot": {"schema": "torq-fleet-snapshot-v3", "run": None},
        "annotations": [],
        "eligibility": {"context": {"eligible": False, "reason": "no_run"}},
        "pending": [],
        "session": {
            "write_capable": True,
            "read_only_reason": None,
            "expires_at": "2026-07-25T12:00:00Z",
        },
    }
    first = _fleet_event_id(envelope)
    envelope["session"]["expires_at"] = "2026-07-25T12:01:00Z"

    assert _fleet_event_id(envelope) == first
    envelope["pending"].append({"correlation_id": "command-1"})
    assert _fleet_event_id(envelope) != first


def test_action_route_decodes_one_canonical_opaque_id_segment(tmp_path: Path) -> None:
    class Projector:
        run_root = tmp_path / "run-colon-action"
        operational_state: Mapping[str, Any] = {}

        def snapshot(self) -> dict[str, Any]:
            return {
                "schema": "torq-fleet-snapshot-v3",
                "verification": {
                    "state": "live_verified",
                    "finding": None,
                    "covered_sequence": 1,
                    "manifest_generation": 1,
                },
                "data_status": "available",
                "run": {
                    "run_id": "run-colon-action",
                    "workflow_state": "action_open",
                },
                "summary": {"open_actions": 1},
                "lanes": [],
                "actions": [{
                    "action_id": "approval:1",
                    "state": "open",
                    "allowed_resolutions": ["approved"],
                    "outcome_map": {"approved": "completed"},
                }],
                "settlement": None,
            }

    class Resolver:
        root = Projector.run_root

        def __init__(self) -> None:
            self.action_id: str | None = None

        def resolve_action(
            self,
            *,
            action_id: str,
            resolution: str,
            resolver_identity: str,
        ) -> Mapping[str, Any]:
            assert resolution == "approved"
            assert resolver_identity == "operator:local-session"
            self.action_id = action_id
            return {"run_decision_sequence": 2}

    projector = Projector()
    projector.run_root.mkdir()
    resolver = Resolver()
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        projector,  # type: ignore[arg-type]
        port=0,
        action_resolver=resolver,
        sessions=sessions,
    )
    thread = _serve(server)
    try:
        status, _, body = _request(
            server,
            "POST",
            "/api/v1/fleet/actions/approval%3A1/resolve",
            cookie,
            {"correlation_id": "resolve-colon", "resolution": "approved"},
        )
    finally:
        _close(server, thread)

    assert status == 202
    assert body["status"] == "accepted"
    assert resolver.action_id == "approval:1"


def test_malformed_percent_encoded_action_path_returns_governed_404(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-malformed-action-path")
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(FleetProjector(chain.root), port=0, sessions=sessions)
    thread = _serve(server)
    try:
        status, _, body = _request(
            server,
            "POST",
            "/api/v1/fleet/actions/%FF/resolve",
            cookie,
        )
    finally:
        _close(server, thread)

    assert status == 404
    assert body == {"status": "not_found"}


def test_action_resolution_route_is_session_gated_atomic_and_sse_visible(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-action-http")
    profile = load_registry().profiles["torq-v5-6-live"]
    orchestrator = GovernedOrchestrator(
        _Dispatcher(),
        budget_usd=1,
        cost_ceiling_usd_by_role={role: 0.1 for role in profile.bindings},
    )
    orchestrator.execute(
        goal="Open one approval",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )
    injector = GovernedContextInjector(orchestrator, chain)
    action_id = str(FleetProjector(chain.root).snapshot()["actions"][0]["action_id"])
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        FleetProjector(chain.root),
        port=0,
        context_injector=injector,
        action_resolver=injector,
        sessions=sessions,
    )
    thread = _serve(server)
    try:
        status, headers, context = _request(
            server,
            "POST",
            "/api/v1/fleet/context",
            cookie,
            {
                "correlation_id": "context-1",
                "content": "Preserve this constraint if execution resumes",
            },
        )
        assert status == 202
        assert context["correlation_id"] == "context-1"
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, headers, body = _request(
            server,
            "POST",
            f"/api/v1/fleet/actions/{action_id}/resolve",
            cookie,
            {
                "correlation_id": "resolve-1",
                "resolution": "approved",
            },
        )
        assert status == 202
        assert body["result"]["status"] == "completed"
        rotated = headers["Set-Cookie"].split(";", 1)[0]
        assert verify_receipt_store(chain.root).status == "verified"

        host, port = server.server_address[:2]
        connection = http.client.HTTPConnection(host, port, timeout=10)
        connection.request(
            "GET",
            "/api/v1/fleet/events",
            headers={"Cookie": rotated, "Last-Event-ID": "untrusted-hint"},
        )
        response = connection.getresponse()
        event = response.read().decode("utf-8")
        connection.close()
        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        data_line = next(line for line in event.splitlines() if line.startswith("data: "))
        envelope = json.loads(data_line.removeprefix("data: "))
        assert envelope["snapshot"]["schema"] == "torq-fleet-snapshot-v3"
        assert envelope["snapshot"]["run"]["workflow_state"] == "closed"
    finally:
        _close(server, thread)


def test_recovery_requires_fresh_two_step_confirmation_bound_to_coverage(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-recovery-http")
    broker = EvidenceBroker(
        chain,
        allowed_roles=frozenset(
            {"orchestrator", "supervisor", "operator_gateway", "recovery"}
        ),
    )
    client = BrokeredReceiptChain(broker)
    attempt = {
        "role": "g1d",
        "attempt_id": "attempt-g1d-1",
        "attempt_ordinal": 1,
        "repair_cycle": 0,
        "provider_dispatch": False,
    }
    client.append("stage_attempt_created", attempt)
    state = SupervisorState(tmp_path / "supervisor.json", client.run_id)
    supervisor = RunSupervisor(broker, state)
    supervisor.mark_orphaned("g1d")

    class InterleavingRecovery:
        def __init__(self) -> None:
            self.armed = True

        @property
        def root(self) -> Path:
            return supervisor.root

        def abandon(
            self,
            attempt_ids: list[str],
            last_sequence: int,
            manifest_generation: int,
        ) -> Mapping[str, Any]:
            if self.armed:
                self.armed = False
                client.append("run_attested", {"checkpoint": "inside-recovery-race"})
            return supervisor.abandon(
                attempt_ids,
                last_sequence,
                manifest_generation,
            )

    interleaving_recovery = InterleavingRecovery()
    sessions = FleetSessionManager()
    cookie = f"torq_fleet_session={sessions.exchange(sessions.bootstrap_nonce)}"
    server = create_fleet_server(
        FleetProjector(chain.root),
        port=0,
        recovery_controller=interleaving_recovery,
        operational_state_provider=state.snapshot,
        sessions=sessions,
    )
    thread = _serve(server)
    try:
        status, headers, confirmation = _request(
            server,
            "POST",
            "/api/v1/fleet/recover/confirm",
            cookie,
            {"correlation_id": "recover-1"},
        )
        assert status == 200
        rotated = headers["Set-Cookie"].split(";", 1)[0]
        token = confirmation["confirmation_token"]

        client.append("run_attested", {"checkpoint": "coverage-advanced"})
        status, headers, stale = _request(
            server,
            "POST",
            "/api/v1/fleet/recover",
            rotated,
            {
                "correlation_id": "recover-1",
                "confirmation_token": token,
            },
        )
        assert status == 409
        assert stale["finding"] == "recovery_confirmation_stale"
        rotated = headers["Set-Cookie"].split(";", 1)[0]

        status, headers, confirmation = _request(
            server,
            "POST",
            "/api/v1/fleet/recover/confirm",
            rotated,
            {"correlation_id": "recover-1"},
        )
        assert status == 200
        rotated = headers["Set-Cookie"].split(";", 1)[0]
        token = confirmation["confirmation_token"]

        before_internal_race = chain.receipts_path.read_bytes()
        status, headers, stale = _request(
            server,
            "POST",
            "/api/v1/fleet/recover",
            rotated,
            {
                "correlation_id": "recover-1",
                "confirmation_token": token,
            },
        )
        assert status == 409
        assert stale["finding"] == "recovery_confirmation_stale"
        assert chain.receipts_path.read_bytes() != before_internal_race
        assert all(
            row["transition"] != "run_abandoned"
            for row in client.covered_receipts()
        )
        rotated = headers["Set-Cookie"].split(";", 1)[0]

        status, headers, confirmation = _request(
            server,
            "POST",
            "/api/v1/fleet/recover/confirm",
            rotated,
            {"correlation_id": "recover-1"},
        )
        assert status == 200
        rotated = headers["Set-Cookie"].split(";", 1)[0]
        token = confirmation["confirmation_token"]

        status, _, recovered = _request(
            server,
            "POST",
            "/api/v1/fleet/recover",
            rotated,
            {
                "correlation_id": "recover-1",
                "confirmation_token": token,
            },
        )
        assert status == 202
        assert recovered["result"]["payload"]["attempt_ids"] == [
            "attempt-g1d-1"
        ]
        assert verify_receipt_store(chain.root).status == "verified"
        assert FleetProjector(chain.root).snapshot()["run"]["workflow_state"] == (
            "abandoned"
        )
    finally:
        _close(server, thread)
