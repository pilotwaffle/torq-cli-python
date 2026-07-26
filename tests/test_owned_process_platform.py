from __future__ import annotations

import os
import subprocess
import sys

import pytest

from torq_cli.adapters import process as process_module
from torq_cli.adapters.macos_containment import macos_containment_capability
from torq_cli.adapters.process import OwnedProcess


@pytest.mark.skipif(os.name == "nt" or sys.platform.startswith("linux"), reason="macOS gate")
def test_macos_owned_process_fails_closed_before_starting_provider() -> None:
    with pytest.raises(OSError, match="strong_containment_unavailable"):
        OwnedProcess(("provider-must-not-start",), cwd=".", env={})


def test_macos_capability_pins_the_ordinary_wheel_fail_closed_contract() -> None:
    capability = macos_containment_capability()

    assert capability.available is False
    assert capability.distribution == "ordinary_python_wheel"
    assert capability.mechanism is None
    assert capability.finding == "owned_process_strong_containment_unavailable"
    assert capability.reason == "macos_signed_containment_helper_required"
    assert set(capability.required_guarantees) == {
        "no_provider_execution_before_containment",
        "setsid_and_double_fork_cannot_escape",
        "coordinator_crash_triggers_tree_termination",
        "terminal_cancellation_requires_confirmed_empty",
    }


def test_macos_refuses_before_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    launched = False

    def forbidden_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        del args, kwargs
        nonlocal launched
        launched = True
        raise AssertionError("provider process must not start")

    monkeypatch.setattr(process_module.sys, "platform", "darwin")
    monkeypatch.setattr(process_module.subprocess, "Popen", forbidden_popen)

    with pytest.raises(OSError, match="^owned_process_strong_containment_unavailable$"):
        OwnedProcess(("provider-must-not-start",), cwd=".", env={})
    assert launched is False
