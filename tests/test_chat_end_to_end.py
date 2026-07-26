from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import pytest

from torq_cli.application.chat_projection import reduce_chat_projection
from torq_cli.application.chat_runtime import (
    ChatProviderCommand,
    ChatRuntimeCoordinator,
)
from torq_cli.application.orchestrator import GovernedOrchestrator
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.safety.chat_evidence import ChatEvidenceJournal, verify_chat_evidence
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


_PROVIDER = Path("tests/fixtures/fake_owned_provider.py").resolve()


def _journal(tmp_path: Path) -> ChatEvidenceJournal:
    evidence = tmp_path / "evidence"
    store = FileRunKeyStore(evidence)
    chain = ReceiptChain(
        evidence,
        "run-chat-e2e",
        store,
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
    return ChatEvidenceJournal(
        chain.root,
        store.get_or_create_run_keys(chain.run_id).operator_gateway,
    )


def _command(role: str, cwd: Path):
    def build(*_args: object) -> ChatProviderCommand:
        return ChatProviderCommand(
            (sys.executable, str(_PROVIDER), "--role", role),
            str(cwd),
            dict(os.environ),
        )

    return build


def _wait_for(journal: ChatEvidenceJournal, event: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if any(row["event"] == event for row in journal.rows()):
            return
        time.sleep(0.02)
    raise AssertionError(f"chat event not observed: {event}")


@pytest.mark.skipif(os.name != "nt", reason="strong Windows containment only")
def test_real_owned_completion_rebuilds_verified_transcript(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    runtime = ChatRuntimeCoordinator(journal, _command("complete", tmp_path))
    runtime.submit(turn_id="turn-e2e-complete", text="hello")
    _wait_for(journal, "turn_completed")

    rows = verify_chat_evidence(journal.run_root)
    projection = reduce_chat_projection(rows, verification_state="verified")
    assert projection["status"] == "ready"
    assert [message["role"] for message in projection["messages"]] == [
        "user",
        "assistant",
    ]
    assert '"kind": "complete"' in projection["messages"][-1]["content"]


@pytest.mark.skipif(os.name != "nt", reason="strong Windows containment only")
def test_real_owned_stop_commits_cancelled_only_after_empty_job(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    runtime = ChatRuntimeCoordinator(journal, _command("parent", tmp_path))
    runtime.submit(turn_id="turn-e2e-cancel", text="stop this")
    _wait_for(journal, "turn_started")
    terminal = runtime.cancel("turn-e2e-cancel", timeout=10)
    assert terminal["event"] == "turn_cancelled"
    assert terminal["body"]["containment_state"] == "known_empty"
    assert runtime.snapshot()["active_turn_id"] is None
