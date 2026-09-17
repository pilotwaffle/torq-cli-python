from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_task_javascript_runtime_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node_runtime_unavailable")
    result = subprocess.run(
        [
            node,
            str(Path("tests/js/task_runtime.test.cjs").resolve()),
            str(Path("src/torq_cli/data/fleet/task.js").resolve()),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "task runtime contract checks passed" in result.stdout


def test_resumed_task_refreshes_capability_for_next_plan() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node_runtime_unavailable")
    result = subprocess.run(
        [
            node,
            str(Path("tests/js/task_resume.test.cjs").resolve()),
            str(Path("src/torq_cli/data/fleet/task.js").resolve()),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "resumed task capability refresh checks passed" in result.stdout
