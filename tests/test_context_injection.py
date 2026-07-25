from __future__ import annotations

import http.client
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
from torq_cli.interfaces.fleet_http import create_fleet_server
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
    assert receipt["payload"]["provider_dispatch"] is False
    assert receipt["payload"]["redactions"] == ["KEY_ASSIGNMENT"]
    assert receipt["payload"]["route"] == "lead_replan"
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

    orchestrator.inject_context(chain, "Builder-only constraint", target_role="builder")
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


def test_context_validation_fails_before_receipt_or_artifact(tmp_path: Path) -> None:
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

    assert not chain.receipts_path.exists()
    assert not (chain.root / "artifacts").exists()


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
    server = create_fleet_server(
        FleetProjector(chain.root),
        port=0,
        context_injector=injector,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}/api/v1/context"
    body = json.dumps({"content": "Use this new constraint", "target_role": "g2a"}).encode()
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
        with pytest.raises(urllib.error.HTTPError) as blocked:
            urllib.request.urlopen(denied)
        assert blocked.value.code == 403

        accepted = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{port}",
            },
            method="POST",
        )
        with urllib.request.urlopen(accepted) as response:
            result = json.loads(response.read())
        assert response.status == 202
        assert result["status"] == "accepted"
        assert "content" not in result["context"]

        snapshot = FleetProjector(chain.root).snapshot()
        assert snapshot["verification"]["status"] == "verified"
        assert snapshot["run"]["context_injections"] == 1
        g2a = next(row for row in snapshot["lanes"] if row["role"] == "g2a")
        assert g2a["latest_transition"] == "context_injected"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
