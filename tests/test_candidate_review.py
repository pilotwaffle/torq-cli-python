from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from torq_cli.adapters.candidate_provider import CandidateProviderCommand
from torq_cli.adapters.process import OwnedProcess
from torq_cli.application.candidate_review import CandidateReviewService
from torq_cli.application.candidate_tasks import CandidateTaskService, TaskProject
from torq_cli.safety.primary_transaction import AnchoredPrimary
from torq_cli.safety.receipts import verify_receipt_store

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native primary transaction fixture is Windows")


def _ready(service: CandidateTaskService, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = service.task(task_id)
        if result["state"] not in {"reserved", "preparing", "generating", "checking"}:
            return result
        time.sleep(0.02)
    raise AssertionError("candidate did not finish")


def _fixture(tmp_path: Path) -> tuple[CandidateTaskService, Path, list[list[str]]]:
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    source.mkdir()
    runtime.mkdir()
    target = source / "calculator.py"
    target.write_bytes(b"def add(left, right):\r\n    return left - right")
    launches: list[list[str]] = []
    provider_path = Path("tests/fixtures/candidate_provider.py").resolve()

    def owner(*args: object, **kwargs: object) -> OwnedProcess:
        launches.append(list(args[0]))  # type: ignore[arg-type]
        return OwnedProcess(*args, **kwargs)  # type: ignore[arg-type]

    def provider(prompt: str) -> CandidateProviderCommand:
        return CandidateProviderCommand(
            (sys.executable, "-I", "-S", str(provider_path)), str(runtime),
            {"PYTHONIOENCODING": "utf-8"}, prompt.encode(), "fixture", "local-fixture",
        )

    service = CandidateTaskService(
        projects=[TaskProject("calculator", "Calculator", source)],
        state_root=tmp_path / "state", work_root=tmp_path / "work",
        provider_factory=provider, provider="fixture", model="local-fixture", owner_factory=owner,
    )
    return service, target, launches


def test_review_accept_apply_and_new_session_replay_are_evidence_backed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, target, launches = _fixture(tmp_path)
    try:
        draft = tasks.save_draft(
            "calculator", goal="Fix addition", input_paths=["calculator.py"],
            output_paths=["calculator.py"], expected_revision=0,
        )
        plan = tasks.review_plan(
            project_id="calculator", draft_revision=draft["revision"],
            input_paths=["calculator.py"], output_paths=["calculator.py"],
        )
        started = tasks.start(request_id="build-one", plan_hash=plan["plan_hash"])
        assert _ready(tasks, str(started["task_id"]))["state"] == "candidate_ready"
        assert len(launches) == 2
        service = CandidateReviewService(
            tasks=tasks, state_root=tmp_path / "state", common_authority_root=tmp_path / "authority",
        )
        review = service.review(str(started["task_id"]), 4)
        change = review["review"]["changes"][0]
        assert "[CRLF]" in change["diff"] and "[NO EOL]" in change["diff"]
        accepted = service.accept(
            request_id="accept-one", task_id=str(started["task_id"]), ready_sequence=4,
            review_hash=review["review_hash"], subject_id="session-first",
        )
        assert accepted["state"] == "accepted"
        assert b"left - right" in target.read_bytes()
        replay = service.accept(
            request_id="accept-one", task_id=str(started["task_id"]), ready_sequence=4,
            review_hash=review["review_hash"], subject_id="session-after-restart",
        )
        assert replay == accepted
        applied = service.apply(
            request_id="apply-one", candidate=accepted["candidate"],
            acceptance=accepted["acceptance"], subject_id="session-first",
        )
        assert applied["state"] == "applied"
        assert target.read_bytes() == b"def add(left, right):\n    return left + right\n"
        replay_apply = service.apply(
            request_id="apply-one", candidate=accepted["candidate"],
            acceptance=accepted["acceptance"], subject_id="session-after-restart",
        )
        assert replay_apply == applied
        original_context = service.store.save_continuation_context
        original_update = service.store.update

        def interrupted_context(*args: object, **kwargs: object) -> None:
            raise SystemExit("simulated process death after durable draft")

        def interrupted_update(*args: object, **kwargs: object) -> dict[str, object]:
            raise SystemExit("simulated process death before reservation completion")

        monkeypatch.setattr(service.store, "save_continuation_context", interrupted_context)
        monkeypatch.setattr(service.store, "update", interrupted_update)
        with pytest.raises(SystemExit, match="reservation completion"):
            service.continue_task(
                request_id="continue-one", application_id=applied["application_id"]
            )
        monkeypatch.setattr(service.store, "save_continuation_context", original_context)
        monkeypatch.setattr(service.store, "update", original_update)
        continuation = service.continue_task(
            request_id="continue-one", application_id=applied["application_id"]
        )
        assert service.continue_task(
            request_id="continue-one", application_id=applied["application_id"]
        ) == continuation
        continued_draft = continuation["draft"]
        continued_draft = tasks.save_draft(
            "calculator", goal="Add another verified change",
            input_paths=continued_draft["input_paths"],
            output_paths=continued_draft["output_paths"],
            expected_revision=continued_draft["revision"],
        )
        continued_plan = tasks.review_plan(
            project_id="calculator", draft_revision=continued_draft["revision"],
            input_paths=continued_draft["input_paths"],
            output_paths=continued_draft["output_paths"],
        )
        assert continued_plan["contract"] == "torq-candidate-plan-v2"
        assert continued_plan["lineage"]["mode"] == "continuation"
        newest_draft = tasks.save_draft(
            "calculator", goal="A newer continuation goal",
            input_paths=continued_draft["input_paths"],
            output_paths=continued_draft["output_paths"],
            expected_revision=continued_draft["revision"],
        )
        with pytest.raises(ValueError, match="task_plan_stale"):
            tasks.start(request_id="stale-continuation", plan_hash=continued_plan["plan_hash"])
        tasks.delete_draft("calculator", expected_revision=newest_draft["revision"])
        empty_draft = tasks.draft("calculator")
        assert empty_draft is not None
        original_save = tasks.save_draft
        raced = False

        def racing_save(
            project_id: str, *, goal: str, input_paths: list[str],
            output_paths: list[str], expected_revision: int,
        ) -> dict[str, object]:
            nonlocal raced
            if not raced:
                raced = True
                original_save(
                    project_id, goal="Concurrent ordinary draft",
                    input_paths=input_paths, output_paths=output_paths,
                    expected_revision=expected_revision,
                )
            return original_save(
                project_id, goal=goal, input_paths=input_paths,
                output_paths=output_paths, expected_revision=expected_revision,
            )

        monkeypatch.setattr(tasks, "save_draft", racing_save)
        with pytest.raises(ValueError, match="task_draft_revision_conflict"):
            service.continue_task(
                request_id="continue-cas-race", application_id=applied["application_id"]
            )
        assert service.store.active_continuation("calculator") is None
        concurrent = tasks.draft("calculator")
        assert concurrent is not None and concurrent["goal"] == "Concurrent ordinary draft"
        ordinary_plan = tasks.review_plan(
            project_id="calculator", draft_revision=concurrent["revision"],
            input_paths=concurrent["input_paths"], output_paths=concurrent["output_paths"],
        )
        assert ordinary_plan["contract"] == "torq-candidate-plan-v1"
        assert len(launches) == 2
        assert verify_receipt_store(
            service.review_evidence_root / accepted["acceptance"]["decision_id"]
        ).status == "verified"
        assert verify_receipt_store(service.apply_evidence_root / applied["application_id"]).status == "verified"
    finally:
        tasks.close()


def test_correction_creates_verified_immutable_child_task(tmp_path: Path) -> None:
    tasks, target, launches = _fixture(tmp_path)
    try:
        draft = tasks.save_draft(
            "calculator", goal="Fix addition", input_paths=["calculator.py"],
            output_paths=["calculator.py"], expected_revision=0,
        )
        plan = tasks.review_plan(
            project_id="calculator", draft_revision=draft["revision"],
            input_paths=draft["input_paths"], output_paths=draft["output_paths"],
        )
        parent = tasks.start(request_id="build-parent", plan_hash=plan["plan_hash"])
        assert _ready(tasks, str(parent["task_id"]))["state"] == "candidate_ready"
        reviews = CandidateReviewService(
            tasks=tasks, state_root=tmp_path / "state",
            common_authority_root=tmp_path / "authority",
        )
        parent_review = reviews.review(str(parent["task_id"]), 4)
        correction = reviews.save_correction(
            str(parent["task_id"]), 4, text="Keep the corrected addition implementation.",
            expected_revision=0,
        )
        child_plan = reviews.review_child_plan(
            str(parent["task_id"]), 4, request_id="review-child",
            correction_revision=correction["revision"],
            correction_hash=correction["content_hash"],
            review_hash=parent_review["review_hash"], subject_id="session-one",
        )
        assert reviews.review_child_plan(
            str(parent["task_id"]), 4, request_id="review-child",
            correction_revision=correction["revision"],
            correction_hash=correction["content_hash"],
            review_hash=parent_review["review_hash"], subject_id="session-two",
        ) == child_plan
        child = reviews.start_revision(
            request_id="start-child", plan_hash=child_plan["plan_hash"]
        )
        child_ready = _ready(tasks, str(child["task_id"]))
        assert child_ready["state"] == "candidate_ready"
        child_review = reviews.review(str(child["task_id"]), int(child_ready["ready_sequence"]))
        assert child_review["review"]["lineage"]["mode"] == "refinement"
        assert child_review["review"]["lineage"]["parent_candidate"]["task_id"] == parent["task_id"]
        assert target.read_bytes() == b"def add(left, right):\r\n    return left - right"
        assert len(launches) == 4
    finally:
        tasks.close()


def test_recovery_resumes_signed_rollback_after_native_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks, target, launches = _fixture(tmp_path)
    original = target.read_bytes()
    try:
        draft = tasks.save_draft(
            "calculator", goal="Fix addition", input_paths=["calculator.py"],
            output_paths=["calculator.py"], expected_revision=0,
        )
        plan = tasks.review_plan(
            project_id="calculator", draft_revision=draft["revision"],
            input_paths=draft["input_paths"], output_paths=draft["output_paths"],
        )
        started = tasks.start(request_id="rollback-build", plan_hash=plan["plan_hash"])
        assert _ready(tasks, str(started["task_id"]))["state"] == "candidate_ready"
        service = CandidateReviewService(
            tasks=tasks, state_root=tmp_path / "state",
            common_authority_root=tmp_path / "authority",
        )
        review = service.review(str(started["task_id"]), 4)
        accepted = service.accept(
            request_id="rollback-accept", task_id=str(started["task_id"]),
            ready_sequence=4, review_hash=review["review_hash"], subject_id="session",
        )
        native_replace = AnchoredPrimary.replace
        forward_failed = False

        def interrupted_replace(self: AnchoredPrimary, *args: object, **kwargs: object) -> object:
            nonlocal forward_failed
            result = native_replace(self, *args, **kwargs)  # type: ignore[arg-type]
            if kwargs.get("rollback") is True:
                raise SystemExit("simulated death after rollback rename")
            if not forward_failed:
                forward_failed = True
                raise RuntimeError("task_injected_apply_failure")
            return result

        monkeypatch.setattr(AnchoredPrimary, "replace", interrupted_replace)
        interrupted = service.apply(
            request_id="rollback-apply", candidate=accepted["candidate"],
            acceptance=accepted["acceptance"], subject_id="session",
        )
        assert interrupted["state"] == "recovery_required"
        assert target.read_bytes() == original
        monkeypatch.setattr(AnchoredPrimary, "replace", native_replace)

        restarted = CandidateReviewService(
            tasks=tasks, state_root=tmp_path / "state",
            common_authority_root=tmp_path / "authority",
        )
        recovered = restarted.recover("calculator")
        assert recovered["state"] == "rolled_back"
        assert restarted.recover("calculator")["state"] == "idle"
        assert target.read_bytes() == original
        assert len(launches) == 2
    finally:
        tasks.close()
