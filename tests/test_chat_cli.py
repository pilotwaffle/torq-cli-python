from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.interfaces import cli
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


class _Server:
    server_address = ("127.0.0.1", 9876)
    fleet_bootstrap_nonce = "nonce"

    def serve_forever(self) -> None:
        return

    def server_close(self) -> None:
        return


@pytest.mark.skipif(cli.sys.platform != "win32", reason="strong Windows containment only")
def test_fleet_chat_cli_requires_explicit_provider_and_wires_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    evidence = tmp_path / "evidence"
    chain = ReceiptChain(
        evidence,
        "run-chat-cli",
        FileRunKeyStore(evidence),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    profile = load_registry().profiles["torq-v5-6-live"]
    chain.append(
        "run_planned",
        {
            "mode": "live",
            "profile_id": "torq-v5-6-live",
            "strategy_id": "standard_v1",
            "planned_roles": tuple(profile.bindings),
            "lane_catalog": GovernedOrchestrator._lane_catalog(profile, mode=ExecutionMode.LIVE),
        },
    )
    captured: dict[str, Any] = {}

    def create_server(projector: object, **kwargs: Any) -> _Server:
        del projector
        captured.update(kwargs)
        return _Server()

    monkeypatch.setattr(cli, "create_fleet_server", create_server)
    result = cli.main(
        [
            "fleet",
            "--run-root",
            str(chain.root),
            "--serve",
            "--chat-provider",
            "claude",
            "--chat-model",
            "claude-opus-4-8",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 0, report
    assert report["status"] == "serving"
    assert captured["chat_controller"] is not None
    assert callable(captured["chat_snapshot_provider"])


def test_fleet_chat_cli_fails_closed_without_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = cli.main(
        [
            "fleet",
            "--run-root",
            str(tmp_path / "missing"),
            "--serve",
            "--chat-provider",
            "claude",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 3
    assert report == {"finding": "chat_model_required", "status": "blocked"}
