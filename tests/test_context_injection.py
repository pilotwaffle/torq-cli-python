from __future__ import annotations

import http.client
import base64
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import pytest

from torq_cli.application.context import GovernedContextInjector
from torq_cli.application.fleet import FleetProjector
from torq_cli.application.orchestrator import GovernedOrchestrator, OrchestrationBlocked
from torq_cli.core.engine import NormalizedResponse, Provenance
from torq_cli.core.graph import ExecutionMode
from torq_cli.core.redaction import RedactionBlocked
from torq_cli.domain.registry_schema import load_registry
from torq_cli.domain.run_plan import plan_hash, revision_body
from torq_cli.interfaces.fleet_http import FleetSessionManager, create_fleet_server
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store


class _CapturingDispatcher:
    def __init__(self, responses: Mapping[str, list[NormalizedResponse]]) -> None:
        self.responses = {role: list(rows) for role, rows in responses.items()}
        self.prompts: dict[str, list[str]] = {}

    def dispatch(
        self,
        *,
        role: str,
        provider: str,
        model: str,
        prompt: str,
    ) -> NormalizedResponse:
        del provider, model
        self.prompts.setdefault(role, []).append(prompt)
        return self.responses[role].pop(0)


def _response(provider: str, model: str, body: Mapping[str, object]) -> NormalizedResponse:
    return NormalizedResponse(
        json.dumps(body),
        "",
        {"prompt_tokens": 2, "completion_tokens": 3, "reasoning_tokens": 0},
        Provenance(provider, model, False),
    )


def _chain(tmp_path: Path, name: str) -> ReceiptChain:
    evidence = tmp_path / "evidence"
    return ReceiptChain(
        evidence,
        name,
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )


def _ceilings() -> dict[str, float]:
    return {
        role: 0.1
        for role in ("g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui")
    }


def test_context_is_redacted_encrypted_receipted_and_consumed_by_next_stage(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _CapturingDispatcher({
        "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
        "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "reject"})],
    })
    orchestrator = GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role=_ceilings(),
    )
    chain = _chain(tmp_path, "run-context")
    secret = 'api_key="abcdefghijklmnopqrstuv"'

    injected = orchestrator.inject_context(
        chain,
        f"Review the new constraint; {secret}",
        source_name="operator-note.txt",
    )
    result = orchestrator.execute(
        goal="Use the injected context",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )
    chain.seal()

    assert result.status == "design_rejected"
    prompt = dispatcher.prompts["g1d"][0]
    assert secret not in prompt
    assert "[REDACTED:KEY_ASSIGNMENT]" in prompt
    receipt_text = (chain.root / "receipts.jsonl").read_text(encoding="utf-8")
    assert secret not in receipt_text
    receipt = next(
        json.loads(line)
        for line in receipt_text.splitlines()
        if json.loads(line)["transition"] == "context_injected"
    )
    accepted = next(
        json.loads(line)
        for line in receipt_text.splitlines()
        if json.loads(line)["transition"] == "command_accepted"
    )
    replan = next(
        json.loads(line)
        for line in receipt_text.splitlines()
        if json.loads(line)["transition"] == "run_replanned"
    )
    created = next(
        json.loads(line)
        for line in receipt_text.splitlines()
        if json.loads(line)["transition"] == "stage_attempt_created"
    )
    started = next(
        json.loads(line)
        for line in receipt_text.splitlines()
        if json.loads(line)["transition"] == "stage_dispatch_started"
    )
    assert accepted["payload"]["command_id"] == receipt["payload"]["command_id"]
    assert receipt["payload"]["accepted_sequence"] == accepted["sequence"]
    assert accepted["payload"]["earliest_eligible_attempt"] == {
        "kind": "attempt_created_after_acknowledgement"
    }
    assert receipt["payload"]["provider_dispatch"] is False
    assert receipt["payload"]["redactions"] == ["KEY_ASSIGNMENT"]
    assert receipt["payload"]["route"] == "lead_replan"
    assert accepted["sequence"] < created["sequence"] < replan["sequence"]
    assert replan["sequence"] < receipt["sequence"] < started["sequence"]
    revision = revision_body(
        plan_revision=replan["payload"]["plan_revision"],
        previous_plan_hash=replan["payload"]["old_plan_hash"],
        command_id=accepted["payload"]["command_id"],
        accepted_sequence=accepted["sequence"],
        affected_future_attempts=replan["payload"]["affected_future_attempts"],
    )
    assert replan["payload"]["new_plan_hash"] == plan_hash(revision)
    artifact = chain.root / str(injected["artifact"])
    assert secret.encode() not in artifact.read_bytes()
    assert b"REDACTED" not in artifact.read_bytes()
    assert verify_receipt_store(chain.root).status == "verified"


