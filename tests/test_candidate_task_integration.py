from __future__ import annotations

import hashlib
import os
import sys
import time
import json
from pathlib import Path

import pytest

from torq_cli.adapters.candidate_provider import CandidateProviderCommand
from torq_cli.adapters.process import ContainmentState, ExitObservation, OwnedProcess
from torq_cli.application.candidate_tasks import CandidateTaskService, TaskProject


pytestmark = pytest.mark.skipif(os.name != "nt", reason="strong OwnedProcess fixture is Windows")


def _wait(service: CandidateTaskService, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = service.task(task_id)
        if result["state"] not in {"reserved", "preparing", "generating", "checking", "stop_requested"}:
            return result
        time.sleep(0.02)
    raise AssertionError("candidate task did not terminate")


def test_real_owned_fixture_builds_verified_candidate_without_changing_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    target = source / "calculator.py"
    target.write_text("def add(left, right):\n    return left - right\n", encoding="utf-8")
    original = hashlib.sha256(target.read_bytes()).digest()
    launches: list[list[str]] = []
    fixture = Path("tests/fixtures/candidate_provider.py").resolve()

    def owner(*args: object, **kwargs: object) -> OwnedProcess:
        launches.append(list(args[0]))  # type: ignore[arg-type]
        return OwnedProcess(*args, **kwargs)  # type: ignore[arg-type]

    def provider(prompt: str) -> CandidateProviderCommand:
        return CandidateProviderCommand(
            (sys.executable, "-I", "-S", str(fixture)),
            str(runtime),
            {"PYTHONIOENCODING": "utf-8"},
            prompt.encode(),
            "fixture",
            "local-fixture",
        )

    service = CandidateTaskService(
        projects=[TaskProject("calculator", "Calculator", source)],
        state_root=tmp_path / "state",
        work_root=tmp_path / "work",
        provider_factory=provider,
        provider="fixture",
        model="local-fixture",
        owner_factory=owner,
    )
    try:
        draft = service.save_draft(
            "calculator", goal="Fix addition", input_paths=["calculator.py"],
            output_paths=["calculator.py"], expected_revision=0,
        )
        plan = service.review_plan(
            project_id="calculator", draft_revision=draft["revision"],
            input_paths=["calculator.py"], output_paths=["calculator.py"],
        )
        started = service.start(request_id="request-one", plan_hash=plan["plan_hash"])
        result = _wait(service, started["task_id"])
        candidate = tmp_path / "work" / started["task_id"] / "candidate" / "calculator.py"
        assert result["state"] == "candidate_ready"
        assert result["verified"] is True
        assert result["check"] == {"profile": "structural-v1", "exit_code": 0}
        assert "return left + right" in candidate.read_text(encoding="utf-8")
        assert hashlib.sha256(target.read_bytes()).digest() == original
        assert len(launches) == 2

        replay = service.start(request_id="request-one", plan_hash=plan["plan_hash"])
        assert replay["task_id"] == started["task_id"] and len(launches) == 2
        candidate.with_name("extra.txt").write_text("extra", encoding="utf-8")
        assert service.task(started["task_id"])["state"] == "untrusted"
    finally:
        service.close()


def test_durable_unknown_termination_blocks_new_reservation_and_allows_read_replay(tmp_path: Path) -> None:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    service = CandidateTaskService(
        projects=[TaskProject("calculator", "Calculator", source)],
        state_root=tmp_path / "state", work_root=tmp_path / "work",
        provider_factory=lambda prompt: pytest.fail("provider must not dispatch"),
        provider="fixture", model="local-fixture", acquire_lock=False,
    )
    try:
        task, _ = service.store.reserve("old", {"plan_hash": "old-plan", "project_id": "calculator"})
        service.store.update_task(task["task_id"], state="termination_unknown")
        assert service.capabilities()["can_start_task"] is False
        with pytest.raises(ValueError, match="task_termination_unknown"):
            service.start(request_id="new", plan_hash="missing")
        assert service.store.snapshot()["requests"].get("new") is None
        assert service.start(request_id="old", plan_hash="old-plan")["task_id"] == task["task_id"]
    finally:
        service.close()


class _UnknownOwner:
    pid = 4242

    def __init__(self) -> None:
        self.output_closed = False
        self.stopped = False

    def poll(self) -> int | None:
        return 1 if self.stopped else None

    def next_event(self, *, timeout: float) -> None:
        time.sleep(min(timeout, 0.01))
        return None

    def _observation(self) -> ExitObservation:
        return ExitObservation(1 if self.stopped else None, False, None, True, False, ContainmentState.UNKNOWN)

    def wait(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        return self._observation()

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation:
        del timeout
        self.stopped = True
        self.output_closed = True
        return self._observation()

    def close(self) -> ExitObservation:
        self.stopped = True
        self.output_closed = True
        return self._observation()


def test_unconfirmed_stop_terminalizes_interrupted_and_blocks_late_ready(tmp_path: Path) -> None:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    target = source / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    owner = _UnknownOwner()
    service = CandidateTaskService(
        projects=[TaskProject("project", "Project", source)],
        state_root=tmp_path / "state", work_root=tmp_path / "work",
        provider_factory=lambda prompt: CandidateProviderCommand(
            (sys.executable, "-c", "pass"), str(runtime), {}, prompt.encode(), "fixture", "model"
        ),
        provider="fixture", model="model", owner_factory=lambda *args, **kwargs: owner,
    )
    try:
        draft = service.save_draft(
            "project", goal="change", input_paths=["a.py"], output_paths=["a.py"], expected_revision=0
        )
        plan = service.review_plan(
            project_id="project", draft_revision=draft["revision"], input_paths=["a.py"], output_paths=["a.py"]
        )
        started = service.start(request_id="one", plan_hash=plan["plan_hash"])
        deadline = time.monotonic() + 5
        while service.task(started["task_id"])["state"] != "generating" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service.stop(started["task_id"])["state"] == "termination_unknown"
        result = _wait(service, started["task_id"])
        assert result["state"] == "termination_unknown"
        receipt_path = tmp_path / "state" / "evidence" / started["task_id"] / "receipts.jsonl"
        rows = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8").splitlines()]
            if rows[-1]["payload"]["event"] == "task_interrupted":
                break
            time.sleep(0.01)
        assert rows[-1]["payload"]["event"] == "task_interrupted"
        assert rows[-1]["payload"]["termination"] == "unknown"
        with pytest.raises(ValueError, match="task_termination_unknown"):
            service.start(request_id="two", plan_hash=plan["plan_hash"])
    finally:
        service.close()
