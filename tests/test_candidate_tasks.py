from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from torq_cli.adapters.candidate_provider import (
    CandidateProviderCommandFactory,
    OUTPUT_CONTRACT,
    parse_candidate_output,
    trusted_python_executable,
)
from torq_cli.application.task_store import TaskStore
from torq_cli.safety.task_workspace import (
    digest_bytes,
    materialize_candidate,
    snapshot_candidate_exact,
    snapshot_source,
    validate_relative_path,
)


def test_trusted_running_interpreter_resolves_alias_before_native_validation() -> None:
    assert trusted_python_executable() == str(Path(sys.executable).resolve(strict=True))


def test_candidate_output_is_duplicate_free_finite_and_plan_bound() -> None:
    digest = "sha256:" + "1" * 64
    raw = json.dumps(
        {
            "contract": OUTPUT_CONTRACT,
            "plan_hash": digest,
            "input_hash": digest,
            "operations": [
                {"operation": "create", "path": "answer.py", "base_hash": None, "content": "x = 1\n"}
            ],
        },
        separators=(",", ":"),
    ).encode()
    operations, raw_hash = parse_candidate_output(raw, plan_hash=digest, input_hash=digest)
    assert operations[0]["path"] == "answer.py"
    assert raw_hash == digest_bytes(raw)
    with pytest.raises(ValueError, match="task_provider_output_invalid"):
        parse_candidate_output(b'{"contract":"x","contract":"y"}', plan_hash=digest, input_hash=digest)
    invalid = raw.replace(b'"x = 1\\n"', b'NaN')
    with pytest.raises(ValueError):
        parse_candidate_output(invalid, plan_hash=digest, input_hash=digest)


@pytest.mark.parametrize(
    "value",
    ("../a.py", "./a.py", "a//b.py", ".ssh/key.py", "keys/a.py", ".env.local/a.py", "a.py:stream"),
)
def test_task_paths_reject_escape_alias_and_protected_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_relative_path(value)


def test_empty_source_can_create_candidate_and_extra_inventory_downgrades(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    base = snapshot_source(project, ())
    candidate = tmp_path / "work" / "candidate"
    result = materialize_candidate(
        candidate,
        base,
        ({"operation": "create", "path": "answer.py", "base_hash": None, "content": "x = 1\n"},),
        allowed_output_paths=("answer.py", "notes.md"),
    )
    assert snapshot_candidate_exact(candidate, ("answer.py",)).manifest_hash == result.manifest_hash
    (candidate / "extra.txt").write_text("unrecorded", encoding="utf-8")
    with pytest.raises(ValueError, match="task_candidate_inventory"):
        snapshot_candidate_exact(candidate, ("answer.py",))


def test_task_store_draft_delete_is_monotonic_and_request_body_is_closed(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    first = store.put_draft(
        "project", goal="Build it", input_paths=["a.py"], output_paths=["a.py"], expected_revision=0
    )
    store.delete_draft("project", expected_revision=first["revision"])
    with pytest.raises(ValueError, match="task_draft_revision_conflict"):
        store.put_draft(
            "project", goal="stale", input_paths=["a.py"], output_paths=["a.py"], expected_revision=0
        )
    recreated = store.put_draft(
        "project", goal="new", input_paths=["a.py"], output_paths=["a.py"], expected_revision=2
    )
    assert recreated["revision"] == 3
    with pytest.raises(ValueError, match="task_request_body_invalid"):
        store.reserve("request", {"plan_hash": "p", "project_id": "project", "state": "candidate_ready"})


def test_task_store_idempotency_conflicts_on_changed_body(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    task, created = store.reserve("request", {"plan_hash": "p1", "project_id": "project"})
    replay, created_again = store.reserve("request", {"plan_hash": "p1", "project_id": "project"})
    assert created is True and created_again is False and replay["task_id"] == task["task_id"]
    with pytest.raises(ValueError, match="task_request_replay_conflict"):
        store.reserve("request", {"plan_hash": "p2", "project_id": "project"})


@pytest.mark.skipif(os.name != "nt", reason="Windows wrapper safety")
def test_claude_batch_wrapper_is_not_treated_as_native_owned_executable(tmp_path: Path) -> None:
    wrapper = tmp_path / "claude.BAT"
    wrapper.write_text("@echo off\n", encoding="ascii")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    factory = CandidateProviderCommandFactory(
        provider="claude", model="sonnet", runtime_root=runtime,
        base_environment={}, claude_binary=wrapper,
    )
    with pytest.raises(ValueError, match="task_provider_binary_not_native"):
        factory.preflight()
