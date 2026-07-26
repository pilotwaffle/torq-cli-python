from __future__ import annotations

import hashlib
import http.client
import json
import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from torq_cli.application.fleet import FleetProjector
from torq_cli.application.fleet_controls import FleetControlService
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.application.supervisor import RunSupervisor, SupervisorState
from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    transition_authority_finding,
)
from torq_cli.interfaces.fleet_http import FleetSessionManager, create_fleet_server
import torq_cli.safety.evidence_broker as evidence_broker_module
from torq_cli.safety.evidence_broker import (
    BrokerEndpoint,
    BrokeredReceiptChain,
    EvidenceBroker,
    EvidenceBrokerServer,
    RemoteEvidenceBroker,
)
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    MemoryRunKeyStore,
    ReceiptChain,
    _atomic_write,
    verify_receipt_store,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _use_foreign_broker_capability(
    endpoint: BrokerEndpoint,
    token: str,
    result: multiprocessing.Queue[str],
) -> None:
    try:
        RemoteEvidenceBroker(endpoint).append(
            token,
            "run_attested",
            {"mode": "dry_run"},
        )
    except Exception as exc:
        result.put(str(exc))
    else:
        result.put("unexpected-success")


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
    certificate_value = json.loads(certificate)
    manifest_value = json.loads(manifest)
    assert certificate_value["canonical_encoding_marker"] == "UTF-8 ✓"
    assert manifest_value["canonical_encoding_marker"] == "UTF-8 ✓"
    assert b'"canonical_encoding_marker":"UTF-8 \\u2713"' in certificate
    assert b'"canonical_encoding_marker":"UTF-8 \\u2713"' in manifest
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


def test_atomic_binary_write_preserves_ciphertext_newlines(tmp_path: Path) -> None:
    payload = b"cipher-prefix\x00\n\r\n\xffcipher-suffix"
    target = tmp_path / "artifact.enc"

    _atomic_write(target, payload)

    assert target.read_bytes() == payload


