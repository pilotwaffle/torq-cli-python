from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_fleet_javascript_executes_core_envelope_and_notification_behaviors() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node_runtime_unavailable")
    source_path = Path("src/torq_cli/data/fleet/fleet.js").resolve()
    harness = Path("tests/js/fleet_runtime.test.cjs").resolve()
    completed = subprocess.run(
        [node, str(harness), str(source_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_task_javascript_rejects_delayed_selection_and_mutation_responses() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node_runtime_unavailable")
    source_path = Path("src/torq_cli/data/fleet/task.js").resolve()
    harness = Path("tests/js/task_runtime.test.cjs").resolve()
    completed = subprocess.run(
        [node, str(harness), str(source_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