def test_direct_lane_context_waits_for_that_lane(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _CapturingDispatcher({
        "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
        "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "approve"})],
        "builder": [_response("deepseek", "deepseek-v4-pro", {"status": "build_complete"})],
        "g2a": [_response("openai", "gpt-5.5", {"verdict": "approve", "defects": []})],
    })
    orchestrator = GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role=_ceilings(),
    )
    chain = _chain(tmp_path, "run-targeted")

    orchestrator.inject_context(
        chain,
        "Builder-only constraint",
        target_role="builder",
        confirm_direct=True,
    )
    orchestrator.execute(
        goal="Route context",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert "Builder-only constraint" not in dispatcher.prompts["g1d"][0]
    assert "Builder-only constraint" not in dispatcher.prompts["g1r"][0]
    assert "Builder-only constraint" in dispatcher.prompts["builder"][0]
    receipts = [
        json.loads(line)
        for line in (chain.root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    started = next(
        row["payload"]
        for row in receipts
        if row["transition"] == "stage_dispatch_started"
        and row["payload"]["role"] == "builder"
    )
    assert len(started["context_ids"]) == 1
    assert started["command_ids"] == [
        next(
            row["payload"]["command_id"]
            for row in receipts
            if row["transition"] == "command_accepted"
        )
    ]
    assert not any(row["transition"] == "run_replanned" for row in receipts)


def test_accepted_context_survives_a_fresh_orchestrator_instance(tmp_path: Path) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    chain = _chain(tmp_path, "run-context-reconstructed")
    GovernedOrchestrator().inject_context(chain, "Durable restart constraint")
    dispatcher = _CapturingDispatcher({
        "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
        "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "reject"})],
    })

    GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role=_ceilings(),
    ).execute(
        goal="Resume from receipts",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert "Durable restart constraint" in dispatcher.prompts["g1d"][0]
    rows = [json.loads(line) for line in chain.receipts_path.read_text().splitlines()]
    applied = [row for row in rows if row["transition"] == "context_injected"]
    assert len(applied) == 1
    created_sequence = applied[0]["payload"]["effective_attempt"][
        "attempt_created_sequence"
    ]
    assert applied[0]["payload"]["accepted_sequence"] < created_sequence
    assert created_sequence < applied[0]["sequence"]


def test_json_artifact_is_extracted_then_sanitized_and_provenance_bound(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _CapturingDispatcher({
        "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
        "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "reject"})],
    })
    orchestrator = GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role=_ceilings(),
    )
    chain = _chain(tmp_path, "run-json-artifact")
    secret = "abcdefghijklmnopqrstuv"
    accepted = orchestrator.inject_artifact(
        chain,
        ('{"constraint":"QWEN_TOKEN_PLAN_\\u0041PI_KEY=' + secret + '"}').encode(),
        media_type="application/json",
        source_name="constraint.json",
    )

    orchestrator.execute(
        goal="Use extracted artifact",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )

    assert secret not in dispatcher.prompts["g1d"][0]
    assert "[REDACTED:KEY_ASSIGNMENT]" in dispatcher.prompts["g1d"][0]
    rows = [json.loads(line) for line in chain.receipts_path.read_text().splitlines()]
    applied = next(row for row in rows if row["transition"] == "context_injected")
    assert applied["payload"]["extraction"] == accepted["extraction"]
    assert applied["payload"]["command_type"] == "artifact"
    assert secret not in chain.read_artifact(chain.root / str(accepted["artifact"]))


def test_context_cannot_apply_to_an_attempt_created_before_acceptance(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    binding = profile.bindings["g1d"]
    chain = _chain(tmp_path, "run-context-boundary")
    chain.append(
        "run_planned",
        {
            "mode": "live",
            "profile_id": profile.profile_id,
            "strategy_id": profile.strategy_id,
            "planned_roles": GovernedOrchestrator._PLANNED_ROLES,
            "lane_catalog": GovernedOrchestrator._lane_catalog(profile),
        },
    )
    created = chain.append(
        "stage_attempt_created",
        {
            "role": "g1d",
            "attempt_id": "attempt-preexisting",
            "attempt_ordinal": 1,
            "repair_cycle": 0,
            "provider": binding.provider_id,
            "model": binding.model_id,
            "prompt_id": binding.prompt_id,
            "provider_dispatch": False,
        },
    )
    accepted = GovernedOrchestrator().inject_context(
        chain,
        "Too late for this attempt",
        target_role="g1d",
        confirm_direct=True,
    )
    before = chain.sequence

    with pytest.raises(ValueError, match="command_boundary_violated"):
        chain.append(
            "context_injected",
                {
                    "command_id": accepted["command_id"],
                    "command_type": "context",
                    "accepted_sequence": accepted["accepted_sequence"],
                "context_id": accepted["context_id"],
                    "target_role": "g1d",
                "artifact": accepted["artifact"],
                "artifact_hash": accepted["artifact_hash"],
                    "media_type": "text/plain",
                    "source_name": None,
                    "content_bytes": accepted["content_bytes"],
                    "redactions": accepted["redactions"],
                    "extraction": None,
                    "direct_route_confirmed": True,
                "effective_attempt": {
                    "role": "g1d",
                    "attempt_id": "attempt-preexisting",
                    "attempt_ordinal": 1,
                    "attempt_created_sequence": created["sequence"],
                },
                "provider_dispatch": False,
            },
            writer_role="orchestrator",
            evidence_basis="derived",
        )
    assert chain.sequence == before


def test_unreachable_context_is_finalized_unapplied_before_terminal(
    tmp_path: Path,
) -> None:
    profile = load_registry().profiles["torq-v5-6-live"]
    dispatcher = _CapturingDispatcher({
        "g1d": [_response("anthropic", "claude-fable-5", {"status": "design_complete"})],
        "g1r": [_response("anthropic", "claude-opus-4-8", {"verdict": "reject"})],
    })
    orchestrator = GovernedOrchestrator(
        dispatcher,
        budget_usd=1.0,
        cost_ceiling_usd_by_role=_ceilings(),
    )
    chain = _chain(tmp_path, "run-context-unapplied")
    accepted = orchestrator.inject_context(
        chain,
        "Only for the UI repair lane",
        target_role="refine_ui",
        confirm_direct=True,
    )

    orchestrator.execute(
        goal="Reject before repair",
        profile=profile,
        mode=ExecutionMode.LIVE,
        chain=chain,
    )
    chain.seal()
    rows = [json.loads(line) for line in chain.receipts_path.read_text().splitlines()]
    final = rows[-3:]
    assert [row["transition"] for row in final] == [
        "terminalization_started",
        "command_unapplied",
        "run_decision",
    ]
    assert final[0]["payload"]["reason"] == "pending_commands"
    assert final[1]["payload"]["command_id"] == accepted["command_id"]
    assert final[1]["payload"]["reason"] == "run_terminating"
    assert final[2]["payload"]["decision"] == "blocked"
    assert verify_receipt_store(chain.root).status == "verified"


def test_context_validation_writes_safe_rejections_before_artifact(tmp_path: Path) -> None:
    orchestrator = GovernedOrchestrator()
    chain = _chain(tmp_path, "run-invalid")

    with pytest.raises(OrchestrationBlocked, match="context_empty"):
        orchestrator.inject_context(chain, " ")
    with pytest.raises(OrchestrationBlocked, match="context_target_invalid"):
        orchestrator.inject_context(chain, "content", target_role="unknown")
    with pytest.raises(OrchestrationBlocked, match="context_too_large"):
        orchestrator.inject_context(chain, "x" * 1_048_577)
    with pytest.raises(OrchestrationBlocked, match="context_media_type_invalid"):
        orchestrator.inject_context(chain, "opaque", media_type="image/png")
    with pytest.raises(RedactionBlocked, match="OPENAI_STYLE_KEY"):
        orchestrator.inject_context(chain, "sk-" + "A" * 24)

    rows = [
        json.loads(line)
        for line in chain.receipts_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 5
    assert {row["transition"] for row in rows} == {"command_rejected"}
    assert [row["payload"]["finding"] for row in rows] == [
        "context_empty",
        "context_target_invalid",
        "context_too_large",
        "context_media_type_invalid",
        "redaction_blocked:OPENAI_STYLE_KEY",
    ]
    serialized = chain.receipts_path.read_text(encoding="utf-8")
    assert "sk-" + "A" * 24 not in serialized
    assert not (chain.root / "artifacts").exists()


def test_context_command_id_replay_is_durable_and_writes_nothing(
    tmp_path: Path,
) -> None:
    chain = _chain(tmp_path, "run-context-replay")
    first = GovernedOrchestrator().inject_context(
        chain,
        "Preserve this constraint",
        command_id="context-correlation-1",
    )
    before = chain.sequence

    replay = GovernedOrchestrator().inject_context(
        chain,
        "A retry body must not replace the accepted command",
        command_id="context-correlation-1",
    )

    assert replay["idempotent_replay"] is True
    assert replay["sequence"] == first["sequence"]
    assert replay["artifact_hash"] == first["artifact_hash"]
    assert chain.sequence == before
    assert verify_receipt_store(chain.root).status == "verified"


def test_duplicate_command_id_is_rejected_before_append(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-duplicate-command")
    payload = {
        "command_id": "cmd-replayed",
        "command_type": "context",
        "target_role": "lead",
        "media_type": "text/plain",
        "source_name": None,
        "content_bytes": 0,
        "finding": "context_empty",
        "earliest_eligible_attempt": None,
        "provider_dispatch": False,
    }
    chain.append(
        "command_rejected",
        payload,
        writer_role="operator_gateway",
        evidence_basis="submitted",
    )
    with pytest.raises(ValueError, match="command_id_duplicate"):
        chain.append(
            "command_rejected",
            payload,
            writer_role="operator_gateway",
            evidence_basis="submitted",
        )
    assert chain.sequence == 1


def test_same_origin_http_context_endpoint_is_opt_in_and_receipt_backed(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-http-context")
    profile = load_registry().profiles["torq-v5-6-live"]
    chain.append(
        "run_planned",
        {
            "mode": "live",
            "profile_id": "torq-v5-6-live",
            "planned_roles": (
                "g1d",
                "g1r",
                "builder",
                "g2a",
                "refine_bug",
                "refine_ui",
            ),
            "lane_catalog": GovernedOrchestrator._lane_catalog(profile),
        },
    )
    orchestrator = GovernedOrchestrator()
    injector = GovernedContextInjector(orchestrator, chain)
    sessions = FleetSessionManager()
    token = sessions.exchange(sessions.bootstrap_nonce)
    cookie = f"torq_fleet_session={token}"
    server = create_fleet_server(
        FleetProjector(chain.root),
        port=0,
        context_injector=injector,
        sessions=sessions,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/api/v1/context"
    body = json.dumps({
        "content": "Use this new constraint",
        "target_role": "g2a",
        "confirm_direct": True,
    }).encode()
    try:
        connection = http.client.HTTPConnection(host, port)
        connection.putrequest("POST", "/api/v1/context", skip_host=True)
        connection.putheader("Host", f"attacker.example:{port}")
        connection.putheader("Origin", f"http://attacker.example:{port}")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        rebound = connection.getresponse()
        assert rebound.status == 421
        assert json.loads(rebound.read())["finding"] == "fleet_host_denied"
        connection.close()

        denied = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            method="POST",
        )
        denied.add_header("Cookie", cookie)
        with pytest.raises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(denied)
        assert blocked.value.code == 403
        rotated_cookie = blocked.value.headers["Set-Cookie"].split(";", 1)[0]

        accepted = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "Cookie": rotated_cookie,
            },
            method="POST",
        )
        with urllib.request.urlopen(accepted) as response:
            result = json.loads(response.read())
            accepted_cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        assert response.status == 202
        assert result["status"] == "accepted"
        assert "content" not in result["context"]

        stale = urllib.request.Request(
            f"http://{host}:{port}/api/v1/fleet",
            headers={"Cookie": rotated_cookie},
        )
        with pytest.raises(urllib.error.HTTPError) as stale_error:
            urllib.request.urlopen(stale)
        assert stale_error.value.code == 401

        file_body = json.dumps({
            "input_kind": "file",
            "content_base64": base64.b64encode(b'{"constraint":"file"}').decode(),
            "media_type": "application/json",
            "source_name": "constraint.json",
        }).encode()
        file_request = urllib.request.Request(
            url,
            data=file_body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
                "Cookie": accepted_cookie,
            },
            method="POST",
        )
        with urllib.request.urlopen(file_request) as file_response:
            file_result = json.loads(file_response.read())
        assert file_response.status == 202
        assert file_result["context"]["command_type"] == "artifact"

        snapshot = FleetProjector(chain.root).snapshot()
        assert snapshot["verification"]["state"] == "live_verified"
        assert snapshot["run"]["context_commands_accepted"] == 2
        assert snapshot["run"]["context_injections"] == 0
        g2a = next(row for row in snapshot["lanes"] if row["role"] == "g2a")
        assert g2a["latest_transition"] == "run_planned"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_terminal_run_rejects_mutation_without_a_prior_fleet_poll(tmp_path: Path) -> None:
    chain = _chain(tmp_path, "run-http-terminal")
    profile = load_registry().profiles["torq-v5-6-live"]
    GovernedOrchestrator().execute(
        goal="Close this run",
        profile=profile,
        mode=ExecutionMode.DRY_RUN,
        chain=chain,
    )
    chain.seal()
    before = chain.sequence
    sessions = FleetSessionManager()
    token = sessions.exchange(sessions.bootstrap_nonce)
    server = create_fleet_server(
        FleetProjector(chain.root),
        port=0,
        context_injector=GovernedContextInjector(GovernedOrchestrator(), chain),
        sessions=sessions,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    request = urllib.request.Request(
        f"http://{host}:{port}/api/v1/context",
        data=json.dumps({"content": "too late"}).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "Cookie": f"torq_fleet_session={token}",
        },
        method="POST",
    )
    try:
        with pytest.raises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(request)
        assert blocked.value.code == 409
        assert json.loads(blocked.value.read())["finding"] == "run_terminal"
        assert chain.sequence == before
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
