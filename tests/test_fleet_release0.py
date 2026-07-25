from __future__ import annotations

import hashlib
import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from torq_cli.application.fleet import FleetProjector
from torq_cli.application.supervisor import RunSupervisor, SupervisorState
from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    transition_authority_finding,
)
from torq_cli.interfaces.fleet_http import FleetSessionManager, create_fleet_server
from torq_cli.safety.evidence_broker import BrokeredReceiptChain, EvidenceBroker
import torq_cli.safety.evidence_broker as evidence_broker_module
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    MemoryRunKeyStore,
    ReceiptChain,
    verify_receipt_store,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _chain(
    root: Path,
    run_id: str,
    *,
    observer: object | None = None,
) -> ReceiptChain:
    return ReceiptChain(
        root,
        run_id,
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
        commit_observer=observer if callable(observer) else None,
    )


def _attempt() -> dict[str, object]:
    return {
        "role": "g1d",
        "attempt_id": "attempt-g1d-1",
        "attempt_ordinal": 1,
        "repair_cycle": 0,
        "provider_dispatch": False,
    }


def test_canonical_oracle_and_all_signed_files_share_one_encoding(tmp_path: Path) -> None:
    oracle_path = files("torq_cli").joinpath("data/oracles/canonical-json.v1.json")
    oracle = json.loads(oracle_path.read_text(encoding="utf-8"))
    encoded = _canonical(oracle["value"])

    assert encoded.decode("ascii") == oracle["canonical_ascii"]
    assert hashlib.sha256(encoded).hexdigest() == oracle["sha256"]

    chain = _chain(tmp_path / "evidence", "run-canonical")
    chain.append("run_attested", {"summary": "café ✓"})
    chain.seal()
    certificate = chain.certificate_path.read_bytes()
    manifest_path = chain.root / "terminal-manifest.json"
    manifest = manifest_path.read_bytes()

    assert certificate == _canonical(json.loads(certificate))
    assert manifest == _canonical(json.loads(manifest))
    manifest_value = json.loads(manifest)
    assert manifest_value["certificate_hash"] == ReceiptChain.hash_file(
        chain.certificate_path
    )


