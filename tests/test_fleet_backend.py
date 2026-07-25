from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from torq_cli.application.fleet import FleetProjector
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces.cli import main
from torq_cli.interfaces.fleet_http import create_fleet_server
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store


def _chain(tmp_path: Path, name: str = "run-fleet") -> ReceiptChain:
    evidence = tmp_path / "evidence"
    return ReceiptChain(
        evidence,
        name,
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _planned(chain: ReceiptChain) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain.append(
        "run_planned",
        {
            "mode": "live",
            "profile_id": "torq-v5-6-live",
            "strategy_id": "standard_v1",
            "planned_roles": ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"),
            "lane_catalog": GovernedOrchestrator._lane_catalog(profile),
        },
    )


def _attempt(role: str, ordinal: int = 1, cycle: int = 0) -> dict[str, object]:
    return {
        "role": role,
        "attempt_id": f"attempt-{role}-{ordinal}",
        "attempt_ordinal": ordinal,
        "repair_cycle": cycle,
    }


def _created(chain: ReceiptChain, role: str, ordinal: int = 1) -> dict[str, object]:
    attempt = _attempt(role, ordinal)
    chain.append(
        "stage_attempt_created",
        {**attempt, "provider_dispatch": False},
    )
    return attempt


def test_rolling_manifest_authenticates_a_live_unsealed_snapshot(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    _planned(chain)
    attempt = _created(chain, "g1d")
    chain.append(
        "stage_dispatch_started",
        {
            **attempt,
            "provider": "anthropic",
            "model": "claude-fable-5",
            "settlement": "plan_covered",
            "provider_dispatch": True,
        },
    )

    assert verify_receipt_store(chain.root).status == "verified"
    manifest = json.loads((chain.root / "terminal-manifest.json").read_text(encoding="utf-8"))
    assert manifest["sealed"] is False
    snapshot = FleetProjector(chain.root).snapshot()
    assert snapshot["verification"]["normalized_state"] == "live_verified"
    assert snapshot["run"]["sealed"] is False
    assert snapshot["run"]["status"] == "running"
    assert snapshot["summary"]["running"] == 1
    assert snapshot["summary"]["queued"] == 3
    assert snapshot["summary"]["dormant"] == 2
    assert snapshot["summary"]["open_actions"] == 0
    assert snapshot["lanes"][0]["role"] == "g1d"
    assert snapshot["lanes"][0]["state"] == "running"
    assert snapshot["run"]["elapsed_status"] == "receipt_timestamps_available"


def test_completed_and_blocked_lanes_project_receipt_backed_values(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-blocked")
    _planned(chain)
    completed_attempt = _created(chain, "g1d")
    chain.append(
        "stage_dispatch_started",
        {**completed_attempt, "provider_dispatch": True},
    )
    chain.append(
        "stage_completed",
        {
            **completed_attempt,
            "provider": "anthropic",
            "model": "claude-fable-5",
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "reasoning_tokens": 0,
                "tokens": 120,
            },
            "cost_usd": 0.0,
            "billed_usd": 0.0,
            "metered_usd": 0.002,
            "pricing_status": "priced",
            "rate_table_version": "test.v1",
            "settlement": "plan_covered",
            "entitlement": {
                "account": "anthropic-max",
                "used": 1,
                "limit": 100,
                "used_source": "receipt_derived",
                "limit_source": "operator_declared",
            },
            "provider_dispatch": True,
        },
    )
    blocked_attempt = _created(chain, "g1r")
    chain.append(
        "stage_blocked",
        {
            **blocked_attempt,
            "provider": "anthropic",
            "model": "claude-opus-4-8",
            "reason": "plan_window_exceeded:g1r",
            "provider_dispatch": False,
            "settlement": "plan_covered",
        },
    )
    chain.append(
        "run_decision",
        {
            "status": "blocked",
            "reason": "plan_window_exceeded:g1r",
            "provider_dispatch": True,
        },
    )
    chain.seal()

    snapshot = FleetProjector(chain.root).snapshot()

    assert snapshot["run"]["sealed"] is True
    assert snapshot["run"]["status"] == "blocked"
    assert snapshot["run"]["waiting_on"] == []
    assert snapshot["summary"]["needs_you"] == 1
    assert snapshot["settlement"]["metered_equivalent_usd"] == 0.002
    blocked = next(row for row in snapshot["lanes"] if row["role"] == "g1r")
    assert blocked["provider_dispatch"] is False
    assert blocked["reason"] == "plan_window_exceeded:g1r"
    assert "subscription account" in blocked["reason_gloss"]


def test_tampered_chain_never_projects_plausible_fleet_data(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-tampered")
    _planned(chain)
    chain.seal()
    receipts = chain.root / "receipts.jsonl"
    receipts.write_text(receipts.read_text(encoding="utf-8").replace("live", "forged"), encoding="utf-8")

    snapshot = FleetProjector(chain.root).snapshot()

    assert snapshot["verification"]["status"] == "tampered"
    assert snapshot["data_status"] == "unavailable"
    assert snapshot["run"] is None
    assert snapshot["lanes"] == []
    assert snapshot["settlement"] is None


def test_fleet_cli_emits_stable_json_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    chain = _chain(tmp_path, "run-cli")
    _planned(chain)
    chain.append("run_decision", {"status": "dry_run_complete", "provider_dispatch": False})
    chain.seal()

    code = main(["fleet", "--run-root", str(chain.root)])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["schema"] == "torq-fleet-snapshot-v2"
    assert output["run"]["run_id"] == "run-cli"


def test_fleet_projects_certified_writer_provenance(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-authority")
    _planned(chain)
    attempt = _created(chain, "g1d")
    interrupted = chain.append(
        "stage_interrupted",
        {
            **attempt,
            "reason": "worker_lease_expired",
            "provider_dispatch": "unknown",
        },
        writer_role="supervisor",
        evidence_basis="derived",
    )
    chain.append(
        "run_decision",
        {
            "status": "workflow_failed",
            "interruption_sequence": interrupted["sequence"],
        },
        writer_role="supervisor",
        evidence_basis="derived",
    )
    chain.seal()

    snapshot = FleetProjector(chain.root).snapshot()
    lane = next(row for row in snapshot["lanes"] if row["role"] == "g1d")

    assert snapshot["run"]["decision_writer"]["writer_role"] == "supervisor"
    assert lane["latest_writer_role"] == "supervisor"
    assert [
        row["writer_role"] for row in lane["attempts"][0]["transitions"]
    ] == [
        "orchestrator",
        "supervisor",
    ]


def test_fleet_http_is_loopback_read_only_and_reverifies_each_request(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-http")
    _planned(chain)
    server = create_fleet_server(FleetProjector(chain.root), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/v1/fleet") as response:
            assert response.headers["Cache-Control"] == "no-store"
            first = json.loads(response.read())
        assert first["verification"]["status"] == "verified"

        connection = http.client.HTTPConnection(host, port)
        connection.putrequest("GET", "/healthz", skip_host=True)
        connection.putheader("Host", f"LOCALHOST:{port}")
        connection.endheaders()
        localhost_response = connection.getresponse()
        assert localhost_response.status == 200
        assert json.loads(localhost_response.read())["status"] == "ok"
        connection.close()

        connection = http.client.HTTPConnection(host, port)
        connection.putrequest("GET", "/api/v1/fleet", skip_host=True)
        connection.putheader("Host", f"attacker.example:{port}")
        connection.endheaders()
        rebound = connection.getresponse()
        assert rebound.status == 421
        assert json.loads(rebound.read()) == {
            "finding": "fleet_host_denied",
            "status": "blocked",
        }
        connection.close()

        receipts = chain.root / "receipts.jsonl"
        receipts.write_text(receipts.read_text(encoding="utf-8").replace("live", "forged"), encoding="utf-8")
        with urllib.request.urlopen(f"http://{host}:{port}/api/v1/fleet") as response:
            second = json.loads(response.read())
        assert second["verification"]["status"] == "tampered"
        assert second["data_status"] == "unavailable"

        request = urllib.request.Request(
            f"http://{host}:{port}/api/v1/fleet",
            data=b"{}",
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(request)
        assert denied.value.code == 405

        connection = http.client.HTTPConnection(host, port)
        connection.putrequest("POST", "/api/v1/fleet", skip_host=True)
        connection.putheader("Host", f"127.1:{port}")
        connection.putheader("Content-Length", "2")
        connection.endheaders(b"{}")
        denied_rebound_post = connection.getresponse()
        assert denied_rebound_post.status == 421
        denied_rebound_post.read()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    with pytest.raises(ValueError, match="fleet_loopback_required"):
        create_fleet_server(FleetProjector(chain.root), host="0.0.0.0")
