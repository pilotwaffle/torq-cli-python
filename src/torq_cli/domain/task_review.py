"""Historical, artifact-backed projection of one exact candidate result."""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from torq_cli.adapters.candidate_provider import parse_candidate_output
from torq_cli.domain.candidate_decision_evidence import (
    REVIEW_CONTRACT,
    validate_candidate_ref,
    validate_decision_ref,
)
from torq_cli.domain.task_evidence import TASK_CONTRACT
from torq_cli.safety.receipts import ReceiptChain, read_verified_artifact, verify_receipt_store
from torq_cli.safety.task_workspace import (
    MAX_TASK_FILES,
    MAX_TASK_FILE_BYTES,
    MAX_TASK_TOTAL_BYTES,
    canonical_json,
    digest_bytes,
    digest_json,
    validate_relative_path,
)

REVIEW_SCHEMA = "torq-task-review-v1"
_MAX_DIFF_CHARS = 262_144
_TASK_ID = re.compile(r"run-task-[a-f0-9]{24}")


def _strict_json(raw: bytes, finding: str) -> Any:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(finding)
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError(finding)

    try:
        return json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError(finding) from exc


def _source_bundle(value: object) -> dict[str, bytes]:
    if not isinstance(value, list) or len(value) > MAX_TASK_FILES:
        raise ValueError("task_review_input_invalid")
    result: dict[str, bytes] = {}
    total = 0
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {
            "path", "content", "content_bytes", "content_hash"
        }:
            raise ValueError("task_review_input_invalid")
        path = row.get("path")
        content = row.get("content")
        size = row.get("content_bytes")
        content_hash = row.get("content_hash")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("task_review_input_invalid")
        path = validate_relative_path(path)
        raw = content.encode("utf-8")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size != len(raw)
            or size > MAX_TASK_FILE_BYTES
            or content_hash != digest_bytes(raw)
            or path.casefold() in {existing.casefold() for existing in result}
        ):
            raise ValueError("task_review_input_invalid")
        total += size
        if total > MAX_TASK_TOTAL_BYTES:
            raise ValueError("task_review_input_invalid")
        result[path] = raw
    return result


def _manifest(files: Mapping[str, bytes]) -> str:
    return digest_json(
        [
            {"path": path, "content_bytes": len(content), "content_hash": digest_bytes(content)}
            for path, content in sorted(files.items(), key=lambda item: item[0].casefold())
        ]
    )


def _newline(value: bytes) -> dict[str, object]:
    crlf = value.count(b"\r\n")
    without_crlf = value.replace(b"\r\n", b"")
    lf = without_crlf.count(b"\n")
    cr = without_crlf.count(b"\r")
    kinds = sum(count > 0 for count in (crlf, lf, cr))
    return {
        "style": "none" if kinds == 0 else "mixed" if kinds > 1 else "crlf" if crlf else "lf" if lf else "cr",
        "crlf_count": crlf,
        "lf_count": lf,
        "cr_count": cr,
        "final_newline": value.endswith((b"\n", b"\r")),
        "bytes": len(value),
    }


def _display_lines(value: bytes) -> list[str]:
    text = value.decode("utf-8")
    result: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.endswith("\r\n"):
            result.append(line[:-2] + " [CRLF]")
        elif line.endswith("\n"):
            result.append(line[:-1] + " [LF]")
        elif line.endswith("\r"):
            result.append(line[:-1] + " [CR]")
        else:
            result.append(line + " [NO EOL]")
    return result


def _change(path: str, before: bytes | None, after: bytes, operation: str) -> dict[str, object]:
    before_text = "" if before is None else before.decode("utf-8")
    after_text = after.decode("utf-8")
    lines = difflib.unified_diff(
        [] if before is None else _display_lines(before),
        _display_lines(after),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="\n",
    )
    rendered = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    complete = len(rendered) <= _MAX_DIFF_CHARS
    return {
        "operation": operation,
        "path": path,
        "base_hash": None if before is None else digest_bytes(before),
        "result_hash": digest_bytes(after),
        "base_text": None if before is None else before_text,
        "result_text": after_text,
        "base_newlines": None if before is None else _newline(before),
        "result_newlines": _newline(after),
        "diff": rendered[:_MAX_DIFF_CHARS],
        "diff_complete": complete,
    }