def test_artifacts_are_aead_authenticated_and_run_bound(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    first = _chain(root, "run-a")
    second = _chain(root, "run-b")
    artifact = first.write_artifact("design", "governed output")

    assert artifact.read_bytes().startswith(b"TORQAEAD1")
    assert first.read_artifact(artifact) == "governed output"

    transplanted = second.root / "artifacts" / "design.enc"
    transplanted.parent.mkdir(parents=True)
    transplanted.write_bytes(artifact.read_bytes())
    with pytest.raises(InvalidTag):
        second.read_artifact(transplanted)


def test_machine_readable_matrix_rejects_every_wrong_basis() -> None:
    assert len(TRANSITION_RULES) == len(
        {(rule.writer_role, rule.transition) for rule in TRANSITION_RULES}
    )
    for rule in TRANSITION_RULES:
        payload = (
            {"status": sorted(rule.permitted_statuses)[0]}
            if rule.permitted_statuses is not None
            else {}
        )
        assert (
            transition_authority_finding(
                rule.writer_role,
                rule.transition,
                rule.evidence_basis,
                payload,
            )
            is None
        )
        wrong = "submitted" if rule.evidence_basis != "submitted" else "observed"
        assert transition_authority_finding(
            rule.writer_role,
            rule.transition,
            wrong,
            payload,
        ) == "receipt_writer_unauthorized"


def test_broker_capabilities_are_process_bound_single_use_and_expiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [10.0]
    chain = ReceiptChain(
        tmp_path,
        "run-broker",
        MemoryRunKeyStore(),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    broker = EvidenceBroker(chain, clock=lambda: clock[0])
    client = BrokeredReceiptChain(broker)
    monkeypatch.setattr(evidence_broker_module.os, "getpid", lambda: 1234)
    capability = broker.issue("orchestrator")

    assert not hasattr(client, "key")
    assert not hasattr(client, "run_keys")
    with pytest.raises(PermissionError, match="broker_writer_role_unbound"):
        client.append(
            "stage_interrupted",
            {**_attempt(), "provider_dispatch": "unknown"},
            writer_role="supervisor",
            evidence_basis="derived",
        )
    monkeypatch.setattr(evidence_broker_module.os, "getpid", lambda: 5678)
    with pytest.raises(PermissionError, match="broker_capability_binding_invalid"):
        broker.append(capability.token, "run_attested", {"mode": "live"})
    monkeypatch.setattr(evidence_broker_module.os, "getpid", lambda: 1234)
    receipt = broker.append(capability.token, "run_attested", {"mode": "live"})
    assert receipt["writer_role"] == "orchestrator"
    with pytest.raises(PermissionError, match="broker_capability_replayed"):
        broker.append(capability.token, "run_attested", {"mode": "live"})

    expired = broker.issue("orchestrator", ttl_seconds=1)
    clock[0] = 11.0
    with pytest.raises(PermissionError, match="broker_capability_expired"):
        broker.append(expired.token, "run_attested", {"mode": "live"})

    privileged_broker = EvidenceBroker(
        chain,
        clock=lambda: clock[0],
        allowed_roles=frozenset({"supervisor"}),
    )
    supervisor = privileged_broker.issue("supervisor")
    with pytest.raises(PermissionError, match="broker_transition_unauthorized"):
        privileged_broker.append(
            supervisor.token,
            "stage_completed",
            {**_attempt(), "provider_dispatch": True},
        )


def test_broker_serializes_concurrent_multi_writer_appends(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-concurrent")
    broker = EvidenceBroker(
        chain,
        allowed_roles=frozenset(
            {"orchestrator", "supervisor", "operator_gateway"}
        ),
    )
    client = BrokeredReceiptChain(broker)
    client.append("stage_attempt_created", _attempt())

    def append_orchestrator() -> dict[str, object]:
        capability = broker.issue("orchestrator")
        return broker.append(capability.token, "run_attested", {"mode": "live"})

    def append_operator() -> dict[str, object]:
        capability = broker.issue("operator_gateway")
        return broker.append(
            capability.token,
            "context_injected",
            {"context_id": "ctx-1", "target_role": "lead"},
        )

    def append_supervisor() -> dict[str, object]:
        capability = broker.issue("supervisor")
        return broker.append(
            capability.token,
            "stage_interrupted",
            {**_attempt(), "provider_dispatch": "unknown"},
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(
            pool.map(
                lambda function: function(),
                (append_orchestrator, append_operator, append_supervisor),
            )
        )

    assert sorted(int(row["sequence"]) for row in rows) == [2, 3, 4]
    assert verify_receipt_store(chain.root).status == "verified"
    manifest = json.loads(
        (chain.root / "terminal-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["receipt_count"] == 4


@pytest.mark.parametrize(
    "crash_step",
    ("receipt_fsynced", "temporary_fsynced", "replaced", "directory_fsynced"),
)
def test_crash_boundaries_never_read_as_tampered(
    tmp_path: Path,
    crash_step: str,
) -> None:
    armed = [False]

    def observer(step: str) -> None:
        if armed[0] and step == crash_step:
            raise RuntimeError("simulated_crash")

    chain = _chain(tmp_path / crash_step, "run", observer=observer)
    chain.append("run_attested", {"ordinal": 1})
    armed[0] = True
    with pytest.raises(RuntimeError, match="simulated_crash"):
        chain.append("run_attested", {"ordinal": 2})

    result = verify_receipt_store(chain.root)
    assert result.status in {"verified", "live_catching_up"}
    assert result.status != "tampered"


def test_manifest_covered_prefix_excludes_uncovered_receipt(tmp_path: Path) -> None:
    armed = [False]

    def observer(step: str) -> None:
        if armed[0] and step == "receipt_fsynced":
            raise RuntimeError("simulated_crash")

    chain = _chain(tmp_path / "evidence", "run-prefix", observer=observer)
    chain.append("run_attested", {"ordinal": 1})
    armed[0] = True
    with pytest.raises(RuntimeError):
        chain.append("run_attested", {"ordinal": 2})

    verification = verify_receipt_store(chain.root)
    snapshot = FleetProjector(chain.root).snapshot()

    assert verification.status == "live_catching_up"
    assert snapshot["verification"]["status"] == "live_catching_up"
    assert snapshot["run"]["receipt_count"] == 1


def test_double_death_stays_running_until_recovery_seals_abandonment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    key_chain = _chain(root, "run-recovery")
    broker = EvidenceBroker(
        key_chain,
        allowed_roles=frozenset({"orchestrator", "supervisor", "recovery"}),
    )
    client = BrokeredReceiptChain(broker)
    client.append("stage_attempt_created", _attempt())
    state = SupervisorState(tmp_path / "supervisor.json", client.run_id)
    supervisor = RunSupervisor(broker, state)
    supervisor.mark_orphaned("g1d")

    live = FleetProjector(
        client.root,
        operational_state=state.snapshot(),
    ).snapshot()
    assert live["lanes"][0]["state"] == "running"
    assert live["run"]["operational_annotations"] == {
        "orphaned_roles": ["g1d"],
        "recovery_required": True,
    }

    supervisor.abandon(["attempt-g1d-1"], client.sequence)
    assert verify_receipt_store(client.root).status == "verified"
    abandoned = FleetProjector(client.root).snapshot()
    assert abandoned["lanes"][0]["state"] == "abandoned"
    assert abandoned["run"]["workflow_state"] == "abandoned"


def test_http_bootstrap_is_single_use_and_all_run_reads_require_session(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "evidence", "run-http-auth")
    chain.append("run_attested", {"mode": "dry_run"})
    chain.seal()
    server = create_fleet_server(FleetProjector(chain.root), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    nonce = str(getattr(server, "fleet_bootstrap_nonce"))
    try:
        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/healthz")
        health = connection.getresponse()
        assert health.status == 200
        assert json.loads(health.read()) == {"status": "ok"}
        connection.close()

        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", "/api/v1/fleet")
        denied = connection.getresponse()
        assert denied.status == 401
        denied.read()
        connection.close()

        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", f"/bootstrap?nonce={nonce}")
        bootstrap = connection.getresponse()
        assert bootstrap.status == 303
        assert bootstrap.getheader("Location") == "/"
        cookie = str(bootstrap.getheader("Set-Cookie")).partition(";")[0]
        bootstrap.read()
        connection.close()

        connection = http.client.HTTPConnection(host, port)
        connection.request("GET", f"/bootstrap?nonce={nonce}")
        replay = connection.getresponse()
        assert replay.status == 403
        replay.read()
        connection.close()

        connection = http.client.HTTPConnection(host, port)
        connection.request(
            "GET",
            "/api/v1/fleet",
            headers={"Cookie": cookie},
        )
        allowed = connection.getresponse()
        assert allowed.status == 200
        assert allowed.getheader("Referrer-Policy") == "no-referrer"
        assert json.loads(allowed.read())["verification"]["status"] == "verified"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_fleet_sessions_expire_and_rotate() -> None:
    clock = [100.0]
    sessions = FleetSessionManager(
        clock=lambda: clock[0],
        idle_seconds=10,
        absolute_seconds=30,
    )
    token = sessions.exchange(sessions.bootstrap_nonce)
    session = sessions.authenticate(f"torq_fleet_session={token}")
    assert session is not None
    rotated = sessions.rotate(session)
    assert sessions.authenticate(f"torq_fleet_session={token}") is None
    assert sessions.authenticate(f"torq_fleet_session={rotated}") is not None
    clock[0] = 110.0
    assert sessions.authenticate(f"torq_fleet_session={rotated}") is None