def test_machine_readable_matrix_rejects_every_wrong_basis() -> None:
    assert len(TRANSITION_RULES) == len({
        (rule.writer_role, rule.transition, rule.decision_value)
        for rule in TRANSITION_RULES
    })
    for rule in TRANSITION_RULES:
        payload = (
            {"decision": rule.decision_value}
            if rule.decision_value is not None
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


def test_authenticated_local_broker_transport_binds_capability_to_peer_pid(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-broker-ipc")
    server = EvidenceBrokerServer(EvidenceBroker(chain))
    endpoint = server.start()
    remote = RemoteEvidenceBroker(endpoint)
    try:
        capability = remote.issue("orchestrator")
        assert capability.process_id == os.getpid()

        context = multiprocessing.get_context("spawn")
        result = context.Queue()
        process = context.Process(
            target=_use_foreign_broker_capability,
            args=(endpoint, capability.token, result),
        )
        process.start()
        process.join(timeout=15)
        assert process.exitcode == 0
        assert result.get(timeout=2) == "broker_capability_binding_invalid"

        receipt = remote.append(
            capability.token,
            "run_attested",
            {"mode": "dry_run"},
        )
        assert receipt["writer_role"] == "orchestrator"
        assert remote.sequence == 1
    finally:
        server.close()


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
                {
                    **_attempt(),
                    "provider_dispatch": "unknown",
                    "observation_source": "worker_exit",
                },
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
    assert result.status in {"verified", "live_catching_up", "incomplete"}
    assert result.status != "tampered"
    if result.status == "incomplete":
        assert result.finding == "manifest_anchor_update_pending"


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
    assert snapshot["verification"]["state"] == "live_catching_up"
    assert snapshot["run"]["receipt_count"] == 1


def test_replaying_an_older_valid_manifest_is_detected_with_matching_receipts(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "evidence", "run-rollback")
    chain.append("run_attested", {"ordinal": 1})
    old_manifest = (chain.root / "terminal-manifest.json").read_bytes()
    old_receipts = chain.receipts_path.read_bytes()
    chain.append("run_attested", {"ordinal": 2})

    (chain.root / "terminal-manifest.json").write_bytes(old_manifest)
    result = verify_receipt_store(chain.root)
    assert result.status == "tampered"
    assert result.finding == "manifest_rollback_detected"

    chain.receipts_path.write_bytes(old_receipts)
    result = verify_receipt_store(chain.root)
    assert result.status == "tampered"
    assert result.finding == "manifest_rollback_detected"


def test_reopened_chain_hydrates_sequence_and_continues_without_corruption(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    first = _chain(root, "run-reopened")
    first.append("run_attested", {"ordinal": 1})

    reopened = _chain(root, "run-reopened")
    second = reopened.append("run_attested", {"ordinal": 2})

    assert second["sequence"] == 2
    assert reopened.sequence == 2
    assert verify_receipt_store(reopened.root).status == "verified"


def test_manifest_anchor_lag_is_reconciled_once_on_writer_restart(
    tmp_path: Path,
) -> None:
    armed = [False]

    def observer(step: str) -> None:
        if armed[0] and step == "anchor_temporary_fsynced":
            raise RuntimeError("simulated_anchor_crash")

    root = tmp_path / "evidence"
    chain = _chain(root, "run-anchor-recovery", observer=observer)
    chain.append("run_attested", {"ordinal": 1})
    armed[0] = True
    with pytest.raises(RuntimeError, match="simulated_anchor_crash"):
        chain.append("run_attested", {"ordinal": 2})

    pending = verify_receipt_store(chain.root)
    assert pending.status == "incomplete"
    assert pending.finding == "manifest_anchor_update_pending"

    reopened = _chain(root, "run-anchor-recovery")
    assert reopened.sequence == 2
    assert verify_receipt_store(reopened.root).status == "verified"


def test_seal_advances_manifest_generation_without_an_extra_receipt(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "evidence", "run-seal-generation")
    chain.append("run_attested", {"ordinal": 1})
    before_bytes = (chain.root / "terminal-manifest.json").read_bytes()
    before = json.loads(before_bytes)

    chain.seal()
    after = json.loads(
        (chain.root / "terminal-manifest.json").read_text(encoding="utf-8")
    )

    assert after["manifest_generation"] == before["manifest_generation"] + 1
    assert after["receipt_count"] == before["receipt_count"]
    assert after["previous_manifest_hash"] == (
        "sha256:" + hashlib.sha256(before_bytes).hexdigest()
    )


def test_workflow_reconciliation_is_external_and_leaves_manifest_unchanged(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path / "evidence", "run-reconciled")
    chain.append("run_attested", {"mode": "dry_run"})
    manifest_path = chain.root / "terminal-manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    state = SupervisorState(tmp_path / "supervisor.json", chain.run_id)

    record = state.record_workflow_reconciliation(
        manifest_generation=manifest["manifest_generation"],
        receipt_count=manifest["receipt_count"],
        terminal_receipt_hash=manifest["terminal_receipt_hash"],
        operator_identity="operator:local-session",
        reason="operator_reconciled",
    )

    assert manifest_path.read_bytes() == manifest_bytes
    assert state.reconciliation_path.is_file()
    assert not state.reconciliation_path.is_relative_to(chain.root)
    assert record["evidence_assertion"] == "none"
    assert state.snapshot()["lifecycle"] == "workflow_reconciled"



def test_anchor_required_certificate_fails_closed_when_anchor_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    chain = _chain(root, "run-anchor-missing")
    chain.append("run_attested", {"ordinal": 1})
    anchor = next((root / ".torq-run-identities").glob("*/manifest-head.v1.json"))
    anchor.unlink()

    result = verify_receipt_store(chain.root)
    assert result.status == "tampered"
    assert result.finding == "manifest_anchor_missing"


def test_manifest_anchor_substitution_with_invalid_root_signature_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    chain = _chain(root, "run-anchor-substituted")
    chain.append("run_attested", {"ordinal": 1})
    anchor = next((root / ".torq-run-identities").glob("*/manifest-head.v1.json"))
    substituted = json.loads(anchor.read_text(encoding="utf-8"))
    substituted["root_signature"] = "00" * 64
    anchor.write_bytes(_canonical(substituted))

    result = verify_receipt_store(chain.root)
    assert result.status == "tampered"
    assert result.finding == "manifest_anchor_invalid"


def test_manifest_anchor_hardlink_is_rejected_as_unsafe(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    chain = _chain(root, "run-anchor-hardlink")
    chain.append("run_attested", {"ordinal": 1})
    anchor = next((root / ".torq-run-identities").glob("*/manifest-head.v1.json"))
    os.link(anchor, anchor.with_name("attacker-link.json"))

    result = verify_receipt_store(chain.root)
    assert result.status == "tampered"
    assert result.finding == "manifest_anchor_unsafe"


def test_anchorless_certificate_v2_remains_readable_but_is_read_only(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    chain = _chain(root, "run-anchorless-v2")
    chain.append("run_attested", {"ordinal": 1})
    certificate = json.loads(chain.certificate_path.read_text(encoding="utf-8"))
    certificate.pop("root_signature")
    certificate.pop("manifest_rollback_policy")
    certificate["certificate_schema_version"] = "2.0.0"
    signed_certificate = {
        **certificate,
        "root_signature": Ed25519PrivateKey.from_private_bytes(chain.key)
        .sign(_canonical(certificate))
        .hex(),
    }
    chain.certificate_path.write_bytes(_canonical(signed_certificate))
    manifest_path = chain.root / "terminal-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("signature")
    manifest["certificate_hash"] = ReceiptChain.hash_file(chain.certificate_path)
    signed_manifest = {
        **manifest,
        "signature": Ed25519PrivateKey.from_private_bytes(chain.run_keys.manifest)
        .sign(_canonical(manifest))
        .hex(),
    }
    manifest_path.write_bytes(_canonical(signed_manifest))
    downgraded = verify_receipt_store(chain.root)
    assert downgraded.status == "tampered"
    assert downgraded.finding == "run_certificate_rollback_detected"
    anchor = next((root / ".torq-run-identities").glob("*/manifest-head.v1.json"))
    anchor.unlink()

    assert verify_receipt_store(chain.root).status == "verified"
    reopened = _chain(root, "run-anchorless-v2")
    with pytest.raises(ValueError, match="receipt_store_legacy_read_only"):
        reopened.append("run_attested", {"ordinal": 2})


def test_double_death_stays_running_until_recovery_seals_abandonment(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    key_chain = _chain(root, "run-recovery")
    broker = EvidenceBroker(
        key_chain,
        allowed_roles=frozenset({
            "operator_gateway",
            "orchestrator",
            "recovery",
            "supervisor",
        }),
    )
    client = BrokeredReceiptChain(broker)
    operator = BrokeredReceiptChain(broker, writer_role="operator_gateway")
    accepted = GovernedOrchestrator().inject_context(
        operator,
        "Preserve this operator constraint if recovery resumes.",
        target_role="g1d",
        confirm_direct=True,
    )
    client.append("stage_attempt_created", _attempt())
    state = SupervisorState(tmp_path / "supervisor.json", client.run_id)
    supervisor = RunSupervisor(broker, state)
    supervisor.mark_orphaned("g1d")

    live = FleetProjector(
        client.root,
        operational_state=state.snapshot(),
    ).snapshot()
    assert live["lanes"][0]["state"] == "running"
    operational = state.snapshot()
    observed_at = str(operational["heartbeat_at"])
    controls = FleetControlService(
        recovery_available=True,
        annotation_provider=lambda: [
            {
                "kind": "orphaned",
                "scope": "g1d",
                "observed_at": observed_at,
                "source": "supervisor",
            },
            {
                "kind": "recovery_required",
                "scope": "run",
                "observed_at": observed_at,
                "source": "supervisor",
            },
        ],
    )
    envelope = controls.envelope(
        live, session_write_capable=True, expires_at="2099-01-01T00:00:00Z"
    )
    assert [item["kind"] for item in envelope["annotations"]] == [
        "orphaned", "recovery_required"
    ]
    assert envelope["eligibility"]["recover_run"]["eligible"] is True

    manifest_generation = FleetProjector(client.root).snapshot()["verification"][
        "manifest_generation"
    ]
    assert isinstance(manifest_generation, int)
    supervisor.abandon(
        ["attempt-g1d-1"],
        client.sequence,
        manifest_generation,
    )
    assert verify_receipt_store(client.root).status == "verified"
    abandoned = FleetProjector(client.root).snapshot()
    assert abandoned["lanes"][0]["state"] == "abandoned"
    assert abandoned["run"]["workflow_state"] == "abandoned"
    unapplied = next(
        row
        for row in client.covered_receipts()
        if row["transition"] == "command_unapplied"
    )
    assert unapplied["payload"]["command_id"] == accepted["command_id"]


def test_recovery_abandonment_rejects_stale_operational_generation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    chain = _chain(root, "run-stale-recovery-state")
    broker = EvidenceBroker(
        chain,
        allowed_roles=frozenset({"orchestrator", "recovery"}),
    )
    client = BrokeredReceiptChain(broker)
    client.append("stage_attempt_created", _attempt())
    state = SupervisorState(tmp_path / "supervisor.json", client.run_id)
    supervisor = RunSupervisor(broker, state)
    marked = supervisor.mark_orphaned("g1d")
    stale_generation = int(marked["generation"])
    state.update(lifecycle="running", worker_pid=4242)

    with pytest.raises(
        ValueError,
        match="supervisor_state_generation_changed",
    ):
        supervisor.abandon(
            ["attempt-g1d-1"],
            client.sequence,
            0,
            expected_state_generation=stale_generation,
        )

    assert client.sequence == 1


def test_recovery_abandonment_rejects_live_worker_without_issuing_receipt(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    chain = _chain(root, "run-live-worker-recovery")
    broker = EvidenceBroker(
        chain,
        allowed_roles=frozenset({"orchestrator", "recovery"}),
    )
    client = BrokeredReceiptChain(broker)
    client.append("stage_attempt_created", _attempt())
    state = SupervisorState(tmp_path / "supervisor.json", client.run_id)
    supervisor = RunSupervisor(broker, state)
    supervisor.mark_orphaned("g1d")
    state.update(lifecycle="recovery_required", worker_pid=4242)

    with pytest.raises(ValueError, match="supervisor_live_worker_present"):
        supervisor.abandon(["attempt-g1d-1"], client.sequence, 0)

    assert client.sequence == 1


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
        assert (
            json.loads(allowed.read())["snapshot"]["verification"]["state"]
            == "sealed_verified"
        )
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


def test_mutation_session_claim_is_atomic_and_rotation_keeps_absolute_age() -> None:
    clock = [100.0]
    sessions = FleetSessionManager(
        clock=lambda: clock[0],
        idle_seconds=20,
        absolute_seconds=30,
    )
    token = sessions.exchange(sessions.bootstrap_nonce)
    cookie = f"torq_fleet_session={token}"
    barrier = threading.Barrier(3)
    claims: list[object] = []

    def claim() -> None:
        barrier.wait()
        claims.append(sessions.claim_mutation(cookie))

    workers = [threading.Thread(target=claim) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1

    clock[0] = 120.0
    rotated = sessions.rotate(winners[0])  # type: ignore[arg-type]
    assert sessions.authenticate(f"torq_fleet_session={rotated}") is not None
    clock[0] = 130.0
    assert sessions.authenticate(f"torq_fleet_session={rotated}") is None