def _lineage_change(path: str, before: bytes | None, after: bytes | None) -> dict[str, object]:
    if after is None:
        assert before is not None
        return {
            "operation": "dropped_parent_create",
            "path": path,
            "parent_hash": digest_bytes(before),
            "result_hash": None,
            "summary": "The revision drops this parent-created file and restores original absence.",
        }
    operation = "added_in_revision" if before is None else "changed_from_parent"
    change = _change(path, before, after, operation)
    return {
        "operation": operation,
        "path": path,
        "parent_hash": None if before is None else digest_bytes(before),
        "result_hash": digest_bytes(after),
        "diff": change["diff"],
        "diff_complete": change["diff_complete"],
    }


def _verify_correction_decision(
    review_evidence_root: Path,
    value: object,
    parent: "ResolvedTaskReview",
    child_plan_hash: str,
    correction_revision: int,
    correction_hash: str,
) -> None:
    if not validate_decision_ref(value):
        raise ValueError("task_review_correction_decision_invalid")
    assert isinstance(value, Mapping)
    decision_id = str(value["decision_id"])
    root = (review_evidence_root.absolute() / decision_id).absolute()
    if root.parent != review_evidence_root.absolute() or verify_receipt_store(root).status != "verified":
        raise ValueError("task_review_correction_decision_unverified")
    manifest_raw = (root / "terminal-manifest.json").read_bytes()
    rows = [
        _strict_json(line.encode("utf-8"), "task_review_correction_decision_invalid")
        for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise ValueError("task_review_correction_decision_invalid")
    row = rows[0]
    payload = row.get("payload")
    expected = {
        "decision_id": decision_id,
        "decision_sequence": row.get("sequence"),
        "decision_receipt_hash": row.get("receipt_hash"),
        "terminal_manifest_hash": digest_bytes(manifest_raw),
    }
    if (
        dict(value) != expected
        or not isinstance(payload, Mapping)
        or payload.get("review_contract") != REVIEW_CONTRACT
        or payload.get("event") != "correction_requested"
        or payload.get("decision_id") != decision_id
        or payload.get("candidate") != parent.candidate_ref
        or payload.get("detail")
        != {
            "correction_revision": correction_revision,
            "correction_hash": correction_hash,
            "child_plan_hash": child_plan_hash,
        }
    ):
        raise ValueError("task_review_correction_decision_mismatch")


def _verify_application_ref(
    apply_evidence_root: Path,
    review_evidence_root: Path,
    value: object,
    parent: "ResolvedTaskReview",
) -> None:
    if not validate_decision_ref(value):
        raise ValueError("task_review_continuation_application_invalid")
    assert isinstance(value, Mapping)
    application_id = str(value["decision_id"])
    root = (apply_evidence_root.absolute() / application_id).absolute()
    if root.parent != apply_evidence_root.absolute() or verify_receipt_store(root).status != "verified":
        raise ValueError("task_review_continuation_application_unverified")
    manifest_raw = (root / "terminal-manifest.json").read_bytes()
    rows = [
        _strict_json(line.encode("utf-8"), "task_review_continuation_application_invalid")
        for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if not rows or not isinstance(rows[-1], Mapping):
        raise ValueError("task_review_continuation_application_invalid")
    row = rows[-1]
    payload = row.get("payload")
    expected = {
        "decision_id": application_id,
        "decision_sequence": row.get("sequence"),
        "decision_receipt_hash": row.get("receipt_hash"),
        "terminal_manifest_hash": digest_bytes(manifest_raw),
    }
    if (
        dict(value) != expected
        or not isinstance(payload, Mapping)
        or payload.get("event") != "candidate_applied"
        or payload.get("application_id") != application_id
        or payload.get("candidate") != parent.candidate_ref
    ):
        raise ValueError("task_review_continuation_application_mismatch")
    _verify_acceptance_ref(review_evidence_root, payload.get("acceptance"), parent)


def _verify_acceptance_ref(
    review_evidence_root: Path,
    value: object,
    parent: "ResolvedTaskReview",
) -> None:
    if not validate_decision_ref(value):
        raise ValueError("task_review_continuation_acceptance_invalid")
    assert isinstance(value, Mapping)
    decision_id = str(value["decision_id"])
    root = (review_evidence_root.absolute() / decision_id).absolute()
    if root.parent != review_evidence_root.absolute() or verify_receipt_store(root).status != "verified":
        raise ValueError("task_review_continuation_acceptance_unverified")
    manifest_raw = (root / "terminal-manifest.json").read_bytes()
    rows = [
        _strict_json(line.encode("utf-8"), "task_review_continuation_acceptance_invalid")
        for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise ValueError("task_review_continuation_acceptance_invalid")
    row = rows[0]
    payload = row.get("payload")
    expected = {
        "decision_id": decision_id,
        "decision_sequence": row.get("sequence"),
        "decision_receipt_hash": row.get("receipt_hash"),
        "terminal_manifest_hash": digest_bytes(manifest_raw),
    }
    if (
        dict(value) != expected
        or not isinstance(payload, Mapping)
        or payload.get("review_contract") != REVIEW_CONTRACT
        or payload.get("event") != "candidate_accepted"
        or payload.get("decision_id") != decision_id
        or payload.get("candidate") != parent.candidate_ref
    ):
        raise ValueError("task_review_continuation_acceptance_mismatch")
    detail = payload.get("detail")
    if (
        not isinstance(detail, Mapping)
        or detail.get("source_scope_hash") != parent.candidate_ref["base_scope_manifest_hash"]
        or not isinstance(detail.get("review_artifact"), Mapping)
    ):
        raise ValueError("task_review_continuation_acceptance_mismatch")
    artifact = detail["review_artifact"]
    assert isinstance(artifact, Mapping)
    relative = artifact.get("artifact")
    if not isinstance(relative, str):
        raise ValueError("task_review_continuation_acceptance_artifact_invalid")
    plain = read_verified_artifact(review_evidence_root, decision_id, relative)
    if (
        ReceiptChain.hash_file(root / relative) != artifact.get("artifact_hash")
        or digest_bytes(plain) != artifact.get("content_hash")
        or digest_json(_strict_json(plain, "task_review_continuation_acceptance_artifact_invalid"))
        != parent.candidate_ref["review_hash"]
    ):
        raise ValueError("task_review_continuation_acceptance_artifact_mismatch")


@dataclass(frozen=True, slots=True)
class ResolvedTaskReview:
    envelope: dict[str, Any]
    candidate_ref: dict[str, Any]
    plan: dict[str, Any]
    base_files: dict[str, bytes]
    result_files: dict[str, bytes]
    changed_paths: tuple[str, ...]


def resolve_task_review(
    evidence_root: Path,
    task_id: str,
    ready_sequence: int,
) -> ResolvedTaskReview:
    """Resolve one sealed task by exact sequence without consulting live source/cache."""

    if (
        _TASK_ID.fullmatch(task_id) is None
        or not isinstance(ready_sequence, int)
        or isinstance(ready_sequence, bool)
        or ready_sequence <= 0
    ):
        raise ValueError("task_review_sequence_invalid")
    root = (evidence_root.absolute() / task_id).absolute()
    if root.parent != evidence_root.absolute():
        raise ValueError("task_review_task_id_invalid")
    verification = verify_receipt_store(root)
    if verification.status != "verified":
        raise ValueError("task_review_store_unverified")
    manifest_raw = (root / "terminal-manifest.json").read_bytes()
    manifest = _strict_json(manifest_raw, "task_review_manifest_invalid")
    if not isinstance(manifest, Mapping) or manifest.get("sealed") is not True:
        raise ValueError("task_review_store_unsealed")
    receipt_rows = [
        _strict_json(line.encode("utf-8"), "task_review_receipt_invalid")
        for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rows = [row for row in receipt_rows if isinstance(row, Mapping)]
    by_event: dict[str, Mapping[str, Any]] = {}
    row_by_event: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        payload = row.get("payload")
        if isinstance(payload, Mapping) and payload.get("task_contract") == TASK_CONTRACT:
            event = payload.get("event")
            if isinstance(event, str):
                by_event[event] = payload
                row_by_event[event] = row
    required = {"task_start_accepted", "candidate_generated", "candidate_check_completed", "candidate_ready"}
    if not required.issubset(by_event):
        raise ValueError("task_review_receipts_missing")
    start = by_event["task_start_accepted"]
    generated = by_event["candidate_generated"]
    checked = by_event["candidate_check_completed"]
    ready = by_event["candidate_ready"]
    ready_row = row_by_event["candidate_ready"]
    check_row = row_by_event["candidate_check_completed"]
    if ready_row.get("sequence") != ready_sequence or ready.get("task_id") != task_id:
        raise ValueError("task_review_sequence_mismatch")

    input_raw = read_verified_artifact(evidence_root, task_id, str(start["artifact"]))
    candidate_raw = read_verified_artifact(evidence_root, task_id, str(generated["artifact"]))
    check_raw = read_verified_artifact(evidence_root, task_id, str(checked["artifact"]))
    for body, raw in ((start, input_raw), (generated, candidate_raw), (checked, check_raw)):
        artifact_path = root / str(body["artifact"])
        if (
            ReceiptChain.hash_file(artifact_path) != body.get("artifact_hash")
            or digest_bytes(raw) != body.get("artifact_content_hash")
        ):
            raise ValueError("task_review_artifact_hash_mismatch")
    if digest_bytes(input_raw) != start.get("input_hash") or digest_bytes(check_raw) != checked.get("output_hash"):
        raise ValueError("task_review_artifact_identity_mismatch")

    accepted = _strict_json(input_raw, "task_review_input_invalid")
    bundle = _strict_json(candidate_raw, "task_review_candidate_invalid")
    check = _strict_json(check_raw, "task_review_check_invalid")
    if not isinstance(accepted, Mapping) or not isinstance(accepted.get("plan"), Mapping):
        raise ValueError("task_review_input_invalid")
    input_contract = accepted.get("contract")
    if input_contract == "torq-candidate-input-v1":
        if set(accepted) != {"contract", "plan", "files"}:
            raise ValueError("task_review_input_invalid")
    elif input_contract == "torq-candidate-input-v2":
        if set(accepted) != {"contract", "plan", "files", "parent_context", "correction"}:
            raise ValueError("task_review_input_invalid")
    else:
        raise ValueError("task_review_input_invalid")
    plan = dict(accepted["plan"])
    plan_keys = {
        "contract", "project_id", "goal", "draft_revision", "input_paths", "output_paths",
        "absent_output_paths", "base_manifest_hash", "provider", "model", "provider_binary_hash",
        "check_profile_id", "check_profile_version", "helper_hash", "limits",
    }
    plan_contract = plan.get("contract")
    expected_plan_keys = plan_keys if plan_contract == "torq-candidate-plan-v1" else plan_keys | {"lineage"}
    if (
        set(plan) != expected_plan_keys
        or (input_contract, plan_contract)
        not in {
            ("torq-candidate-input-v1", "torq-candidate-plan-v1"),
            ("torq-candidate-input-v2", "torq-candidate-plan-v2"),
        }
    ):
        raise ValueError("task_review_plan_invalid")
    plan_hash = digest_json(plan)
    if plan_hash != start.get("plan_hash") or plan_hash != ready.get("plan_hash"):
        raise ValueError("task_review_plan_hash_mismatch")
    base = _source_bundle(accepted.get("files"))
    for key in ("input_paths", "output_paths", "absent_output_paths"):
        values = plan.get(key)
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError("task_review_plan_invalid")
        safe = [validate_relative_path(item) for item in values]
        if safe != values or len({item.casefold() for item in safe}) != len(safe):
            raise ValueError("task_review_plan_invalid")
    if set(base) != set(plan["input_paths"]):
        raise ValueError("task_review_base_scope_mismatch")
    if set(plan["absent_output_paths"]) != set(plan["output_paths"]) - set(base):
        raise ValueError("task_review_absent_scope_mismatch")
    base_hash = _manifest(base)
    if base_hash != plan.get("base_manifest_hash") or base_hash != start.get("base_manifest_hash"):
        raise ValueError("task_review_base_hash_mismatch")

    lineage_projection: dict[str, Any] | None = None
    parent_result: dict[str, bytes] | None = None
    if plan_contract == "torq-candidate-plan-v2":
        lineage = plan.get("lineage")
        parent_context = accepted.get("parent_context")
        correction = accepted.get("correction")
        if not isinstance(lineage, Mapping) or not isinstance(parent_context, Mapping):
            raise ValueError("task_review_lineage_invalid")
        if set(parent_context) != {"candidate", "files"} or not validate_candidate_ref(parent_context.get("candidate")):
            raise ValueError("task_review_lineage_invalid")
        parent_candidate = dict(parent_context["candidate"])
        parent = resolve_task_review(
            evidence_root, str(parent_candidate["task_id"]), int(parent_candidate["ready_sequence"])
        )
        if parent.candidate_ref != parent_candidate:
            raise ValueError("task_review_lineage_invalid")
        parent_result = _source_bundle(parent_context.get("files"))
        if parent_result != parent.result_files:
            raise ValueError("task_review_parent_context_mismatch")
        mode = lineage.get("mode")
        if mode == "refinement":
            if set(lineage) != {
                "mode", "parent_candidate", "correction_revision", "correction_hash",
                "root_base_scope_hash",
            } or lineage.get("parent_candidate") != parent_candidate:
                raise ValueError("task_review_lineage_invalid")
            if base != parent.base_files or lineage.get("root_base_scope_hash") != base_hash:
                raise ValueError("task_review_lineage_base_mismatch")
            if (
                not isinstance(correction, Mapping)
                or set(correction) != {"revision", "text", "content_hash", "decision"}
                or not isinstance(correction.get("revision"), int)
                or isinstance(correction.get("revision"), bool)
                or correction["revision"] <= 0
                or not isinstance(correction.get("text"), str)
                or digest_bytes(correction["text"].encode("utf-8")) != correction.get("content_hash")
                or correction.get("revision") != lineage.get("correction_revision")
                or correction.get("content_hash") != lineage.get("correction_hash")
                or plan.get("goal") != correction.get("text")
            ):
                raise ValueError("task_review_correction_invalid")
            _verify_correction_decision(
                evidence_root.parent / "review-evidence",
                correction.get("decision"),
                parent,
                plan_hash,
                int(correction["revision"]),
                str(correction["content_hash"]),
            )
            lineage_projection = {
                "mode": "refinement",
                "parent_candidate": parent_candidate,
                "correction_revision": correction["revision"],
                "correction_hash": correction["content_hash"],
                "parent_changes": [],
            }
        elif mode == "continuation":
            if (
                set(lineage) != {"mode", "parent_candidate", "application"}
                or lineage.get("parent_candidate") != parent_candidate
                or correction is not None
                or not validate_decision_ref(lineage.get("application"))
            ):
                raise ValueError("task_review_lineage_invalid")
            _verify_application_ref(
                evidence_root.parent / "apply-evidence",
                evidence_root.parent / "review-evidence",
                lineage["application"],
                parent,
            )
            lineage_projection = {
                "mode": "continuation",
                "parent_candidate": parent_candidate,
                "application": dict(lineage["application"]),
                "parent_changes": [],
            }
        else:
            raise ValueError("task_review_lineage_invalid")

    if (
        not isinstance(bundle, Mapping)
        or set(bundle) != {"contract", "manifest_hash", "operations", "raw_response"}
        or bundle.get("contract") != "torq-candidate-bundle-v1"
        or not isinstance(bundle.get("operations"), list)
        or not isinstance(bundle.get("raw_response"), str)
        or digest_bytes(bundle["raw_response"].encode("utf-8")) != generated.get("raw_response_hash")
    ):
        raise ValueError("task_review_candidate_invalid")
    if start.get("provider") != plan.get("provider") or start.get("model") != plan.get("model"):
        raise ValueError("task_review_provider_mismatch")
    if (
        plan.get("check_profile_id") != "structural-v1"
        or plan.get("check_profile_version") != "1.0.0"
        or checked.get("check_profile_id") != plan.get("check_profile_id")
        or checked.get("check_profile_version") != plan.get("check_profile_version")
        or checked.get("helper_hash") != plan.get("helper_hash")
    ):
        raise ValueError("task_review_check_identity_mismatch")
    output_paths = plan["output_paths"]
    allowed = {validate_relative_path(item) for item in output_paths}
    result = dict(base)
    changed: list[str] = []
    changes: list[dict[str, object]] = []
    if not bundle["operations"] or len(bundle["operations"]) > MAX_TASK_FILES:
        raise ValueError("task_review_operation_invalid")
    parsed, _raw_hash = parse_candidate_output(
        bundle["raw_response"].encode("utf-8"), plan_hash=plan_hash, input_hash=str(start["input_hash"])
    )
    expected_operations: list[dict[str, object]] = []
    for operation in parsed:
        raw_content = str(operation["content"]).encode("utf-8")
        expected_operations.append(
            {**operation, "content_bytes": len(raw_content), "content_hash": digest_bytes(raw_content)}
        )
    if bundle["operations"] != expected_operations:
        raise ValueError("task_review_raw_operation_mismatch")
    changed_folded: set[str] = set()
    for operation in bundle["operations"]:
        if not isinstance(operation, Mapping) or set(operation) != {
            "operation", "path", "base_hash", "content", "content_bytes", "content_hash"
        }:
            raise ValueError("task_review_operation_invalid")
        path_value = operation.get("path")
        content_value = operation.get("content")
        if not isinstance(path_value, str) or not isinstance(content_value, str):
            raise ValueError("task_review_operation_invalid")
        path = validate_relative_path(path_value)
        after = content_value.encode("utf-8")
        before = base.get(path)
        kind = operation.get("operation")
        valid_base = (kind == "create" and before is None and operation.get("base_hash") is None) or (
            kind == "replace" and before is not None and operation.get("base_hash") == digest_bytes(before)
        )
        if (
            not valid_base
            or path not in allowed
            or path.casefold() in changed_folded
            or len(after) > MAX_TASK_FILE_BYTES
            or operation.get("content_bytes") != len(after)
            or operation.get("content_hash") != digest_bytes(after)
            or before == after
        ):
            raise ValueError("task_review_operation_invalid")
        result[path] = after
        changed.append(path)
        changed_folded.add(path.casefold())
        changes.append(_change(path, before, after, str(kind)))
    if (
        not changed
        or len(result) > MAX_TASK_FILES
        or sum(len(value) for value in result.values()) > MAX_TASK_TOTAL_BYTES
        or _manifest(result) != bundle.get("manifest_hash")
        or _manifest(result) != ready.get("candidate_manifest_hash")
    ):
        raise ValueError("task_review_candidate_hash_mismatch")

    if (
        not isinstance(check, Mapping)
        or set(check) != {"contract", "checked", "status"}
        or check.get("contract") != "torq-structural-result-v1"
        or check.get("status") != "passed"
        or checked.get("exit_code") != 0
        or checked.get("output_complete") is not True
        or checked.get("termination") != "confirmed_empty"
    ):
        raise ValueError("task_review_check_invalid")
    validators = {".py": "python_ast", ".json": "strict_json", ".md": "utf8_text", ".txt": "utf8_text"}
    expected_checked = [
        {"path": path, "validator": validators[Path(path).suffix.casefold()]}
        for path in sorted(result, key=str.casefold)
    ]
    if check.get("checked") != expected_checked or checked.get("candidate_manifest_hash") != _manifest(result):
        raise ValueError("task_review_check_mismatch")

    if lineage_projection is not None and parent_result is not None:
        comparison: list[dict[str, object]] = []
        for path in sorted(set(parent_result) | set(result), key=str.casefold):
            parent_content = parent_result.get(path)
            revision_content = result.get(path)
            if parent_content != revision_content:
                comparison.append(_lineage_change(path, parent_content, revision_content))
        lineage_projection["parent_changes"] = comparison

    immutable = {
        "schema": "torq-task-review-core-v1",
        "task_id": task_id,
        "ready_sequence": ready_sequence,
        "plan": {
            "project_id": plan["project_id"], "goal": plan["goal"], "provider": plan["provider"],
            "model": plan["model"], "input_paths": plan["input_paths"], "output_paths": plan["output_paths"],
            "plan_hash": plan_hash, "base_scope_manifest_hash": base_hash,
            "candidate_scope_manifest_hash": _manifest(result),
        },
        "changes": changes,
        "checks": {
            "sequence": check_row["sequence"], "profile_id": checked["check_profile_id"],
            "profile_version": checked["check_profile_version"], "exit_code": checked["exit_code"],
            "output_complete": checked["output_complete"], "termination": checked["termination"],
            "output": check, "helper_hash": checked["helper_hash"], "argv_hash": checked["argv_hash"],
            "command_contract": "python -I -S <structural-v1-helper> <manifest>",
        },
        "lineage": lineage_projection,
    }
    review_hash = digest_json(immutable)
    terminal_hash = digest_bytes(manifest_raw)
    candidate_ref = {
        "task_id": task_id,
        "ready_sequence": ready_sequence,
        "ready_receipt_hash": ready_row["receipt_hash"],
        "terminal_manifest_hash": terminal_hash,
        "plan_hash": plan_hash,
        "base_scope_manifest_hash": base_hash,
        "input_hash": start["input_hash"],
        "candidate_scope_manifest_hash": _manifest(result),
        "check_sequence": check_row["sequence"],
        "check_receipt_hash": check_row["receipt_hash"],
        "review_hash": review_hash,
    }
    envelope = {"schema": REVIEW_SCHEMA, "verified": True, "review_hash": review_hash, "review": immutable}
    if len(canonical_json(envelope).encode("utf-8")) > 2_097_152:
        raise ValueError("task_review_payload_too_large")
    return ResolvedTaskReview(envelope, candidate_ref, plan, base, result, tuple(changed))


__all__ = ["REVIEW_SCHEMA", "ResolvedTaskReview", "resolve_task_review"]
