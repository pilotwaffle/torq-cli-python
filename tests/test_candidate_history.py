from pathlib import Path
from types import SimpleNamespace

import pytest

from torq_cli.application.candidate_review import CandidateReviewService
from torq_cli.application.task_store import TaskStore


def test_history_cursor_survives_new_tasks_and_ordinals_survive_restart(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    plan = store.save_plan({"project_id": "app", "goal": "Straße correction"})
    body = {"project_id": "app", "plan_hash": plan["plan_hash"]}
    first, _ = store.reserve("first", body)
    second, _ = store.reserve("second", body)
    restarted = TaskStore(tmp_path / "state")
    assert restarted.reserve("first", body) == (first, False)
    third, _ = restarted.reserve("third", body)
    assert [item["history_ordinal"] for item in (first, second, third)] == [1, 2, 3]

    service = CandidateReviewService.__new__(CandidateReviewService)
    service.tasks = SimpleNamespace(  # type: ignore[assignment]
        store=restarted, projects={"app": SimpleNamespace(label="My project")},
        evidence_root=tmp_path / "evidence",
    )
    page = service.history(query="STRASSE", limit=1)
    assert page["items"][0]["task_id"] == third["task_id"]
    assert page["next_cursor"] == third["task_id"]
    newest, _ = restarted.reserve("newest", body)
    next_page = service.history(query="STRASSE", limit=1, cursor=page["next_cursor"])
    assert next_page["items"][0]["task_id"] == second["task_id"]
    last = service.history(query="STRASSE", limit=1, cursor=next_page["next_cursor"])
    assert last["items"][0]["task_id"] == first["task_id"]
    assert last["next_cursor"] is None
    assert service.history(limit=1)["items"][0]["task_id"] == newest["task_id"]
    with pytest.raises(ValueError, match="cursor_invalid"):
        service.history(cursor="not-a-task")
    with pytest.raises(ValueError, match="query_invalid"):
        service.history(limit=True)


def test_legacy_history_keeps_explicit_undated_fallback_without_rewriting(tmp_path: Path) -> None:
    store = TaskStore(tmp_path / "state")
    plan = store.save_plan({"project_id": "app", "goal": "Legacy"})
    task, _ = store.reserve("legacy", {"project_id": "app", "plan_hash": plan["plan_hash"]})
    data = store.snapshot()
    del data["tasks"][task["task_id"]]["history_ordinal"]
    data["tasks"]["../outside"] = dict(task)
    store._write(data)
    before = store.path.read_bytes()
    service = CandidateReviewService.__new__(CandidateReviewService)
    service.tasks = SimpleNamespace(  # type: ignore[assignment]
        store=store, projects={}, evidence_root=tmp_path / "evidence",
    )
    items = service.history()["items"]
    assert len(items) == 1
    row = items[0]
    assert row["created_at"] is None and row["legacy_order"] is True
    assert row["history_ordinal"] is None
    assert store.path.read_bytes() == before
