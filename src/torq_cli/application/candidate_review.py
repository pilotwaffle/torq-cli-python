"""Verified candidate review, signed acceptance, and explicit primary application."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.application.candidate_tasks import CandidateTaskService
from torq_cli.application.review_store import ReviewStore
from torq_cli.domain.candidate_decision_evidence import (
    APPLY_CONTRACT,
    REVIEW_CONTRACT,
    validate_decision_ref,
)
from torq_cli.domain.task_review import ResolvedTaskReview, resolve_task_review
from torq_cli.safety.evidence_broker import EvidenceBroker
from torq_cli.safety.primary_marker import PrimaryMarkerAuthority
from torq_cli.safety.primary_transaction import AnchoredFile, AnchoredPrimary
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    read_verified_artifact,
    read_verified_prefix_artifact,
    verify_receipt_store,
)
from torq_cli.safety.task_workspace import canonical_json, digest_bytes, digest_json, validate_disjoint_roots


def _manifest(files: Mapping[str, bytes]) -> str:
    return digest_json(
        [
            {"path": path, "content_bytes": len(content), "content_hash": digest_bytes(content)}
            for path, content in sorted(files.items(), key=lambda item: item[0].casefold())
        ]
    )


def _append(broker: EvidenceBroker, body: Mapping[str, Any]) -> dict[str, Any]:
    capability = broker.issue("orchestrator")
    return broker.append(capability.token, "audit", body)


def _artifact(broker: EvidenceBroker, name: str, content: str) -> dict[str, str]:
    capability = broker.issue("orchestrator")
    path = broker.write_artifact(capability.token, name, content)
    if broker.read_artifact(path).encode("utf-8") != content.encode("utf-8"):
        raise ValueError("candidate_artifact_content_changed")
    return {
        "artifact": path.relative_to(broker.run_root).as_posix(),
        "artifact_hash": ReceiptChain.hash_file(path),
        "content_hash": digest_bytes(content.encode("utf-8")),
    }


class CandidateReviewService:
    """Single service boundary used by Fleet HTTP and recovery startup."""

    def __init__(
        self,
        *,
        tasks: CandidateTaskService,
        state_root: Path,
        common_authority_root: Path,
    ) -> None:
        self.tasks = tasks
        self.state_root = state_root.absolute()
        if self.state_root != tasks.state_root:
            raise ValueError("candidate_review_state_root_mismatch")
        validate_disjoint_roots(
            [
                *(project.root for project in tasks.projects.values()),
                tasks.state_root,
                tasks.work_root,
                common_authority_root.absolute(),
            ]
        )
        self.store = ReviewStore(self.state_root)
        self.review_evidence_root = self.state_root / "review-evidence"
        self.apply_evidence_root = self.state_root / "apply-evidence"
        self.review_evidence_root.mkdir(parents=True, exist_ok=True)
        self.apply_evidence_root.mkdir(parents=True, exist_ok=True)
        self.markers = PrimaryMarkerAuthority(common_authority_root)
        self.tasks.project_gate = self.markers.assert_clear
        self.tasks.input_bundle_provider = self._child_input_bundle
        self.tasks.plan_transformer = self._transform_plan
        self.tasks.draft_saved_observer = self._draft_saved
        self.tasks.draft_deleted_observer = self._draft_deleted

    def review(self, task_id: str, ready_sequence: int) -> dict[str, Any]:
        resolved = resolve_task_review(self.tasks.evidence_root, task_id, ready_sequence)
        acceptance: dict[str, Any] | None = None
        application: dict[str, Any] | None = None
        snapshot = self.store.snapshot()
        for record in sorted(snapshot["decisions"].values(), key=lambda item: item.get("ordinal", 0), reverse=True):
            if not isinstance(record, Mapping):
                continue
            request = record.get("request")
            if isinstance(request, Mapping) and (
                request.get("task_id"), request.get("ready_sequence"), request.get("review_hash")
            ) == (task_id, ready_sequence, resolved.candidate_ref["review_hash"]):
                try:
                    acceptance = self._replay_accept(record)
                    break
                except (OSError, ValueError):
                    continue
        if acceptance is not None:
            for record in sorted(snapshot["applications"].values(), key=lambda item: item.get("ordinal", 0), reverse=True):
                if not isinstance(record, Mapping):
                    continue
                request = record.get("request")
                if isinstance(request, Mapping) and request.get("candidate") == resolved.candidate_ref and request.get("acceptance") == acceptance.get("acceptance"):
                    try:
                        application = self._replay_application(record)
                        break
                    except (OSError, ValueError):
                        continue
        reason: str | None = None
        try:
            self._assert_project_clear(resolved)
            self._assert_source_base(resolved)
        except (OSError, RuntimeError, ValueError) as exc:
            reason = self._reason(exc, "candidate_source_unavailable")
        application_state = None if application is None else application.get("state")
        application_blocks_apply = application_state in {"applied", "recovery_required"}
        envelope = dict(resolved.envelope)
        envelope["eligibility"] = {
            "can_accept": reason is None and acceptance is None,
            "can_apply": reason is None and acceptance is not None and not application_blocks_apply,
            "reason": reason or (
                "candidate_already_applied" if application_state == "applied"
                else "candidate_apply_recovery_required" if application_state == "recovery_required"
                else "candidate_already_accepted" if acceptance is not None and application is None
                else None
            ),
        }
        envelope["acceptance"] = acceptance
        envelope["application"] = application
        return envelope

    def correction(self, task_id: str, ready_sequence: int) -> dict[str, Any]:
        resolve_task_review(self.tasks.evidence_root, task_id, ready_sequence)
        return self.store.correction(task_id, ready_sequence) or {
            "task_id": task_id, "ready_sequence": ready_sequence, "revision": 0,
            "text": "", "content_hash": digest_bytes(b""),
        }

    def save_correction(
        self,
        task_id: str,
        ready_sequence: int,
        *,
        text: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        resolved = resolve_task_review(self.tasks.evidence_root, task_id, ready_sequence)
        self._assert_project_clear(resolved)
        return self.store.save_correction(
            task_id, ready_sequence, text=text, expected_revision=expected_revision
        )

    def review_child_plan(
        self,
        task_id: str,
        ready_sequence: int,
        *,
        request_id: str,
        correction_revision: int,
        correction_hash: str,
        review_hash: str,
        subject_id: str,
    ) -> dict[str, Any]:
        resolved = resolve_task_review(self.tasks.evidence_root, task_id, ready_sequence)
        correction = self.store.correction(task_id, ready_sequence)
        if correction is None or correction.get("revision") != correction_revision:
            raise ValueError("candidate_correction_revision_stale")
        if (
            correction.get("content_hash") != correction_hash
            or resolved.candidate_ref["review_hash"] != review_hash
        ):
            raise ValueError("candidate_child_context_invalid")
        body = {
            "task_id": task_id, "ready_sequence": ready_sequence,
            "review_hash": resolved.candidate_ref["review_hash"],
            "correction_revision": correction_revision,
            "correction_hash": correction["content_hash"],
        }
        record, created = self.store.reserve(
            request_kind="child", request_id=request_id, body=body, id_prefix="decision-"
        )
        if not created:
            return self._replay_child_plan(record)
        decision_id = str(record["record_id"])
        try:
            self._assert_project_clear(resolved)
            self._assert_source_base(resolved)
            parent_plan = resolved.plan
            plan = {
                "contract": "torq-candidate-plan-v2",
                "project_id": parent_plan["project_id"], "goal": correction["text"],
                "draft_revision": correction_revision,
                "input_paths": parent_plan["input_paths"], "output_paths": parent_plan["output_paths"],
                "absent_output_paths": parent_plan["absent_output_paths"],
                "base_manifest_hash": resolved.candidate_ref["base_scope_manifest_hash"],
                "provider": parent_plan["provider"], "model": parent_plan["model"],
                "provider_binary_hash": parent_plan["provider_binary_hash"],
                "check_profile_id": parent_plan["check_profile_id"],
                "check_profile_version": parent_plan["check_profile_version"],
                "helper_hash": parent_plan["helper_hash"], "limits": parent_plan["limits"],
                "lineage": {
                    "mode": "refinement", "parent_candidate": resolved.candidate_ref,
                    "correction_revision": correction_revision,
                    "correction_hash": correction["content_hash"],
                    "root_base_scope_hash": resolved.candidate_ref["base_scope_manifest_hash"],
                },
            }
            saved = self.tasks.store.save_plan(plan)
            plan_hash = str(saved["plan_hash"])
            chain = ReceiptChain(
                self.review_evidence_root, decision_id, FileRunKeyStore(self.review_evidence_root),
                profile_version="candidate-review-v1", policy_version="candidate-review-v1",
            )
            broker = EvidenceBroker(chain)
            receipt = _append(broker, {
                "review_contract": REVIEW_CONTRACT, "event": "correction_requested",
                "decision_id": decision_id, "request_digest": record["request_digest"],
                "candidate": resolved.candidate_ref,
                "actor": {"subject_id": subject_id, "assurance": "local_operator_session"},
                "detail": {
                    "correction_revision": correction_revision,
                    "correction_hash": correction["content_hash"], "child_plan_hash": plan_hash,
                },
            })
            manifest_path = broker.seal()
            decision = {
                "decision_id": decision_id, "decision_sequence": receipt["sequence"],
                "decision_receipt_hash": receipt["receipt_hash"],
                "terminal_manifest_hash": digest_bytes(manifest_path.read_bytes()),
            }
            context = {
                "plan_hash": plan_hash, "parent_candidate": resolved.candidate_ref,
                "correction_decision": decision, "correction_revision": correction_revision,
                "correction_hash": correction["content_hash"],
            }
            self.store.save_child_plan(plan_hash, context)
            result = {
                "schema": "torq-candidate-child-plan-v1", "plan": saved,
                "plan_hash": plan_hash, "correction_decision": decision,
            }
            self.store.update(decision_id, application=False, state="child_plan_ready", result=result)
            return result
        except BaseException as exc:
            finding = self._reason(exc, "candidate_child_plan_failed")
            self.store.update(decision_id, application=False, state="failed", finding=finding)
            raise ValueError(finding) from exc

    def start_revision(self, *, request_id: str, plan_hash: str, replay_only: bool = False) -> dict[str, Any]:
        self.store.child_plan(plan_hash)
        method = self.tasks.replay_start if replay_only else self.tasks.start
        return method(request_id=request_id, plan_hash=plan_hash)

    def continue_task(self, *, request_id: str, application_id: str) -> dict[str, Any]:
        body = {"application_id": application_id}
        record, created = self.store.reserve(
            request_kind="continuation", request_id=request_id, body=body,
            id_prefix="continuation-",
        )
        if not created:
            return self._replay_continuation(record)
        try:
            return self._complete_continuation(record)
        except BaseException as exc:
            finding = self._reason(exc, "candidate_continuation_failed")
            self.store.update(
                str(record["record_id"]), application=False, state="failed", finding=finding
            )
            raise ValueError(finding) from exc

    def replay_continuation(
        self, *, request_id: str, application_id: str
    ) -> dict[str, Any]:
        return self._replay_continuation(
            self.store.replay(
                request_kind="continuation", request_id=request_id,
                body={"application_id": application_id},
            )
        )

    def replay_child_plan(
        self,
        task_id: str,
        ready_sequence: int,
        *,
        request_id: str,
        correction_revision: int,
        correction_hash: str,
        review_hash: str,
    ) -> dict[str, Any]:
        body = {
            "task_id": task_id,
            "ready_sequence": ready_sequence,
            "review_hash": review_hash,
            "correction_revision": correction_revision,
            "correction_hash": correction_hash,
        }
        return self._replay_child_plan(
            self.store.replay(request_kind="child", request_id=request_id, body=body)
        )

    def _child_input_bundle(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        plan_hash = digest_json(plan)
        context = self.store.child_plan(plan_hash)
        parent = context.get("parent_candidate")
        if not isinstance(parent, Mapping):
            raise ValueError("candidate_child_context_invalid")
        resolved = self._resolve_candidate_ref(parent)
        mode = plan.get("lineage", {}).get("mode") if isinstance(plan.get("lineage"), Mapping) else None
        if mode == "continuation":
            application_id = context.get("application_id")
            if not isinstance(application_id, str):
                raise ValueError("candidate_continuation_context_invalid")
            application = self.application(application_id)
            application_ref = self._application_ref(application)
            if (
                application.get("state") != "applied"
                or context.get("application") != application_ref
                or plan.get("lineage") != {
                    "mode": "continuation", "parent_candidate": resolved.candidate_ref,
                    "application": application_ref,
                }
            ):
                raise ValueError("candidate_continuation_context_invalid")
            project = self.tasks.projects[str(plan["project_id"])]
            from torq_cli.safety.task_workspace import snapshot_source

            fresh = snapshot_source(project.root, [str(item) for item in plan["input_paths"]])
            if fresh.manifest_hash != plan.get("base_manifest_hash"):
                raise ValueError("candidate_continuation_context_invalid")
            value = {
                "contract": "torq-candidate-input-v2", "plan": dict(plan),
                "files": fresh.bundle(),
                "parent_context": {
                    "candidate": resolved.candidate_ref,
                    "files": self._bundle(resolved.result_files),
                },
                "correction": None,
            }
            if len(canonical_json(value).encode("utf-8")) > 4_194_304:
                raise ValueError("candidate_child_input_too_large")
            return value
        correction = self.store.correction(str(parent["task_id"]), int(parent["ready_sequence"]))
        if (
            correction is None
            or correction.get("revision") != context.get("correction_revision")
            or correction.get("content_hash") != context.get("correction_hash")
        ):
            raise ValueError("candidate_child_context_invalid")
        decision = context.get("correction_decision")
        self._verify_correction_decision(decision, resolved, plan_hash, correction)
        value = {
            "contract": "torq-candidate-input-v2", "plan": dict(plan),
            "files": self._bundle(resolved.base_files),
            "parent_context": {
                "candidate": resolved.candidate_ref, "files": self._bundle(resolved.result_files),
            },
            "correction": {
                "revision": correction["revision"], "text": correction["text"],
                "content_hash": correction["content_hash"], "decision": decision,
            },
        }
        if len(canonical_json(value).encode("utf-8")) > 4_194_304:
            raise ValueError("candidate_child_input_too_large")
        return value

    def _transform_plan(self, plan: Mapping[str, Any]) -> Mapping[str, Any]:
        project_id = plan.get("project_id")
        revision = plan.get("draft_revision")
        if not isinstance(project_id, str) or not isinstance(revision, int) or isinstance(revision, bool):
            return plan
        context = self.store.continuation_context(project_id, revision)
        if context is None:
            context = self.store.active_continuation(project_id)
        if context is None:
            return plan
        application_id = context.get("application_id")
        if not isinstance(application_id, str):
            raise ValueError("candidate_continuation_context_invalid")
        application = self.application(application_id)
        application_ref = self._application_ref(application)
        if (
            application.get("state") != "applied"
            or context.get("application") != application_ref
            or context.get("parent_candidate") != application.get("candidate")
        ):
            raise ValueError("candidate_continuation_context_invalid")
        transformed = dict(plan)
        transformed["contract"] = "torq-candidate-plan-v2"
        transformed["lineage"] = {
            "mode": "continuation", "parent_candidate": application["candidate"],
            "application": application_ref,
        }
        plan_hash = digest_json(transformed)
        self.store.save_child_plan(
            plan_hash,
            {
                "plan_hash": plan_hash, "mode": "continuation",
                "parent_candidate": application["candidate"],
                "application": application_ref, "application_id": application_id,
            },
        )
        return transformed

    def _draft_saved(self, project_id: str, draft: Mapping[str, Any]) -> None:
        context = self.store.active_continuation(project_id)
        revision = draft.get("revision")
        if context is not None and isinstance(revision, int) and not isinstance(revision, bool):
            self.store.save_continuation_context(project_id, revision, context)

    def _draft_deleted(self, project_id: str) -> None:
        self.store.set_active_continuation(project_id, None)

    def _replay_continuation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        request = record.get("request")
        if (
            record.get("kind") != "continuation"
            or not isinstance(request, Mapping)
            or set(request) != {"application_id"}
            or digest_json(request) != record.get("request_digest")
        ):
            raise ValueError("candidate_review_store_invalid")
        if record.get("state") == "failed":
            finding = record.get("finding")
            raise ValueError(finding if isinstance(finding, str) else "candidate_continuation_failed")
        return self._complete_continuation(record)

    def _complete_continuation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """Finish or reconstruct one reserved continuation without trusting its cached result."""
        request = record.get("request")
        record_id = record.get("record_id")
        if (
            record.get("kind") != "continuation"
            or not isinstance(record_id, str)
            or not isinstance(request, Mapping)
            or set(request) != {"application_id"}
            or digest_json(request) != record.get("request_digest")
        ):
            raise ValueError("candidate_review_store_invalid")
        application_id = request.get("application_id")
        if not isinstance(application_id, str):
            raise ValueError("candidate_continuation_application_invalid")
        application = self.application(application_id)
        if application.get("state") != "applied" or not isinstance(application.get("candidate"), Mapping):
            raise ValueError("candidate_continuation_application_invalid")
        resolved = self._resolve_candidate_ref(application["candidate"])
        project_id = str(resolved.plan["project_id"])
        self._assert_project_clear(resolved)
        project = self.tasks.projects[project_id]
        with AnchoredPrimary(project.root) as primary:
            self._verify_primary(primary, resolved.result_files)
            self._verify_scope_absence(primary, resolved)

        application_ref = self._application_ref(application)
        context = {
            "mode": "continuation", "parent_candidate": resolved.candidate_ref,
            "application": application_ref, "application_id": application_id,
        }
        goal = f"Continue from: {str(resolved.plan['goal']).splitlines()[0][:240]}"
        input_paths = sorted(resolved.result_files, key=str.casefold)
        output_paths = [str(path) for path in resolved.plan["output_paths"]]
        expected_fields = {
            "project_id": project_id,
            "goal": goal,
            "input_paths": input_paths,
            "output_paths": output_paths,
        }
        current = self.tasks.draft(project_id)
        if current is not None and all(current.get(key) == value for key, value in expected_fields.items()):
            draft = current
        else:
            revision = 0 if current is None else int(current["revision"])
            empty = (
                current is not None
                and current.get("goal") == ""
                and current.get("input_paths") == []
                and current.get("output_paths") == []
            )
            if current is not None and not empty:
                ancestor = self._root_ancestor(resolved)
                if current != {
                    "project_id": project_id,
                    "goal": ancestor.plan["goal"],
                    "input_paths": ancestor.plan["input_paths"],
                    "output_paths": ancestor.plan["output_paths"],
                    "revision": ancestor.plan["draft_revision"],
                }:
                    raise ValueError("candidate_continuation_draft_occupied")
            draft = self.tasks.save_draft(
                project_id, goal=goal,
                input_paths=input_paths,
                output_paths=output_paths,
                expected_revision=revision,
            )
        self.store.set_active_continuation(project_id, context)
        self.store.save_continuation_context(project_id, int(draft["revision"]), context)
        result = {"schema": "torq-candidate-continuation-v1", "draft": dict(draft)}
        self.store.update(record_id, application=False, state="continuation_ready", result=result)
        return result

    @staticmethod
    def _application_ref(application: Mapping[str, Any]) -> dict[str, Any]:
        value = {
            "decision_id": application.get("application_id"),
            "decision_sequence": application.get("sequence"),
            "decision_receipt_hash": application.get("receipt_hash"),
            "terminal_manifest_hash": application.get("terminal_manifest_hash"),
        }
        if not validate_decision_ref(value):
            raise ValueError("candidate_continuation_application_invalid")
        return value

    def _root_ancestor(self, resolved: ResolvedTaskReview) -> ResolvedTaskReview:
        current = resolved
        seen: set[tuple[str, int]] = set()
        while current.plan.get("contract") == "torq-candidate-plan-v2":
            lineage = current.plan.get("lineage")
            if not isinstance(lineage, Mapping):
                raise ValueError("candidate_continuation_context_invalid")
            parent = lineage.get("parent_candidate")
            if not isinstance(parent, Mapping):
                raise ValueError("candidate_continuation_context_invalid")
            identity = (str(parent.get("task_id")), int(parent.get("ready_sequence", 0)))
            if identity in seen:
                raise ValueError("candidate_continuation_context_invalid")
            seen.add(identity)
            current = self._resolve_candidate_ref(parent)
        return current

    @staticmethod
    def _bundle(files: Mapping[str, bytes]) -> list[dict[str, Any]]:
        return [
            {
                "path": path,
                "content": content.decode("utf-8"),
                "content_bytes": len(content),
                "content_hash": digest_bytes(content),
            }
            for path, content in sorted(files.items(), key=lambda item: item[0].casefold())
        ]

    def _verify_correction_decision(
        self,
        decision: object,
        resolved: ResolvedTaskReview,
        plan_hash: str,
        correction: Mapping[str, Any],
    ) -> None:
        if not isinstance(decision, Mapping) or not validate_decision_ref(decision):
            raise ValueError("candidate_correction_decision_invalid")
        decision_id = str(decision["decision_id"])
        record = self.store.snapshot()["decisions"].get(decision_id)
        if not isinstance(record, Mapping):
            raise ValueError("candidate_review_store_invalid")
        request = record.get("request")
        expected_request = {
            "task_id": resolved.candidate_ref["task_id"],
            "ready_sequence": resolved.candidate_ref["ready_sequence"],
            "review_hash": resolved.candidate_ref["review_hash"],
            "correction_revision": correction["revision"],
            "correction_hash": correction["content_hash"],
        }
        if (
            record.get("record_id") != decision_id
            or record.get("kind") != "child"
            or request != expected_request
            or digest_json(expected_request) != record.get("request_digest")
        ):
            raise ValueError("candidate_correction_decision_mismatch")
        root = self.review_evidence_root / decision_id
        if verify_receipt_store(root).status != "verified":
            raise ValueError("candidate_correction_decision_unverified")
        rows = [
            json.loads(line)
            for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        if len(rows) != 1:
            raise ValueError("candidate_correction_decision_invalid")
        row = rows[0]
        payload = row.get("payload", {})
        expected_ref = {
            "decision_id": decision_id,
            "decision_sequence": row.get("sequence"),
            "decision_receipt_hash": row.get("receipt_hash"),
            "terminal_manifest_hash": digest_bytes((root / "terminal-manifest.json").read_bytes()),
        }
        if (
            dict(decision) != expected_ref
            or payload.get("review_contract") != REVIEW_CONTRACT
            or payload.get("event") != "correction_requested"
            or payload.get("decision_id") != decision_id
            or payload.get("request_digest") != record.get("request_digest")
            or payload.get("candidate") != resolved.candidate_ref
            or payload.get("detail")
            != {
                "correction_revision": correction["revision"],
                "correction_hash": correction["content_hash"],
                "child_plan_hash": plan_hash,
            }
        ):
            raise ValueError("candidate_correction_decision_mismatch")

    def _replay_child_plan(self, record: Mapping[str, Any]) -> dict[str, Any]:
        request = record.get("request")
        result = record.get("result")
        if (
            record.get("kind") != "child"
            or not isinstance(request, Mapping)
            or set(request)
            != {
                "task_id", "ready_sequence", "review_hash", "correction_revision",
                "correction_hash",
            }
            or digest_json(request) != record.get("request_digest")
            or not isinstance(result, Mapping)
            or set(result) != {"schema", "plan", "plan_hash", "correction_decision"}
            or result.get("schema") != "torq-candidate-child-plan-v1"
            or not isinstance(result.get("plan"), Mapping)
        ):
            raise ValueError("candidate_review_store_invalid")
        resolved = resolve_task_review(
            self.tasks.evidence_root, str(request["task_id"]), int(request["ready_sequence"])
        )
        correction = self.store.correction(
            str(request["task_id"]), int(request["ready_sequence"])
        )
        if (
            resolved.candidate_ref["review_hash"] != request.get("review_hash")
            or correction is None
            or correction.get("revision") != request.get("correction_revision")
            or correction.get("content_hash") != request.get("correction_hash")
        ):
            raise ValueError("candidate_child_context_invalid")
        plan_hash = str(result["plan_hash"])
        projected_plan = dict(result["plan"])
        if projected_plan.pop("plan_hash", None) != plan_hash or digest_json(projected_plan) != plan_hash:
            raise ValueError("candidate_review_store_invalid")
        context = self.store.child_plan(plan_hash)
        if context.get("parent_candidate") != resolved.candidate_ref:
            raise ValueError("candidate_child_context_invalid")
        self._verify_correction_decision(
            result.get("correction_decision"), resolved, plan_hash, correction
        )
        saved = self.tasks.store.snapshot()["plans"].get(plan_hash)
        if saved != projected_plan:
            raise ValueError("candidate_child_context_invalid")
        return dict(result)

    def accept(
        self,
        *,
        request_id: str,
        task_id: str,
        ready_sequence: int,
        review_hash: str,
        subject_id: str,
    ) -> dict[str, Any]:
        body = {
            "task_id": task_id, "ready_sequence": ready_sequence, "review_hash": review_hash,
        }
        record, created = self.store.reserve(
            request_kind="review", request_id=request_id, body=body, id_prefix="decision-"
        )
        if not created:
            return self._replay_accept(record)
        decision_id = str(record["record_id"])
        try:
            resolved = resolve_task_review(self.tasks.evidence_root, task_id, ready_sequence)
            if resolved.candidate_ref["review_hash"] != review_hash:
                raise ValueError("candidate_review_hash_mismatch")
            self._assert_project_clear(resolved)
            self._assert_source_base(resolved)
            chain = ReceiptChain(
                self.review_evidence_root, decision_id, FileRunKeyStore(self.review_evidence_root),
                profile_version="candidate-review-v1", policy_version="candidate-review-v1",
            )
            broker = EvidenceBroker(chain)
            core = canonical_json(resolved.envelope["review"])
            review_artifact = _artifact(broker, "verified-review", core)
            receipt = _append(
                broker,
                {
                    "review_contract": REVIEW_CONTRACT,
                    "event": "candidate_accepted",
                    "decision_id": decision_id,
                    "request_digest": record["request_digest"],
                    "candidate": resolved.candidate_ref,
                    "actor": {"subject_id": subject_id, "assurance": "local_operator_session"},
                    "detail": {
                        "review_artifact": review_artifact,
                        "source_scope_hash": resolved.candidate_ref["base_scope_manifest_hash"],
                    },
                },
            )
            manifest_path = broker.seal()
            if verify_receipt_store(chain.root).status != "verified":
                raise ValueError("candidate_accept_evidence_unverified")
            decision_ref = {
                "decision_id": decision_id,
                "decision_sequence": receipt["sequence"],
                "decision_receipt_hash": receipt["receipt_hash"],
                "terminal_manifest_hash": digest_bytes(manifest_path.read_bytes()),
            }
            result = {
                "schema": "torq-candidate-acceptance-v1", "state": "accepted",
                "candidate": resolved.candidate_ref, "acceptance": decision_ref,
                "source_changed": False,
            }
            self.store.update(decision_id, application=False, state="accepted", result=result)
            return result
        except BaseException as exc:
            finding = self._reason(exc, "candidate_accept_failed")
            self.store.update(decision_id, application=False, state="failed", finding=finding)
            raise ValueError(finding) from exc

    def apply(
        self,
        *,
        request_id: str,
        candidate: Mapping[str, Any],
        acceptance: Mapping[str, Any],
        subject_id: str,
    ) -> dict[str, Any]:
        body = {"candidate": dict(candidate), "acceptance": dict(acceptance)}
        record, created = self.store.reserve(
            request_kind="apply", request_id=request_id, body=body, id_prefix="application-"
        )
        if not created:
            return self._replay_application(record)
        application_id = str(record["record_id"])
        broker: EvidenceBroker | None = None
        primary: AnchoredPrimary | None = None
        marker_written = False
        started: dict[str, Any] | None = None
        journal_hash: str | None = None
        written: list[tuple[dict[str, Any], AnchoredFile, Mapping[str, Any]]] = []
        operations: list[dict[str, Any]] = []
        try:
            resolved = self._resolve_candidate_ref(candidate)
            accepted = self._resolve_acceptance(acceptance, resolved)
            project = self.tasks.projects[str(resolved.plan["project_id"])]
            chain = ReceiptChain(
                self.apply_evidence_root, application_id, FileRunKeyStore(self.apply_evidence_root),
                profile_version="candidate-apply-v1", policy_version="candidate-apply-v1",
            )
            broker = EvidenceBroker(chain)
            common = {
                "apply_contract": APPLY_CONTRACT, "application_id": application_id,
                "request_digest": record["request_digest"], "candidate": resolved.candidate_ref,
                "acceptance": accepted, "actor": {"subject_id": subject_id, "assurance": "local_operator_session"},
            }
            _append(broker, {**common, "event": "apply_authority_created", "detail": {"installation_id": self.store.installation_id}})
            if verify_receipt_store(chain.root).status != "verified":
                raise ValueError("task_apply_authority_unverified")
            primary = AnchoredPrimary(project.root)
            if self.markers.read(primary) is not None:
                raise ValueError("task_apply_recovery_pending")
            operations, current = self._prepare_operations(primary, resolved)
            marker = {
                "schema": "torq-primary-pending-v1", "primary_identity_hash": primary.identity.digest(),
                "installation_id": self.store.installation_id, "application_id": application_id,
                "authority_locator": self.store.installation_id, "journal_hash": None, "phase": "reserved",
            }
            self.markers.write(primary, marker)
            marker_written = True
            journal = {
                "schema": "torq-primary-journal-v1", "application_id": application_id,
                "request_digest": record["request_digest"], "candidate": resolved.candidate_ref,
                "acceptance": accepted, "primary_identity": primary.identity.value(),
                "base_scope_manifest_hash": resolved.candidate_ref["base_scope_manifest_hash"],
                "result_scope_manifest_hash": resolved.candidate_ref["candidate_scope_manifest_hash"],
                "operations": operations, "created_directories": [],
            }
            journal_text = canonical_json(journal)
            journal_hash = digest_bytes(journal_text.encode("utf-8"))
            journal_artifact = _artifact(broker, "primary-journal", journal_text)
            prepared = _append(
                broker,
                {**common, "event": "apply_prepared", "detail": {
                    "journal_artifact": journal_artifact, "journal_hash": journal_hash,
                    "primary_identity_hash": primary.identity.digest(), "operation_count": len(operations),
                }},
            )
            self.markers.write(primary, {**marker, "journal_hash": journal_hash, "phase": "prepared"})
            started = _append(
                broker,
                {**common, "event": "apply_started", "detail": {
                    "prepared_sequence": prepared["sequence"], "prepared_receipt_hash": prepared["receipt_hash"],
                }},
            )
            self.markers.write(primary, {**marker, "journal_hash": journal_hash, "phase": "started"})
            for operation, before in zip(operations, current, strict=True):
                staged_receipt: dict[str, Any] | None = None

                def record_staged(identity: dict[str, int]) -> None:
                    nonlocal staged_receipt
                    staged_receipt = _append(
                        broker,
                        {**common, "event": "apply_file_staged", "detail": {
                            "started_sequence": started["sequence"],
                            "operation_index": operation["index"], "path": operation["path"],
                            "result_hash": operation["after_hash"], "staged_identity": identity,
                        }},
                    )
                    if verify_receipt_store(chain.root).status != "verified":
                        raise ValueError("task_apply_stage_unverified")

                applied_file = primary.replace(
                    str(operation["path"]), expected_before_hash=operation["before_hash"],
                    expected_parent_identity=before.parent_identity,
                    expected_target_identity=before.target_identity,
                    content=str(operation["after_content"]).encode("utf-8"),
                    permission_policy=dict(operation["permission_policy"]),
                    application_id=application_id, operation_index=int(operation["index"]),
                    staged_callback=record_staged,
                )
                if staged_receipt is None:
                    raise ValueError("task_apply_stage_missing")
                written.append((operation, applied_file, staged_receipt))
                _append(
                    broker,
                    {**common, "event": "apply_file_written", "detail": {
                        "started_sequence": started["sequence"], "operation_index": operation["index"],
                        "staged_sequence": staged_receipt["sequence"],
                        "staged_receipt_hash": staged_receipt["receipt_hash"],
                        "path": operation["path"], "result_hash": operation["after_hash"],
                    }},
                )
            self._verify_primary(primary, resolved.result_files)
            terminal = _append(
                broker,
                {**common, "event": "candidate_applied", "detail": {
                    "started_sequence": started["sequence"], "journal_hash": journal_hash,
                    "result_scope_manifest_hash": resolved.candidate_ref["candidate_scope_manifest_hash"],
                    "operation_count": len(operations),
                }},
            )
            manifest_path = broker.seal()
            self.markers.write(primary, {**marker, "journal_hash": journal_hash, "phase": "terminal"})
            self.markers.clear(primary, installation_id=self.store.installation_id, application_id=application_id)
            marker_written = False
            result = {
                "schema": "torq-candidate-application-v1", "state": "applied",
                "application_id": application_id, "sequence": terminal["sequence"],
                "receipt_hash": terminal["receipt_hash"], "terminal_manifest_hash": digest_bytes(manifest_path.read_bytes()),
                "candidate": resolved.candidate_ref, "acceptance": accepted,
            }
            self.store.update(application_id, application=True, state="applied", result=result)
            return result
        except BaseException as exc:
            finding = self._reason(exc, "task_apply_failed")
            result = self._rollback_or_reject(
                broker, primary, marker_written, started, written, journal_hash, record, application_id,
                candidate, acceptance, subject_id, finding, operations,
            )
            self.store.update(application_id, application=True, state=result["state"], finding=finding, result=result)
            if result["state"] == "recovery_required":
                return result
            raise ValueError(finding) from exc
        finally:
            if primary is not None:
                primary.close()

    def history(self, *, query: str = "", limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        if len(query) > 256 or not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 100:
            raise ValueError("candidate_history_query_invalid")
        needle = query.casefold()
        task_data = self.tasks.store.snapshot()
        rows: list[dict[str, Any]] = []
        for task_id, task in task_data["tasks"].items():
            if not isinstance(task, Mapping) or re.fullmatch(r"run-task-[a-f0-9]{24}", task_id) is None:
                continue
            plan = task_data["plans"].get(task.get("plan_hash"))
            goal = str(plan.get("goal", "")) if isinstance(plan, Mapping) else ""
            project_id = str(plan.get("project_id", "")) if isinstance(plan, Mapping) else ""
            label = self.tasks.projects.get(project_id)
            root = self.tasks.evidence_root / task_id
            ready_sequence: int | None = None
            created_at: str | None = None
            if verify_receipt_store(root).status == "verified":
                try:
                    receipts = [json.loads(line) for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
                    created_at = str(receipts[0].get("observed_at")) if receipts else None
                    ready_sequence = next(
                        int(row["sequence"])
                        for row in receipts
                        if row.get("payload", {}).get("event") == "candidate_ready"
                    )
                except (OSError, ValueError, StopIteration, json.JSONDecodeError):
                    ready_sequence = None
            row = {
                "task_id": task_id, "ready_sequence": ready_sequence,
                "title": goal.splitlines()[0][:120], "goal": goal,
                "project_id": project_id, "project_label": label.label if label else "Configured project",
                "state": "candidate_ready" if ready_sequence is not None else str(task.get("state", "unverified")),
                "created_at": created_at, "legacy_order": created_at is None,
                "history_ordinal": task.get("history_ordinal"),
            }
            if not needle or needle in " ".join(
                str(row.get(key, "")) for key in ("task_id", "title", "goal", "project_id", "project_label")
            ).casefold():
                rows.append(row)
        def order(item: dict[str, Any]) -> tuple[int, int, str, str]:
            ordinal = item.get("history_ordinal")
            if isinstance(ordinal, int) and not isinstance(ordinal, bool) and ordinal > 0:
                return (2, ordinal, "", str(item["task_id"]))
            # Existing P2 entries use signed creation time when available.
            # Undated entries have a labelled deterministic fallback, not a fake date.
            return (1 if item.get("created_at") else 0, 0,
                    str(item.get("created_at") or ""), str(item["task_id"]))

        rows.sort(key=order, reverse=True)
        if cursor is not None:
            positions = [index for index, item in enumerate(rows) if item["task_id"] == cursor]
            if not positions:
                raise ValueError("candidate_history_cursor_invalid")
            rows = rows[positions[0] + 1:]
        filtered = rows[:limit]
        return {
            "schema": "torq-candidate-history-v1", "items": filtered,
            "next_cursor": filtered[-1]["task_id"] if len(rows) > limit else None,
        }

    def application(self, application_id: str) -> dict[str, Any]:
        value = self.store.snapshot()["applications"].get(application_id)
        if not isinstance(value, dict):
            raise ValueError("task_application_unknown")
        return self._replay_application(value)

    def recovery_status(self, project_id: str) -> dict[str, Any]:
        project = self.tasks.projects.get(project_id)
        if project is None:
            raise ValueError("task_project_unknown")
        try:
            with AnchoredPrimary(project.root) as primary:
                marker = self.markers.read(primary)
        except RuntimeError:
            return {"schema": "torq-candidate-recovery-v1", "state": "busy", "finding": "task_apply_primary_busy"}
        if marker is None:
            return {"schema": "torq-candidate-recovery-v1", "state": "idle", "finding": None}
        return {
            "schema": "torq-candidate-recovery-v1", "state": "recovery_required",
            "application_id": marker["application_id"],
            "owned": marker["installation_id"] == self.store.installation_id,
            "finding": "task_apply_recovery_pending",
        }

    def recover(self, project_id: str) -> dict[str, Any]:
        project = self.tasks.projects.get(project_id)
        if project is None:
            raise ValueError("task_project_unknown")
        with AnchoredPrimary(project.root) as primary:
            marker = self.markers.read(primary)
            if marker is None:
                return {"schema": "torq-candidate-recovery-v1", "state": "idle", "finding": None}
            if marker.get("installation_id") != self.store.installation_id:
                raise ValueError("task_apply_marker_owned_elsewhere")
            application_id = str(marker["application_id"])
            record = self.store.snapshot()["applications"].get(application_id)
            if not isinstance(record, Mapping):
                raise ValueError("candidate_review_store_invalid")
            request = record.get("request")
            if (
                record.get("kind") != "apply"
                or record.get("record_id") != application_id
                or not isinstance(request, Mapping)
                or set(request) != {"candidate", "acceptance"}
                or digest_json(request) != record.get("request_digest")
            ):
                raise ValueError("candidate_review_store_invalid")
            candidate = request.get("candidate")
            acceptance = request.get("acceptance")
            if not isinstance(candidate, Mapping) or not isinstance(acceptance, Mapping):
                raise ValueError("candidate_review_store_invalid")
            resolved = self._resolve_candidate_ref(candidate)
            accepted = self._resolve_acceptance(acceptance, resolved)
            chain_root = self.apply_evidence_root / application_id
            if verify_receipt_store(chain_root).status != "verified":
                raise ValueError("task_apply_recovery_evidence_unverified")
            rows = [json.loads(line) for line in (chain_root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
            if not rows:
                raise ValueError("task_apply_recovery_state_invalid")
            first = rows[0]["payload"]
            common = {
                "apply_contract": APPLY_CONTRACT, "application_id": application_id,
                "request_digest": record["request_digest"], "candidate": resolved.candidate_ref,
                "acceptance": accepted, "actor": first["actor"],
            }
            if any(first.get(key) != value for key, value in common.items()):
                raise ValueError("task_apply_recovery_identity_mismatch")
            terminal_events = {"candidate_applied", "apply_rolled_back", "apply_rejected"}
            last_event = rows[-1].get("payload", {}).get("event")
            manifest_path = chain_root / "terminal-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if last_event in terminal_events:
                for index, path in enumerate(resolved.changed_paths):
                    if primary.inspect_staged(
                        path, application_id=application_id, operation_index=index
                    ).content is not None:
                        raise ValueError("task_apply_recovery_conflict")
                if last_event == "candidate_applied":
                    self._verify_primary(primary, resolved.result_files)
                    self._verify_scope_absence(primary, resolved)
                else:
                    self._verify_base_scope(primary, resolved)
                if manifest.get("sealed") is not True:
                    chain = ReceiptChain.open_existing(
                        self.apply_evidence_root, application_id,
                        FileRunKeyStore(self.apply_evidence_root),
                        profile_version="candidate-apply-v1", policy_version="candidate-apply-v1",
                    )
                    EvidenceBroker(chain).seal()
                replayed = self._replay_application(record)
                self.store.update(
                    application_id, application=True, state=replayed["state"], result=replayed
                )
                self.markers.clear(
                    primary, installation_id=self.store.installation_id,
                    application_id=application_id,
                )
                return replayed
            prepared_row = next((row for row in rows if row.get("payload", {}).get("event") == "apply_prepared"), None)
            started_row = next((row for row in rows if row.get("payload", {}).get("event") == "apply_started"), None)
            if prepared_row is None:
                if len(rows) != 1:
                    raise ValueError("task_apply_recovery_state_invalid")
                self._verify_base_scope(primary, resolved)
                for index, path in enumerate(resolved.changed_paths):
                    if primary.inspect_staged(
                        path, application_id=application_id, operation_index=index
                    ).content is not None:
                        raise ValueError("task_apply_recovery_conflict")
                chain = ReceiptChain.open_existing(
                    self.apply_evidence_root, application_id,
                    FileRunKeyStore(self.apply_evidence_root),
                    profile_version="candidate-apply-v1", policy_version="candidate-apply-v1",
                )
                broker = EvidenceBroker(chain)
                terminal = _append(
                    broker,
                    {**common, "event": "apply_rejected", "detail": {
                        "reason_code": "task_apply_recovery_aborted"
                    }},
                )
                sealed_path = broker.seal()
                self.markers.clear(
                    primary, installation_id=self.store.installation_id,
                    application_id=application_id,
                )
                result = {
                    "schema": "torq-candidate-application-v1", "state": "rejected",
                    "application_id": application_id, "sequence": terminal["sequence"],
                    "receipt_hash": terminal["receipt_hash"],
                    "terminal_manifest_hash": digest_bytes(sealed_path.read_bytes()),
                    "candidate": resolved.candidate_ref, "acceptance": accepted,
                    "finding": "task_apply_recovery_aborted",
                }
                self.store.update(
                    application_id, application=True, state="rejected", result=result
                )
                return result
            prepared = prepared_row["payload"]
            artifact = prepared["detail"]["journal_artifact"]
            journal_raw = read_verified_prefix_artifact(
                self.apply_evidence_root, application_id, str(artifact["artifact"])
            )
            if (
                ReceiptChain.hash_file(chain_root / str(artifact["artifact"])) != artifact["artifact_hash"]
                or digest_bytes(journal_raw) != artifact["content_hash"]
                or digest_bytes(journal_raw) != prepared["detail"]["journal_hash"]
                or marker.get("journal_hash") not in {None, digest_bytes(journal_raw)}
                or marker.get("journal_hash") is None and marker.get("phase") != "reserved"
            ):
                raise ValueError("task_apply_journal_hash_mismatch")
            journal = json.loads(journal_raw)
            journal_keys = {
                "schema", "application_id", "request_digest", "candidate", "acceptance",
                "primary_identity", "base_scope_manifest_hash", "result_scope_manifest_hash",
                "operations", "created_directories",
            }
            if (
                not isinstance(journal, Mapping) or set(journal) != journal_keys
                or journal.get("schema") != "torq-primary-journal-v1"
                or journal.get("application_id") != application_id
                or journal.get("request_digest") != record["request_digest"]
                or journal.get("candidate") != resolved.candidate_ref
                or journal.get("acceptance") != accepted
                or journal.get("primary_identity") != primary.identity.value()
                or journal.get("base_scope_manifest_hash") != resolved.candidate_ref["base_scope_manifest_hash"]
                or journal.get("result_scope_manifest_hash") != resolved.candidate_ref["candidate_scope_manifest_hash"]
                or journal.get("created_directories") != []
                or not isinstance(journal.get("operations"), list)
            ):
                raise ValueError("task_apply_journal_invalid")
            operations = self._validate_journal_operations(
                primary, resolved, journal["operations"]
            )
            if len(operations) != prepared["detail"]["operation_count"]:
                raise ValueError("task_apply_journal_invalid")
            staged_rows = {
                row["payload"]["detail"]["operation_index"]: row
                for row in rows if row.get("payload", {}).get("event") == "apply_file_staged"
            }
            written_rows = {
                row["payload"]["detail"]["operation_index"]: row
                for row in rows if row.get("payload", {}).get("event") == "apply_file_written"
            }
            rollback_started = any(
                row.get("payload", {}).get("event") == "apply_rollback_started"
                for row in rows
            )
            rollback_started_row = next(
                (
                    row for row in rows
                    if row.get("payload", {}).get("event") == "apply_rollback_started"
                ),
                None,
            )
            rollback_staged_rows = {
                row["payload"]["detail"]["operation_index"]: row
                for row in rows
                if row.get("payload", {}).get("event") == "apply_rollback_file_staged"
            }
            rollback_restored_rows = {
                row["payload"]["detail"]["operation_index"]: row
                for row in rows
                if row.get("payload", {}).get("event") == "apply_rollback_file_restored"
            }
            states: list[str] = []
            for index, operation in enumerate(operations):
                current = primary.inspect(str(operation["path"]))
                staged = staged_rows.get(index)
                written_row = written_rows.get(index)
                for progress in (staged, written_row):
                    if progress is not None and (
                        progress["payload"]["detail"].get("path") != operation["path"]
                        or progress["payload"]["detail"].get("result_hash")
                        != operation["after_hash"]
                    ):
                        raise ValueError("task_apply_journal_invalid")
                if current.parent_identity != operation.get("parent_identity"):
                    raise ValueError("task_apply_recovery_conflict")
                rollback_stage = rollback_staged_rows.get(index)
                rollback_restored = rollback_restored_rows.get(index)
                rollback_temp = primary.inspect_rollback_staged(
                    str(operation["path"]), application_id=application_id,
                    operation_index=index,
                )
                if rollback_temp.content is not None:
                    if rollback_stage is None or rollback_temp.target_identity is None:
                        raise ValueError("task_apply_recovery_conflict")
                    rollback_stable = {
                        "volume": rollback_temp.target_identity["volume"],
                        "file_id": rollback_temp.target_identity["file_id"],
                    }
                    if (
                        rollback_temp.content_hash != operation["before_hash"]
                        or rollback_temp.parent_identity != operation["parent_identity"]
                        or rollback_stable
                        != rollback_stage["payload"]["detail"].get("staged_identity")
                    ):
                        raise ValueError("task_apply_recovery_conflict")
                for progress in (rollback_stage, rollback_restored):
                    if progress is not None and (
                        progress["payload"]["detail"].get("path") != operation["path"]
                        or progress["payload"]["detail"].get("base_hash")
                        != operation["before_hash"]
                    ):
                        raise ValueError("task_apply_journal_invalid")
                before = None if operation.get("before_content") is None else str(operation["before_content"]).encode("utf-8")
                after = str(operation.get("after_content", "")).encode("utf-8")
                staged_file = primary.inspect_staged(
                    str(operation["path"]), application_id=application_id,
                    operation_index=index,
                )
                if staged_file.content is not None:
                    if staged is None or staged_file.target_identity is None:
                        raise ValueError("task_apply_recovery_conflict")
                    staged_stable = {
                        "volume": staged_file.target_identity["volume"],
                        "file_id": staged_file.target_identity["file_id"],
                    }
                    if (
                        staged_file.content_hash != operation["after_hash"]
                        or staged_file.parent_identity != operation["parent_identity"]
                        or staged_stable != staged["payload"]["detail"].get("staged_identity")
                    ):
                        raise ValueError("task_apply_recovery_conflict")
                if current.content == before:
                    if before is not None:
                        stable = None if current.target_identity is None else {"volume": current.target_identity["volume"], "file_id": current.target_identity["file_id"]}
                        rollback_identity = (
                            None if rollback_stage is None
                            else rollback_stage["payload"]["detail"].get("staged_identity")
                        )
                        if (
                            stable != operation.get("before_identity")
                            and stable != rollback_identity
                        ):
                            raise ValueError("task_apply_recovery_conflict")
                        if stable == rollback_identity and rollback_temp.content is not None:
                            raise ValueError("task_apply_recovery_conflict")
                        if stable == operation.get("before_identity"):
                            if rollback_stage is not None and rollback_restored is None:
                                raise ValueError("task_apply_recovery_conflict")
                            state = "before_original"
                        else:
                            state = "before_rollback"
                    elif rollback_stage is not None and rollback_restored is None:
                        # Absence after a signed delete intent but before its completion
                        # receipt is inherently ambiguous across a process death.
                        raise ValueError("task_apply_recovery_conflict")
                    else:
                        state = "before_rollback" if rollback_stage is not None else "before_original"
                    if staged is not None:
                        if staged_file.content is None:
                            if not rollback_started:
                                raise ValueError("task_apply_recovery_conflict")
                        elif staged_file.target_identity is None:
                            raise ValueError("task_apply_recovery_conflict")
                        else:
                            primary.remove_staged(
                                str(operation["path"]), application_id=application_id,
                                operation_index=index, expected_hash=str(operation["after_hash"]),
                                expected_parent_identity=dict(operation["parent_identity"]),
                                expected_target_identity=staged_file.target_identity,
                            )
                    states.append(state)
                elif current.content == after:
                    if staged_file.content is not None:
                        raise ValueError("task_apply_recovery_conflict")
                    stable = None if current.target_identity is None else {"volume": current.target_identity["volume"], "file_id": current.target_identity["file_id"]}
                    if staged is None or stable != staged["payload"]["detail"].get("staged_identity"):
                        raise ValueError("task_apply_recovery_conflict")
                    states.append("after")
                else:
                    raise ValueError("task_apply_recovery_conflict")
            chain = ReceiptChain.open_existing(
                self.apply_evidence_root, application_id, FileRunKeyStore(self.apply_evidence_root),
                profile_version="candidate-apply-v1", policy_version="candidate-apply-v1",
            )
            broker = EvidenceBroker(chain)
            if started_row is None:
                if staged_rows or written_rows or any(not state.startswith("before_") for state in states):
                    raise ValueError("task_apply_recovery_conflict")
                terminal = _append(
                    broker,
                    {**common, "event": "apply_rejected", "detail": {
                        "reason_code": "task_apply_recovery_aborted"
                    }},
                )
                sealed_path = broker.seal()
                self.markers.clear(
                    primary, installation_id=self.store.installation_id,
                    application_id=application_id,
                )
                result = {
                    "schema": "torq-candidate-application-v1", "state": "rejected",
                    "application_id": application_id, "sequence": terminal["sequence"],
                    "receipt_hash": terminal["receipt_hash"],
                    "terminal_manifest_hash": digest_bytes(sealed_path.read_bytes()),
                    "candidate": resolved.candidate_ref, "acceptance": accepted,
                    "finding": "task_apply_recovery_aborted",
                }
                self.store.update(
                    application_id, application=True, state="rejected", result=result
                )
                return result
            self.markers.write(primary, {**marker, "phase": "recovering"})
            if all(state == "after" for state in states):
                for index, operation in enumerate(operations):
                    if index in written_rows:
                        continue
                    staged = staged_rows[index]
                    _append(broker, {**common, "event": "apply_file_written", "detail": {
                        "started_sequence": started_row["sequence"], "operation_index": index,
                        "staged_sequence": staged["sequence"], "staged_receipt_hash": staged["receipt_hash"],
                        "path": operation["path"], "result_hash": operation["after_hash"],
                    }})
                self._verify_primary(primary, resolved.result_files)
                self._verify_scope_absence(primary, resolved)
                terminal = _append(broker, {**common, "event": "candidate_applied", "detail": {
                    "started_sequence": started_row["sequence"], "journal_hash": marker["journal_hash"],
                    "result_scope_manifest_hash": resolved.candidate_ref["candidate_scope_manifest_hash"],
                    "operation_count": len(operations),
                }})
                manifest_path = broker.seal()
                self.markers.clear(primary, installation_id=self.store.installation_id, application_id=application_id)
                result = {
                    "schema": "torq-candidate-application-v1", "state": "applied", "application_id": application_id,
                    "sequence": terminal["sequence"], "receipt_hash": terminal["receipt_hash"],
                    "terminal_manifest_hash": digest_bytes(manifest_path.read_bytes()),
                    "candidate": resolved.candidate_ref, "acceptance": accepted,
                }
            else:
                if not rollback_started:
                    rollback_started_row = _append(broker, {**common, "event": "apply_rollback_started", "detail": {
                        "caused_by_sequence": broker.sequence, "reason_code": "task_apply_recovery_rollback",
                    }})
                if rollback_started_row is None:
                    raise ValueError("task_apply_recovery_state_invalid")
                rollback_sequence = rollback_started_row["sequence"]
                for index in reversed(range(len(operations))):
                    operation = operations[index]
                    existing_stage = rollback_staged_rows.get(index)
                    existing_restored = rollback_restored_rows.get(index)
                    if states[index] != "after":
                        if states[index] == "before_rollback" and existing_stage is not None and existing_restored is None:
                            if operation["before_content"] is None:
                                raise ValueError("task_apply_recovery_conflict")
                            _append(broker, {**common, "event": "apply_rollback_file_restored", "detail": {
                                "rollback_started_sequence": rollback_sequence,
                                "operation_index": index, "path": operation["path"],
                                "base_hash": operation["before_hash"],
                                "staged_sequence": existing_stage["sequence"],
                                "staged_receipt_hash": existing_stage["receipt_hash"],
                            }})
                        continue
                    current = primary.inspect(str(operation["path"]))
                    staged_identity = staged_rows[index]["payload"]["detail"]["staged_identity"]
                    stable = None if current.target_identity is None else {"volume": current.target_identity["volume"], "file_id": current.target_identity["file_id"]}
                    if stable != staged_identity or current.target_identity is None:
                        raise ValueError("task_apply_recovery_conflict")
                    if operation["before_content"] is None:
                        rollback_stage = existing_stage
                        if rollback_stage is None:
                            rollback_stage = _append(broker, {**common, "event": "apply_rollback_file_staged", "detail": {
                                "rollback_started_sequence": rollback_sequence,
                                "operation_index": index, "path": operation["path"],
                                "base_hash": None, "staged_identity": stable,
                            }})
                        elif rollback_stage["payload"]["detail"].get("staged_identity") != stable:
                            raise ValueError("task_apply_recovery_conflict")
                        primary.remove(
                            str(operation["path"]), expected_hash=str(operation["after_hash"]),
                            expected_parent_identity=current.parent_identity,
                            expected_target_identity=current.target_identity,
                        )
                    else:
                        rollback_stage = existing_stage
                        if rollback_stage is None:
                            captured: dict[str, Any] | None = None

                            def record_rollback_staged(identity: dict[str, int]) -> None:
                                nonlocal captured
                                captured = _append(broker, {**common, "event": "apply_rollback_file_staged", "detail": {
                                    "rollback_started_sequence": rollback_sequence,
                                    "operation_index": index, "path": operation["path"],
                                    "base_hash": operation["before_hash"],
                                    "staged_identity": identity,
                                }})

                            primary.replace(
                                str(operation["path"]), expected_before_hash=str(operation["after_hash"]),
                                expected_parent_identity=current.parent_identity,
                                expected_target_identity=current.target_identity,
                                content=str(operation["before_content"]).encode("utf-8"),
                                permission_policy=dict(operation["permission_policy"]),
                                application_id=application_id, operation_index=index,
                                staged_callback=record_rollback_staged, rollback=True,
                            )
                            if captured is None:
                                raise ValueError("task_apply_rollback_stage_missing")
                            rollback_stage = captured
                        else:
                            rollback_temp = primary.inspect_rollback_staged(
                                str(operation["path"]), application_id=application_id,
                                operation_index=index,
                            )
                            if rollback_temp.target_identity is None:
                                raise ValueError("task_apply_recovery_conflict")
                            primary.commit_rollback_staged(
                                str(operation["path"]), application_id=application_id,
                                operation_index=index,
                                expected_before_hash=str(operation["after_hash"]),
                                expected_before_identity=current.target_identity,
                                expected_parent_identity=current.parent_identity,
                                staged_hash=str(operation["before_hash"]),
                                staged_identity=dict(rollback_stage["payload"]["detail"]["staged_identity"]),
                                permission_policy=dict(operation["permission_policy"]),
                            )
                    _append(broker, {**common, "event": "apply_rollback_file_restored", "detail": {
                        "rollback_started_sequence": rollback_sequence,
                        "operation_index": index, "path": operation["path"],
                        "base_hash": operation["before_hash"],
                        "staged_sequence": rollback_stage["sequence"],
                        "staged_receipt_hash": rollback_stage["receipt_hash"],
                    }})
                self._verify_primary(primary, resolved.base_files)
                for path in resolved.changed_paths:
                    if path not in resolved.base_files and primary.inspect(path).content is not None:
                        raise ValueError("task_apply_recovery_conflict")
                terminal = _append(broker, {**common, "event": "apply_rolled_back", "detail": {
                    "prepared_sequence": prepared_row["sequence"], "journal_hash": marker["journal_hash"],
                    "restored_scope_manifest_hash": resolved.candidate_ref["base_scope_manifest_hash"],
                }})
                manifest_path = broker.seal()
                self.markers.clear(primary, installation_id=self.store.installation_id, application_id=application_id)
                result = {
                    "schema": "torq-candidate-application-v1", "state": "rolled_back", "application_id": application_id,
                    "sequence": terminal["sequence"], "receipt_hash": terminal["receipt_hash"],
                    "terminal_manifest_hash": digest_bytes(manifest_path.read_bytes()),
                    "candidate": resolved.candidate_ref, "acceptance": accepted,
                }
            self.store.update(application_id, application=True, state=result["state"], result=result)
            return result

    def replay_accept(
        self, *, request_id: str, task_id: str, ready_sequence: int, review_hash: str
    ) -> dict[str, Any]:
        body = {"task_id": task_id, "ready_sequence": ready_sequence, "review_hash": review_hash}
        return self._replay_accept(
            self.store.replay(request_kind="review", request_id=request_id, body=body)
        )

    def replay_apply(
        self, *, request_id: str, candidate: Mapping[str, Any], acceptance: Mapping[str, Any]
    ) -> dict[str, Any]:
        body = {"candidate": dict(candidate), "acceptance": dict(acceptance)}
        return self._replay_application(
            self.store.replay(request_kind="apply", request_id=request_id, body=body)
        )

    def _resolve_candidate_ref(self, candidate: Mapping[str, Any]) -> ResolvedTaskReview:
        task_id = candidate.get("task_id")
        sequence = candidate.get("ready_sequence")
        if not isinstance(task_id, str) or not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("candidate_reference_invalid")
        resolved = resolve_task_review(self.tasks.evidence_root, task_id, sequence)
        if resolved.candidate_ref != dict(candidate):
            raise ValueError("candidate_reference_mismatch")
        return resolved

    def _resolve_acceptance(self, acceptance: Mapping[str, Any], resolved: ResolvedTaskReview) -> dict[str, Any]:
        decision_id = acceptance.get("decision_id")
        if not isinstance(decision_id, str):
            raise ValueError("candidate_acceptance_invalid")
        root = self.review_evidence_root / decision_id
        if verify_receipt_store(root).status != "verified":
            raise ValueError("candidate_acceptance_unverified")
        manifest_raw = (root / "terminal-manifest.json").read_bytes()
        rows = [json.loads(line) for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
        if len(rows) != 1:
            raise ValueError("candidate_acceptance_invalid")
        payload = rows[0].get("payload", {})
        expected = {
            "decision_id": decision_id, "decision_sequence": rows[0].get("sequence"),
            "decision_receipt_hash": rows[0].get("receipt_hash"),
            "terminal_manifest_hash": digest_bytes(manifest_raw),
        }
        if (
            dict(acceptance) != expected
            or payload.get("review_contract") != REVIEW_CONTRACT
            or payload.get("event") != "candidate_accepted"
            or payload.get("decision_id") != decision_id
            or payload.get("candidate") != resolved.candidate_ref
            or payload.get("detail", {}).get("source_scope_hash")
            != resolved.candidate_ref["base_scope_manifest_hash"]
        ):
            raise ValueError("candidate_acceptance_mismatch")
        artifact = payload.get("detail", {}).get("review_artifact", {})
        plain = read_verified_artifact(self.review_evidence_root, decision_id, str(artifact.get("artifact")))
        artifact_path = root / str(artifact.get("artifact"))
        if (
            ReceiptChain.hash_file(artifact_path) != artifact.get("artifact_hash")
            or digest_bytes(plain) != artifact.get("content_hash")
            or digest_json(json.loads(plain)) != resolved.candidate_ref["review_hash"]
        ):
            raise ValueError("candidate_acceptance_artifact_mismatch")
        return expected

    def _replay_accept(self, record: Mapping[str, Any]) -> dict[str, Any]:
        request = record.get("request")
        if (
            record.get("kind") != "review"
            or not isinstance(record.get("record_id"), str)
            or not isinstance(request, Mapping)
            or set(request) != {"task_id", "ready_sequence", "review_hash"}
            or digest_json(request) != record.get("request_digest")
        ):
            raise ValueError("candidate_review_store_invalid")
        task_id = request.get("task_id")
        sequence = request.get("ready_sequence")
        if not isinstance(task_id, str) or not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ValueError("candidate_review_store_invalid")
        resolved = resolve_task_review(self.tasks.evidence_root, task_id, sequence)
        if resolved.candidate_ref["review_hash"] != request.get("review_hash"):
            raise ValueError("candidate_review_store_invalid")
        root = self.review_evidence_root / str(record["record_id"])
        if verify_receipt_store(root).status != "verified":
            raise ValueError("candidate_acceptance_unverified")
        manifest_raw = (root / "terminal-manifest.json").read_bytes()
        rows = [json.loads(line) for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
        if len(rows) != 1:
            raise ValueError("candidate_acceptance_invalid")
        payload = rows[0].get("payload", {})
        if (
            payload.get("event") != "candidate_accepted"
            or payload.get("decision_id") != record["record_id"]
            or payload.get("request_digest") != record.get("request_digest")
            or payload.get("candidate") != resolved.candidate_ref
        ):
            raise ValueError("candidate_acceptance_mismatch")
        verified_acceptance = {
            "decision_id": record["record_id"], "decision_sequence": rows[0]["sequence"],
            "decision_receipt_hash": rows[0]["receipt_hash"],
            "terminal_manifest_hash": digest_bytes(manifest_raw),
        }
        self._resolve_acceptance(verified_acceptance, resolved)
        return {
            "schema": "torq-candidate-acceptance-v1", "state": "accepted",
            "candidate": resolved.candidate_ref, "acceptance": verified_acceptance,
            "source_changed": False,
        }

    def _replay_application(self, record: Mapping[str, Any]) -> dict[str, Any]:
        request = record.get("request")
        application_id = record.get("record_id")
        if (
            record.get("kind") != "apply"
            or not isinstance(application_id, str)
            or not isinstance(request, Mapping)
            or set(request) != {"candidate", "acceptance"}
            or digest_json(request) != record.get("request_digest")
        ):
            raise ValueError("candidate_review_store_invalid")
        root = self.apply_evidence_root / application_id
        verification = verify_receipt_store(root)
        if verification.status != "verified":
            raise ValueError("candidate_application_evidence_unverified")
        candidate = request.get("candidate")
        acceptance = request.get("acceptance")
        if not isinstance(candidate, Mapping) or not isinstance(acceptance, Mapping):
            raise ValueError("candidate_review_store_invalid")
        resolved = self._resolve_candidate_ref(candidate)
        verified_acceptance = self._resolve_acceptance(acceptance, resolved)
        rows = [json.loads(line) for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
        terminal = rows[-1]
        first_payload = rows[0].get("payload", {})
        terminal_payload = terminal.get("payload", {})
        if (
            first_payload.get("apply_contract") != APPLY_CONTRACT
            or
            first_payload.get("application_id") != application_id
            or first_payload.get("request_digest") != record.get("request_digest")
            or first_payload.get("candidate") != resolved.candidate_ref
            or first_payload.get("acceptance") != verified_acceptance
        ):
            raise ValueError("candidate_application_evidence_mismatch")
        event = terminal_payload.get("event")
        state_by_event = {
            "candidate_applied": "applied", "apply_rolled_back": "rolled_back",
            "apply_recovery_required": "recovery_required", "apply_rejected": "rejected",
        }
        state = state_by_event.get(event)
        if (
            state is None
            or terminal.get("run_id") != application_id
            or terminal_payload.get("application_id") != application_id
            or terminal_payload.get("request_digest") != record.get("request_digest")
            or terminal_payload.get("candidate") != resolved.candidate_ref
            or terminal_payload.get("acceptance") != verified_acceptance
        ):
            raise ValueError("candidate_application_evidence_mismatch")
        manifest_raw = (root / "terminal-manifest.json").read_bytes()
        result = {
            "schema": "torq-candidate-application-v1", "state": state,
            "application_id": application_id, "sequence": terminal["sequence"],
            "receipt_hash": terminal["receipt_hash"], "terminal_manifest_hash": digest_bytes(manifest_raw),
            "candidate": resolved.candidate_ref, "acceptance": verified_acceptance,
        }
        if state in {"rejected", "recovery_required"}:
            result["finding"] = terminal_payload.get("detail", {}).get("reason_code")
        return result

    def _assert_project_clear(self, resolved: ResolvedTaskReview) -> None:
        self.markers.assert_clear(self.tasks.projects[str(resolved.plan["project_id"])].root)

    def _assert_source_base(self, resolved: ResolvedTaskReview) -> None:
        project = self.tasks.projects[str(resolved.plan["project_id"])]
        with AnchoredPrimary(project.root) as primary:
            try:
                self._verify_base_scope(primary, resolved)
            except ValueError as exc:
                raise ValueError("candidate_source_stale") from exc

    def _prepare_operations(
        self, primary: AnchoredPrimary, resolved: ResolvedTaskReview
    ) -> tuple[list[dict[str, Any]], list[AnchoredFile]]:
        try:
            self._verify_base_scope(primary, resolved)
        except ValueError as exc:
            raise ValueError("candidate_source_stale") from exc
        operations: list[dict[str, Any]] = []
        current: list[AnchoredFile] = []
        for index, path in enumerate(resolved.changed_paths):
            before = primary.inspect(path)
            original = resolved.base_files.get(path)
            if before.content != original:
                raise ValueError("candidate_source_stale")
            policy = before.permission_policy if original is not None else primary.create_permission_policy()
            if policy is None:
                raise ValueError("task_apply_permission_invalid")
            after = resolved.result_files[path]
            operations.append(
                {
                    "index": index, "operation": "replace" if original is not None else "create", "path": path,
                    "before_content": None if original is None else original.decode("utf-8"),
                    "before_hash": None if original is None else digest_bytes(original),
                    "after_content": after.decode("utf-8"), "after_hash": digest_bytes(after),
                    "permission_policy": policy, "parent_identity": before.parent_identity,
                    "before_identity": None if original is None else {
                        "volume": before.target_identity["volume"],
                        "file_id": before.target_identity["file_id"],
                    } if before.target_identity is not None else None,
                }
            )
            current.append(before)
        return operations, current

    def _validate_journal_operations(
        self,
        primary: AnchoredPrimary,
        resolved: ResolvedTaskReview,
        value: object,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) != len(resolved.changed_paths):
            raise ValueError("task_apply_journal_invalid")
        operations: list[dict[str, Any]] = []
        keys = {
            "index", "operation", "path", "before_content", "before_hash",
            "after_content", "after_hash", "permission_policy", "parent_identity",
            "before_identity",
        }
        for index, item in enumerate(value):
            if not isinstance(item, Mapping) or set(item) != keys:
                raise ValueError("task_apply_journal_invalid")
            operation = dict(item)
            path = resolved.changed_paths[index]
            before = resolved.base_files.get(path)
            after = resolved.result_files.get(path)
            expected_kind = "create" if before is None else "replace"
            expected_before_text = None if before is None else before.decode("utf-8")
            if (
                operation.get("index") != index
                or operation.get("operation") != expected_kind
                or operation.get("path") != path
                or operation.get("before_content") != expected_before_text
                or operation.get("before_hash")
                != (None if before is None else digest_bytes(before))
                or after is None
                or operation.get("after_content") != after.decode("utf-8")
                or operation.get("after_hash") != digest_bytes(after)
            ):
                raise ValueError("task_apply_journal_invalid")
            parent_identity = operation.get("parent_identity")
            before_identity = operation.get("before_identity")
            if not self._stable_identity(parent_identity) or (
                before is None and before_identity is not None
            ) or (
                before is not None and not self._stable_identity(before_identity)
            ):
                raise ValueError("task_apply_journal_invalid")
            policy = operation.get("permission_policy")
            if not isinstance(policy, Mapping):
                raise ValueError("task_apply_journal_invalid")
            current = primary.inspect(path)
            if before is None and dict(policy) != primary.create_permission_policy():
                raise ValueError("task_apply_journal_invalid")
            if before is not None and current.content in {before, after} and current.permission_policy != dict(policy):
                raise ValueError("task_apply_journal_invalid")
            operations.append(operation)
        return operations

    @staticmethod
    def _stable_identity(value: object) -> bool:
        return (
            isinstance(value, Mapping)
            and set(value) == {"volume", "file_id"}
            and all(
                isinstance(value.get(key), int)
                and not isinstance(value.get(key), bool)
                and value[key] >= 0
                for key in ("volume", "file_id")
            )
        )

    def _verify_primary(self, primary: AnchoredPrimary, files: Mapping[str, bytes]) -> None:
        for path, content in files.items():
            if primary.inspect(path).content != content:
                raise ValueError("task_apply_result_mismatch")

    def _verify_base_scope(
        self, primary: AnchoredPrimary, resolved: ResolvedTaskReview
    ) -> None:
        for path, content in resolved.base_files.items():
            if primary.inspect(path).content != content:
                raise ValueError("task_apply_result_mismatch")
        for path in resolved.plan["absent_output_paths"]:
            if primary.inspect(str(path)).content is not None:
                raise ValueError("task_apply_result_mismatch")

    def _verify_scope_absence(
        self, primary: AnchoredPrimary, resolved: ResolvedTaskReview
    ) -> None:
        for path in resolved.plan["absent_output_paths"]:
            if path not in resolved.result_files and primary.inspect(str(path)).content is not None:
                raise ValueError("task_apply_result_mismatch")

    def _rollback_or_reject(
        self, broker: EvidenceBroker | None, primary: AnchoredPrimary | None, marker_written: bool,
        started: Mapping[str, Any] | None, written: list[tuple[dict[str, Any], AnchoredFile, Mapping[str, Any]]],
        journal_hash: str | None, record: Mapping[str, Any], application_id: str,
        candidate: Mapping[str, Any], acceptance: Mapping[str, Any], subject_id: str, finding: str,
        operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if broker is None:
            return {"schema": "torq-candidate-application-v1", "state": "failed", "finding": finding}
        common = {
            "apply_contract": APPLY_CONTRACT, "application_id": application_id,
            "request_digest": record["request_digest"], "candidate": dict(candidate),
            "acceptance": dict(acceptance), "actor": {"subject_id": subject_id, "assurance": "local_operator_session"},
        }
        try:
            if started is None:
                _append(broker, {**common, "event": "apply_rejected", "detail": {"reason_code": finding}})
                broker.seal()
                if marker_written and primary is not None:
                    self.markers.clear(primary, installation_id=self.store.installation_id, application_id=application_id)
                return {"schema": "torq-candidate-application-v1", "state": "rejected", "application_id": application_id, "finding": finding}
            caused = broker.sequence
            rollback_started_receipt = _append(
                broker,
                {**common, "event": "apply_rollback_started", "detail": {
                    "caused_by_sequence": caused, "reason_code": finding,
                }},
            )
            assert primary is not None and journal_hash is not None
            del written
            if verify_receipt_store(broker.run_root).status != "verified":
                raise ValueError("task_apply_recovery_evidence_unverified")
            signed_rows = [
                json.loads(line)
                for line in (broker.run_root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            staged_by_path = {
                str(row["payload"]["detail"]["path"]): row
                for row in signed_rows
                if row.get("payload", {}).get("event") == "apply_file_staged"
            }
            for operation in reversed(operations):
                path = str(operation["path"])
                current = primary.inspect(path)
                before_raw = None if operation["before_content"] is None else str(operation["before_content"]).encode("utf-8")
                after_raw = str(operation["after_content"]).encode("utf-8")
                if current.content == before_raw:
                    continue
                if current.content != after_raw:
                    raise ValueError("task_apply_recovery_conflict")
                staged = staged_by_path.get(path)
                staged_identity = None if staged is None else staged.get("payload", {}).get("detail", {}).get("staged_identity")
                current_stable = None if current.target_identity is None else {
                    "volume": current.target_identity["volume"], "file_id": current.target_identity["file_id"]
                }
                if current_stable != staged_identity:
                    raise ValueError("task_apply_recovery_conflict")
                if operation["before_content"] is None:
                    if current.target_identity is None:
                        raise ValueError("task_apply_recovery_conflict")
                    delete_stage = _append(
                        broker,
                        {**common, "event": "apply_rollback_file_staged", "detail": {
                            "rollback_started_sequence": rollback_started_receipt["sequence"],
                            "operation_index": operation["index"], "path": path,
                            "base_hash": None, "staged_identity": current_stable,
                        }},
                    )
                    primary.remove(
                        path, expected_hash=str(operation["after_hash"]),
                        expected_parent_identity=current.parent_identity,
                        expected_target_identity=current.target_identity,
                    )
                    _append(
                        broker,
                        {**common, "event": "apply_rollback_file_restored", "detail": {
                            "rollback_started_sequence": rollback_started_receipt["sequence"],
                            "operation_index": operation["index"], "path": path,
                            "base_hash": None, "staged_sequence": delete_stage["sequence"],
                            "staged_receipt_hash": delete_stage["receipt_hash"],
                        }},
                    )
                else:
                    rollback_stage: dict[str, Any] | None = None

                    def record_rollback_staged(identity: dict[str, int]) -> None:
                        nonlocal rollback_stage
                        rollback_stage = _append(
                            broker,
                            {**common, "event": "apply_rollback_file_staged", "detail": {
                                "rollback_started_sequence": rollback_started_receipt["sequence"],
                                "operation_index": operation["index"], "path": path,
                                "base_hash": operation["before_hash"],
                                "staged_identity": identity,
                            }},
                        )

                    primary.replace(
                        path, expected_before_hash=str(operation["after_hash"]),
                        expected_parent_identity=current.parent_identity,
                        expected_target_identity=current.target_identity,
                        content=str(operation["before_content"]).encode("utf-8"),
                        permission_policy=dict(operation["permission_policy"]),
                        application_id=application_id, operation_index=int(operation["index"]),
                        staged_callback=record_rollback_staged, rollback=True,
                    )
                    if rollback_stage is None:
                        raise ValueError("task_apply_rollback_stage_missing")
                    _append(
                        broker,
                        {**common, "event": "apply_rollback_file_restored", "detail": {
                            "rollback_started_sequence": rollback_started_receipt["sequence"],
                            "operation_index": operation["index"], "path": path,
                            "base_hash": operation["before_hash"],
                            "staged_sequence": rollback_stage["sequence"],
                            "staged_receipt_hash": rollback_stage["receipt_hash"],
                        }},
                    )
            resolved = self._resolve_candidate_ref(candidate)
            self._verify_primary(primary, resolved.base_files)
            for path in resolved.changed_paths:
                if path not in resolved.base_files and primary.inspect(path).content is not None:
                    raise ValueError("task_apply_recovery_conflict")
            terminal = _append(
                broker,
                {**common, "event": "apply_rolled_back", "detail": {
                    "prepared_sequence": 2, "journal_hash": journal_hash,
                    "restored_scope_manifest_hash": resolved.candidate_ref["base_scope_manifest_hash"],
                }},
            )
            broker.seal()
            self.markers.clear(primary, installation_id=self.store.installation_id, application_id=application_id)
            return {"schema": "torq-candidate-application-v1", "state": "rolled_back", "application_id": application_id, "sequence": terminal["sequence"], "candidate": dict(candidate), "acceptance": dict(acceptance), "finding": finding}
        except BaseException:
            try:
                _append(
                    broker,
                    {**common, "event": "apply_recovery_required", "detail": {
                        "caused_by_sequence": broker.sequence, "reason_code": "task_apply_recovery_required",
                    }},
                )
            except BaseException:
                pass
            return {"schema": "torq-candidate-application-v1", "state": "recovery_required", "application_id": application_id, "candidate": dict(candidate), "acceptance": dict(acceptance), "finding": "task_apply_recovery_required"}

    @staticmethod
    def _record_result(record: Mapping[str, Any]) -> dict[str, Any]:
        result = record.get("result")
        if isinstance(result, dict):
            return dict(result)
        return {"schema": "torq-candidate-operation-v1", "state": record.get("state"), "finding": record.get("finding")}

    @staticmethod
    def _reason(exc: BaseException, default: str) -> str:
        if isinstance(exc, (OSError, RuntimeError, ValueError)) and exc.args and isinstance(exc.args[0], str):
            value = exc.args[0].split(":", 1)[0]
            if value.startswith(("task_", "candidate_")) and value.replace("_", "").isalnum() and len(value) <= 64:
                return value
        return default


__all__ = ["CandidateReviewService"]
