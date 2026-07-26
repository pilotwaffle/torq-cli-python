from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.adapters import process
from torq_cli.adapters.linux_containment import linux_containment_capability
from torq_cli.adapters.process import OwnedProcess
from torq_cli.interfaces import cli


def test_linux_production_capability_is_immutable_and_unavailable() -> None:
    capability = linux_containment_capability()
    assert capability.available is False
    assert capability.mechanism == "user_systemd_experimental_not_strong"
    assert capability.reason == "distinct_identity_system_broker_required"
    with pytest.raises((AttributeError, TypeError)):
        capability.available = True  # type: ignore[misc]


def test_normal_owned_process_refuses_linux_before_adapter_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process.sys, "platform", "linux")
    monkeypatch.setattr(process.os, "name", "posix")
    monkeypatch.setattr(
        process,
        "LinuxSystemdCgroup",
        lambda: (_ for _ in ()).throw(AssertionError("adapter_must_not_start")),
    )
    with pytest.raises(OSError, match="^owned_process_strong_containment_unavailable$"):
        OwnedProcess(("provider-must-not-start",), cwd=".", env={})


def test_linux_chat_cli_refuses_before_chat_evidence_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_root = tmp_path / "missing-run"
    monkeypatch.setattr(cli.sys, "platform", "linux")
    result = cli.main(
        [
            "fleet",
            "--run-root",
            str(run_root),
            "--serve",
            "--chat-provider",
            "claude",
            "--chat-model",
            "claude-opus-4-8",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert result == 3
    assert report == {
        "finding": "distinct_identity_system_broker_required",
        "status": "blocked",
    }
    assert not run_root.exists()
    assert not (tmp_path / ".torq-chat-heads").exists()
