"""Durable coordinator for bounded goal-to-candidate execution."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from torq_cli.adapters.candidate_provider import (
    CandidateProviderCommand,
    CandidateProviderCommandFactory,
    parse_candidate_output,
    trusted_python_executable,
)
from torq_cli.adapters.process import ExitObservation, OwnedProcess
from torq_cli.application.task_store import TaskStore
from torq_cli.domain.task_evidence import TASK_CONTRACT
from torq_cli.safety.evidence_broker import EvidenceBroker
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    read_verified_artifact,
    restrict_owner_only_directory,
    restrict_owner_only_file,
    verify_receipt_store,
)
from torq_cli.safety.state_lock import InstallationStateLock
from torq_cli.safety.task_workspace import (
    SourceSnapshot,
    canonical_json,
    digest_bytes,
    digest_json,
    materialize_candidate,
    snapshot_source,
    snapshot_candidate_exact,
    validate_disjoint_roots,
    validate_relative_path,
    verify_output_scope,
)

CHECK_PROFILE_ID = "structural-v1"
CHECK_PROFILE_VERSION = "1.0.0"
PLAN_CONTRACT = "torq-candidate-plan-v1"
INPUT_CONTRACT = "torq-candidate-input-v1"


class ProcessOwner(Protocol):
    pid: int
    output_closed: bool

    def poll(self) -> int | None: ...

    def wait(self, *, timeout: float = 5.0) -> ExitObservation: ...

    def force_stop(self, *, timeout: float = 5.0) -> ExitObservation: ...

    def next_event(self, *, timeout: float) -> Any: ...

    def close(self) -> ExitObservation: ...


@dataclass(frozen=True, slots=True)
class TaskProject:
    project_id: str
    label: str
    root: Path


def _helper_path() -> Path:
    path = Path(__file__).parents[1] / "checkers" / "structural.py"
    if not path.is_file() or path.is_symlink():
        raise ValueError("task_checker_missing")
    return path.resolve()


def _hash_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def _safe_reason(exc: BaseException) -> str:
    if isinstance(exc, (InterruptedError, RuntimeError, ValueError)) and exc.args and isinstance(exc.args[0], str):
        value = exc.args[0].split(":", 1)[0]
        if value.startswith("task_") and value.replace("_", "").isalnum() and len(value) <= 64:
            return value
    return "task_execution_failed"


class CandidateTaskService:
    """Single-owner task authority. GET methods never invoke a provider."""

    def __init__(
        self,
        *,
        projects: Sequence[TaskProject],
        state_root: Path,
        work_root: Path,
        provider_factory: CandidateProviderCommandFactory | Callable[[str], CandidateProviderCommand],
        provider: str,
        model: str,
        owner_factory: Callable[..., ProcessOwner] | None = None,
        python_executable: Path | None = None,
        acquire_lock: bool = True,
    ) -> None:
        if not projects or len(projects) > 32:
            raise ValueError("task_projects_invalid")
        ids = [project.project_id for project in projects]
        if len(set(ids)) != len(ids) or any(not item or len(item) > 64 for item in ids):
            raise ValueError("task_project_id_invalid")
        roots = [project.root.absolute() for project in projects]
        boundaries = [*roots, state_root.absolute(), work_root.absolute()]
        if isinstance(provider_factory, CandidateProviderCommandFactory):
            boundaries.append(provider_factory.runtime_root.absolute())
        validate_disjoint_roots(boundaries)
        self.projects = {project.project_id: TaskProject(project.project_id, project.label, project.root.absolute()) for project in projects}
        self.state_root = state_root.absolute()
        self.work_root = work_root.absolute()
        self._state_lock = InstallationStateLock(self.state_root) if acquire_lock else None
        if not acquire_lock:
            self.state_root.mkdir(parents=True, exist_ok=True)
            restrict_owner_only_directory(self.state_root)
        self.work_root.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(self.work_root)
        self.evidence_root = self.state_root / "evidence"
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        restrict_owner_only_directory(self.evidence_root)
        self.store = TaskStore(self.state_root)
        self.provider_factory = provider_factory
        self.provider = provider.casefold()
        self.model = model
        self.owner_factory = owner_factory or cast(Callable[..., ProcessOwner], OwnedProcess)
        self.python_executable = (python_executable or Path(trusted_python_executable())).resolve()
        self._lock = threading.RLock()
        self._owner: ProcessOwner | None = None
        self._active_task: str | None = None
        self._cancel_requested: set[str] = set()
        self._termination_unknown: set[str] = set()
        self._worker: threading.Thread | None = None
        self._recover_reservations()

    def close(self) -> None:
        with self._lock:
            active = self._active_task
        if active is not None:
            try:
                self.stop(active)
            except (OSError, RuntimeError, ValueError):
                pass
        if self._state_lock is not None:
            self._state_lock.close()

    def _recover_reservations(self) -> None:
        data = self.store.snapshot()
        for task_id, task in data["tasks"].items():
            if task.get("state") in {"reserved", "preparing", "generating", "checking", "stop_requested"}:
                phase = str(task.get("state"))
                if phase == "stop_requested":
                    phase = "generating" if task.get("provider_dispatch") else "preparing"
                provider_dispatched = bool(task.get("provider_dispatch"))
                termination = "unknown" if provider_dispatched else "not_started"
                final_state = "termination_unknown" if provider_dispatched else "interrupted"
                try:
                    chain = ReceiptChain(
                        self.evidence_root,
                        task_id,
                        FileRunKeyStore(self.evidence_root),
                        profile_version="candidate-v1",
                        policy_version="candidate-v1",
                    )
                    broker = EvidenceBroker(chain)
                    existing = chain.covered_receipts()
                    terminal_body = next(
                        (
                            row["payload"]
                            for row in reversed(existing)
                            if isinstance(row.get("payload"), Mapping)
                            and row["payload"].get("event")
                            in {"candidate_ready", "task_failed", "task_interrupted", "task_cancelled"}
                        ),
                        None,
                    )
                    if terminal_body is None:
                        self._append(
                            broker,
                            {
                                "task_contract": TASK_CONTRACT,
                                "event": "task_interrupted",
                                "task_id": task_id,
                                "phase": phase,
                                "reason_code": "task_coordinator_restarted",
                                "caused_by_sequence": chain.sequence or None,
                                "provider_dispatch": provider_dispatched,
                                "termination": termination,
                            },
                        )
                        broker.seal()
                    else:
                        verification = verify_receipt_store(self.evidence_root / task_id)
                        manifest = json.loads(
                            (self.evidence_root / task_id / "terminal-manifest.json").read_text(
                                encoding="utf-8"
                            )
                        )
                        if verification.status == "verified" and manifest.get("sealed") is True:
                            event = terminal_body.get("event")
                            final_state = {
                                "candidate_ready": "candidate_ready",
                                "task_failed": "failed",
                                "task_cancelled": "cancelled",
                                "task_interrupted": (
                                    "termination_unknown"
                                    if terminal_body.get("termination") == "unknown"
                                    else "interrupted"
                                ),
                            }[str(event)]
                except (OSError, PermissionError, ValueError):
                    final_state = "interrupted"
                self.store.update_task(
                    task_id,
                    state=final_state,
                    finding=(None if final_state == "candidate_ready" else "task_restart_interrupted"),
                )

    def capabilities(self) -> dict[str, Any]:
        finding: str | None = None
        try:
            helper = _helper_path()
            _hash_file(helper)
            if isinstance(self.provider_factory, CandidateProviderCommandFactory):
                self.provider_factory.preflight()
            snapshot_source(next(iter(self.projects.values())).root, ())
            execution = True
        except (OSError, ValueError):
            execution = False
            finding = "task_preflight_failed"
        with self._lock:
            active = self._active_task
        blocked_unknown = any(
            task.get("state") == "termination_unknown"
            for task in self.store.snapshot()["tasks"].values()
            if isinstance(task, dict)
        )
        return {
            "schema": "torq-task-capabilities-v1",
            "execution_supported": execution,
            "can_start_task": execution and active is None and not blocked_unknown,
            "active_task_id": active,
            "provider": {"name": self.provider, "model": self.model, "authentication": "not_checked"},
            "policy": {
                "check_profile_id": CHECK_PROFILE_ID,
                "description": "Python syntax, strict JSON, and UTF-8 text structure only; unit tests are not run.",
                "applies_to_source": False,
            },
            "projects": [{"id": item.project_id, "label": item.label} for item in self.projects.values()],
            "finding": "task_termination_unknown" if blocked_unknown else finding,
        }

    def draft(self, project_id: str) -> dict[str, Any] | None:
        self._project(project_id)
        value = self.store.snapshot()["drafts"].get(project_id)
        if value is None:
            return None
        result = dict(value)
        if result.get("goal") is None:
            result["goal"] = ""
        return result

    def save_draft(
        self,
        project_id: str,
        *,
        goal: str,
        input_paths: list[str],
        output_paths: list[str],
        expected_revision: int,
    ) -> dict[str, Any]:
        self._project(project_id)
        safe_inputs = [validate_relative_path(item) for item in input_paths]
        safe_outputs = [validate_relative_path(item) for item in output_paths]
        return self.store.put_draft(
            project_id,
            goal=goal,
            input_paths=safe_inputs,
            output_paths=safe_outputs,
            expected_revision=expected_revision,
        )

    def delete_draft(self, project_id: str, *, expected_revision: int) -> None:
        self._project(project_id)
        self.store.delete_draft(project_id, expected_revision=expected_revision)

    def review_plan(
        self,
        *,
        project_id: str,
        draft_revision: int,
        input_paths: Sequence[str],
        output_paths: Sequence[str],
    ) -> dict[str, Any]:
        project = self._project(project_id)
        draft = self.draft(project_id)
        if draft is None or not draft["goal"] or draft["revision"] != draft_revision:
            raise ValueError("task_draft_revision_stale")
        if draft["input_paths"] != list(input_paths) or draft["output_paths"] != list(output_paths):
            raise ValueError("task_draft_scope_stale")
        base = snapshot_source(project.root, input_paths)
        outputs = tuple(validate_relative_path(item) for item in output_paths)
        if not outputs or len(set(item.casefold() for item in outputs)) != len(outputs):
            raise ValueError("task_output_scope_invalid")
        helper = _helper_path()
        absent_outputs = verify_output_scope(project.root, input_paths, outputs)
        provider_preflight: Mapping[str, object] = {}
        if isinstance(self.provider_factory, CandidateProviderCommandFactory):
            provider_preflight = self.provider_factory.preflight()
        binary = provider_preflight.get("binary")
        binary_hash = _hash_file(Path(str(binary))) if isinstance(binary, str) else digest_json({"fixture": self.provider})
        body = {
            "contract": PLAN_CONTRACT,
            "project_id": project_id,
            "goal": draft["goal"],
            "draft_revision": draft_revision,
            "input_paths": list(sorted(entry.path for entry in base.entries)),
            "output_paths": list(sorted(outputs)),
            "absent_output_paths": list(absent_outputs),
            "base_manifest_hash": base.manifest_hash,
            "provider": self.provider,
            "model": self.model,
            "provider_binary_hash": binary_hash,
            "check_profile_id": CHECK_PROFILE_ID,
            "check_profile_version": CHECK_PROFILE_VERSION,
            "helper_hash": _hash_file(helper),
            "limits": {"files": 32, "file_bytes": 65536, "total_bytes": 2097152},
        }
        return self.store.save_plan(body)

    def start(self, *, request_id: str, plan_hash: str) -> dict[str, Any]:
        prior = self.store.replay(request_id, plan_hash)
        if prior is not None:
            return self.task(str(prior["task_id"]))
        if any(
            task.get("state") == "termination_unknown"
            for task in self.store.snapshot()["tasks"].values()
            if isinstance(task, dict)
        ):
            raise ValueError("task_termination_unknown")
        plans = self.store.snapshot()["plans"]
        plan = plans.get(plan_hash)
        if not isinstance(plan, dict) or digest_json(plan) != plan_hash:
            raise ValueError("task_plan_unknown")
        draft = self.draft(str(plan["project_id"]))
        if draft is None or draft["revision"] != plan["draft_revision"] or draft["goal"] != plan["goal"]:
            raise ValueError("task_plan_stale")
        self._revalidate_plan(plan)
        request = {"plan_hash": plan_hash, "project_id": plan["project_id"]}
        with self._lock:
            if self._active_task is not None:
                raise ValueError("task_runtime_busy")
            task, created = self.store.reserve(request_id, request)
            if not created:
                return self.task(str(task["task_id"]))
            self._active_task = str(task["task_id"])
            self._worker = threading.Thread(target=self._run, args=(str(task["task_id"]), plan_hash, plan), daemon=True)
            self._worker.start()
        return self.task(str(task["task_id"]))

    def replay_start(self, *, request_id: str, plan_hash: str) -> dict[str, Any]:
        prior = self.store.replay(request_id, plan_hash)
        if prior is None:
            raise ValueError("task_request_replay_missing")
        return self.task(str(prior["task_id"]))

    def stop(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            current = self.store.snapshot()["tasks"].get(task_id)
            if isinstance(current, dict) and current.get("state") in {"candidate_ready", "failed", "cancelled", "interrupted", "termination_unknown"}:
                return self.task(task_id)
            if task_id != self._active_task:
                raise ValueError("task_not_active")
            self._cancel_requested.add(task_id)
            owner = self._owner
            self.store.update_task(task_id, state="stop_requested")
        if owner is not None:
            observation = owner.force_stop(timeout=5)
            if not observation.confirmed:
                with self._lock:
                    self._termination_unknown.add(task_id)
                self.store.update_task(task_id, state="termination_unknown", finding="task_termination_unknown")
        return self.task(task_id)

    def history(self) -> list[dict[str, Any]]:
        tasks = self.store.snapshot()["tasks"]
        return [self.task(task_id) for task_id in reversed(list(tasks)[-50:])]

    def task(self, task_id: str) -> dict[str, Any]:
        task = self.store.snapshot()["tasks"].get(task_id)
        if not isinstance(task, dict):
            raise ValueError("task_unknown")
        projected = dict(task)
        plan = self.store.snapshot()["plans"].get(task.get("plan_hash"))
        if isinstance(plan, dict):
            goal = str(plan.get("goal", "")).splitlines()[0][:120]
            projected["goal_summary"] = goal
            project = self.projects.get(str(plan.get("project_id")))
            projected["project_label"] = project.label if project is not None else "Configured project"
        if task.get("state") == "candidate_ready":
            projected.update(self._project_ready(task_id, task))
        projected.pop("request_id", None)
        projected.pop("request_digest", None)
        return projected

    def _project(self, project_id: str) -> TaskProject:
        project = self.projects.get(project_id)
        if project is None:
            raise ValueError("task_project_unknown")
        return project

    def _revalidate_plan(self, plan: Mapping[str, Any]) -> SourceSnapshot:
        if (
            plan.get("contract") != PLAN_CONTRACT
            or plan.get("provider") != self.provider
            or plan.get("model") != self.model
            or plan.get("check_profile_id") != CHECK_PROFILE_ID
            or plan.get("check_profile_version") != CHECK_PROFILE_VERSION
        ):
            raise ValueError("task_plan_identity_invalid")
        project = self._project(str(plan["project_id"]))
        base = snapshot_source(project.root, [str(item) for item in plan["input_paths"]])
        verify_output_scope(
            project.root,
            [str(item) for item in plan["input_paths"]],
            [str(item) for item in plan["output_paths"]],
            expected_absent=[str(item) for item in plan["absent_output_paths"]],
        )
        if base.manifest_hash != plan["base_manifest_hash"] or _hash_file(_helper_path()) != plan["helper_hash"]:
            raise ValueError("task_plan_stale")
        if isinstance(self.provider_factory, CandidateProviderCommandFactory):
            current = self.provider_factory.preflight()
            binary = current.get("binary")
            if not isinstance(binary, str) or _hash_file(Path(binary)) != plan["provider_binary_hash"]:
                raise ValueError("task_provider_binary_changed")
        return base

    def _artifact(self, broker: EvidenceBroker, name: str, content: str) -> tuple[str, str, str]:
        capability = broker.issue("orchestrator")
        path = broker.write_artifact(capability.token, name, content)
        if broker.read_artifact(path).encode("utf-8") != content.encode("utf-8"):
            raise ValueError("task_artifact_content_changed")
        relative = path.relative_to(broker.run_root).as_posix()
        return relative, ReceiptChain.hash_file(path), digest_bytes(content.encode("utf-8"))

    def _append(self, broker: EvidenceBroker, body: Mapping[str, Any]) -> dict[str, Any]:
        capability = broker.issue("orchestrator")
        return broker.append(capability.token, "audit", body)

    def _run_process(self, command: CandidateProviderCommand, task_id: str, *, timeout: float) -> tuple[bytes, ExitObservation]:
        owner = self.owner_factory(command.argv, cwd=command.cwd, env=command.environment, input_data=command.input_data)
        with self._lock:
            self._owner = owner
        try:
            stdout = bytearray()
            overflow = False
            deadline = time.monotonic() + timeout
            stop_started = False
            forced_deadline: float | None = None
            while True:
                event = owner.next_event(timeout=0.05)
                if event is None:
                    if owner.poll() is not None and owner.output_closed:
                        break
                    if time.monotonic() >= deadline and not stop_started:
                        owner.force_stop(timeout=5)
                        stop_started = True
                        forced_deadline = time.monotonic() + 5
                    elif forced_deadline is not None and time.monotonic() >= forced_deadline:
                        raise RuntimeError("task_termination_unknown")
                    continue
                if event.channel == "stdout":
                    stdout.extend(event.data)
                elif event.channel == "system":
                    overflow = True
            if overflow or len(stdout) > 1_048_576:
                raise ValueError("task_provider_output_overflow")
            observation = owner.wait(timeout=5)
            return bytes(stdout), observation
        finally:
            owner.close()
            with self._lock:
                if self._owner is owner:
                    self._owner = None

    def _run(self, task_id: str, plan_hash: str, plan: Mapping[str, Any]) -> None:
        broker: EvidenceBroker | None = None
        last_sequence: int | None = None
        phase = "preparing"
        provider_dispatched = False
        try:
            self.store.update_task(task_id, state=phase)
            base = self._revalidate_plan(plan)
            chain = ReceiptChain(self.evidence_root, task_id, FileRunKeyStore(self.evidence_root), profile_version="candidate-v1", policy_version="candidate-v1")
            broker = EvidenceBroker(chain)
            input_bundle = {"contract": INPUT_CONTRACT, "plan": dict(plan), "files": base.bundle()}
            input_text = canonical_json(input_bundle)
            input_hash = digest_bytes(input_text.encode("utf-8"))
            artifact, cipher_hash, content_hash = self._artifact(broker, "accepted-input", input_text)
            start = self._append(broker, {
                "task_contract": TASK_CONTRACT, "event": "task_start_accepted", "task_id": task_id,
                "provider_dispatch": False, "request_digest": self.store.snapshot()["tasks"][task_id]["request_digest"],
                "plan_hash": plan_hash, "base_manifest_hash": base.manifest_hash, "input_hash": input_hash,
                "artifact": artifact, "artifact_hash": cipher_hash, "artifact_content_hash": content_hash,
                "provider": self.provider, "model": self.model,
            })
            last_sequence = int(start["sequence"])
            if task_id in self._cancel_requested:
                raise InterruptedError("task_cancelled")
            phase = "generating"
            self.store.update_task(task_id, state=phase, provider_dispatch=True)
            prompt = self._prompt(plan, input_bundle, plan_hash, input_hash)
            command = self.provider_factory(prompt)
            provider_dispatched = True
            raw, observation = self._run_process(command, task_id, timeout=120)
            with self._lock:
                cancel_requested = task_id in self._cancel_requested
                termination_unknown = task_id in self._termination_unknown
            if cancel_requested:
                if termination_unknown or not observation.confirmed:
                    raise RuntimeError("task_termination_unknown")
                raise InterruptedError("task_cancelled")
            if observation.returncode != 0 or not observation.confirmed or not observation.output_complete:
                raise ValueError("task_provider_failed")
            operations, raw_hash = parse_candidate_output(raw, plan_hash=plan_hash, input_hash=input_hash)
            if self._revalidate_plan(plan).manifest_hash != base.manifest_hash:
                raise ValueError("task_source_changed")
            task_root = self.work_root / task_id
            task_root.mkdir(parents=True, exist_ok=False)
            restrict_owner_only_directory(task_root)
            candidate = materialize_candidate(task_root / "candidate", base, operations, allowed_output_paths=plan["output_paths"])
            canonical_ops = []
            for operation in operations:
                content = str(operation["content"]).encode("utf-8")
                canonical_ops.append({**operation, "content_bytes": len(content), "content_hash": digest_bytes(content)})
            candidate_bundle = canonical_json(
                {
                    "contract": "torq-candidate-bundle-v1",
                    "manifest_hash": candidate.manifest_hash,
                    "operations": canonical_ops,
                    "raw_response": raw.decode("utf-8"),
                }
            )
            artifact, cipher_hash, content_hash = self._artifact(broker, "candidate-bundle", candidate_bundle)
            generated = self._append(broker, {
                "task_contract": TASK_CONTRACT, "event": "candidate_generated", "task_id": task_id,
                "provider_dispatch": True, "start_sequence": last_sequence, "plan_hash": plan_hash,
                "input_hash": input_hash, "candidate_manifest_hash": candidate.manifest_hash,
                "raw_response_hash": raw_hash, "artifact": artifact, "artifact_hash": cipher_hash,
                "artifact_content_hash": content_hash, "provider": self.provider, "model": self.model,
                "termination": "confirmed_empty",
            })
            last_sequence = int(generated["sequence"])
            phase = "checking"
            self.store.update_task(task_id, state=phase, candidate_files=[item.path for item in candidate.entries])
            check_output, check_observation, helper_hash, argv_hash = self._check(task_root, candidate)
            with self._lock:
                if task_id in self._termination_unknown:
                    raise RuntimeError("task_termination_unknown")
                if task_id in self._cancel_requested:
                    raise InterruptedError("task_cancelled")
            check_text = check_output.decode("utf-8")
            artifact, cipher_hash, content_hash = self._artifact(broker, "structural-check", check_text)
            checked = self._append(broker, {
                "task_contract": TASK_CONTRACT, "event": "candidate_check_completed", "task_id": task_id,
                "provider_dispatch": False, "generated_sequence": last_sequence,
                "candidate_manifest_hash": candidate.manifest_hash, "check_profile_id": CHECK_PROFILE_ID,
                "check_profile_version": CHECK_PROFILE_VERSION, "helper_hash": helper_hash, "argv_hash": argv_hash,
                "exit_code": check_observation.returncode, "output_hash": digest_bytes(check_output),
                "output_complete": check_observation.output_complete, "termination": "confirmed_empty" if check_observation.confirmed else "unknown",
                "artifact": artifact, "artifact_hash": cipher_hash, "artifact_content_hash": content_hash,
            })
            last_sequence = int(checked["sequence"])
            if check_observation.returncode != 0 or not check_observation.confirmed or not check_observation.output_complete:
                raise ValueError("task_check_failed")
            source_recheck = self._revalidate_plan(plan)
            disk_candidate = snapshot_candidate_exact(task_root / "candidate", [item.path for item in candidate.entries])
            if disk_candidate.manifest_hash != candidate.manifest_hash:
                raise ValueError("task_candidate_changed")
            with self._lock:
                if task_id in self._termination_unknown:
                    raise RuntimeError("task_termination_unknown")
                if task_id in self._cancel_requested:
                    raise InterruptedError("task_cancelled")
                ready = self._append(broker, {
                    "task_contract": TASK_CONTRACT, "event": "candidate_ready", "task_id": task_id,
                    "provider_dispatch": False, "start_sequence": int(start["sequence"]),
                    "generated_sequence": int(generated["sequence"]), "check_sequence": last_sequence,
                    "plan_hash": plan_hash, "base_manifest_hash": base.manifest_hash, "input_hash": input_hash,
                    "candidate_manifest_hash": candidate.manifest_hash, "source_recheck_hash": source_recheck.manifest_hash,
                })
                del ready
                broker.seal()
                self.store.update_task(task_id, state="candidate_ready", finding=None, candidate_manifest_hash=candidate.manifest_hash, check_exit_code=0)
        except BaseException as exc:
            reason = _safe_reason(exc)
            unknown = reason == "task_termination_unknown"
            cancelled = isinstance(exc, InterruptedError) and reason == "task_cancelled"
            state = "termination_unknown" if unknown else "cancelled" if cancelled else "failed"
            if broker is not None:
                try:
                    event = "task_interrupted" if unknown else "task_cancelled" if cancelled else "task_failed"
                    termination = "unknown" if unknown else "confirmed_empty" if provider_dispatched else "not_started"
                    self._append(broker, {
                        "task_contract": TASK_CONTRACT, "event": event, "task_id": task_id,
                        "phase": phase, "reason_code": reason, "caused_by_sequence": last_sequence,
                        "provider_dispatch": provider_dispatched, "termination": termination,
                    })
                    broker.seal()
                except BaseException:
                    state = "interrupted"
                    reason = "task_evidence_write_failed"
            self.store.update_task(task_id, state=state, finding=reason)
        finally:
            with self._lock:
                if self._active_task == task_id:
                    self._active_task = None
                self._cancel_requested.discard(task_id)
                self._worker = None

    def _prompt(self, plan: Mapping[str, Any], input_bundle: Mapping[str, Any], plan_hash: str, input_hash: str) -> str:
        return (
            "Return only one JSON object with no markdown. Tools and filesystem access are unavailable. "
            "The contract field must be exactly torq-candidate-output-v1. The exact schema is "
            "{contract,plan_hash,input_hash,operations}; each operation is "
            "{operation:create|replace,path,base_hash,content}. Produce at least one changed UTF-8 file "
            "and only paths in allowed_output_paths.\n"
            + canonical_json({"plan_hash": plan_hash, "input_hash": input_hash, "goal": plan["goal"], "allowed_output_paths": plan["output_paths"], "input": input_bundle})
        )

    def _check(self, task_root: Path, candidate: SourceSnapshot) -> tuple[bytes, ExitObservation, str, str]:
        helper = _helper_path()
        helper_hash = _hash_file(helper)
        manifest_path = task_root / "structural-manifest.json"
        manifest_path.write_text(canonical_json({"contract": "torq-structural-check-v1", "files": [item.path for item in candidate.entries]}), encoding="utf-8")
        restrict_owner_only_file(manifest_path)
        argv = (str(self.python_executable), "-I", "-S", str(helper), str(manifest_path))
        command = CandidateProviderCommand(argv, str(task_root / "candidate"), {"PYTHONIOENCODING": "utf-8"}, b"", "host", CHECK_PROFILE_ID)
        output, observation = self._run_process(command, "checker", timeout=30)
        return output, observation, helper_hash, digest_json(list(argv))

    def _project_ready(self, task_id: str, task: Mapping[str, Any]) -> dict[str, Any]:
        root = self.evidence_root / task_id
        verification = verify_receipt_store(root)
        if verification.status != "verified":
            return {"state": "untrusted", "finding": verification.finding or verification.status}
        try:
            manifest = json.loads((root / "terminal-manifest.json").read_text(encoding="utf-8"))
            if manifest.get("sealed") is not True:
                raise ValueError("task_evidence_unsealed")
            receipts = [json.loads(line) for line in (root / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
            bodies = [row["payload"] for row in receipts if row.get("payload", {}).get("task_contract") == TASK_CONTRACT]
            started = next(body for body in bodies if body["event"] == "task_start_accepted")
            generated = next(body for body in bodies if body["event"] == "candidate_generated")
            checked = next(body for body in bodies if body["event"] == "candidate_check_completed")
            ready = next(body for body in bodies if body["event"] == "candidate_ready")
            input_plain = read_verified_artifact(self.evidence_root, task_id, str(started["artifact"]))
            candidate_plain = read_verified_artifact(self.evidence_root, task_id, str(generated["artifact"]))
            check_plain = read_verified_artifact(self.evidence_root, task_id, str(checked["artifact"]))
            if (
                digest_bytes(input_plain) != started["artifact_content_hash"]
                or digest_bytes(candidate_plain) != generated["artifact_content_hash"]
                or digest_bytes(check_plain) != checked["artifact_content_hash"]
                or digest_bytes(input_plain) != started["input_hash"]
                or digest_bytes(check_plain) != checked["output_hash"]
            ):
                raise ValueError("task_artifact_content_hash_mismatch")
            input_bundle = json.loads(input_plain)
            if input_bundle.get("contract") != INPUT_CONTRACT or not isinstance(input_bundle.get("plan"), dict):
                raise ValueError("task_input_artifact_invalid")
            plan = input_bundle["plan"]
            if digest_json(plan) != started["plan_hash"] or started["plan_hash"] != ready["plan_hash"]:
                raise ValueError("task_plan_artifact_mismatch")
            base = self._revalidate_plan(plan)
            if input_bundle.get("files") != base.bundle() or base.manifest_hash != started["base_manifest_hash"]:
                raise ValueError("task_base_artifact_mismatch")
            paths = list(plan["input_paths"])
            for path in plan["output_paths"]:
                if path not in paths and (self.work_root / task_id / "candidate" / path).exists():
                    paths.append(path)
            disk = snapshot_candidate_exact(self.work_root / task_id / "candidate", paths)
            if disk.manifest_hash != ready["candidate_manifest_hash"] or base.manifest_hash != ready["source_recheck_hash"]:
                raise ValueError("task_result_identity_mismatch")
            bundle = json.loads(candidate_plain)
            check = json.loads(check_plain)
            expected_validators = {
                ".py": "python_ast",
                ".json": "strict_json",
                ".md": "utf8_text",
                ".txt": "utf8_text",
            }
            expected_checked = [
                {
                    "path": item.path,
                    "validator": expected_validators[Path(item.path).suffix.casefold()],
                }
                for item in disk.entries
            ]
            expected_argv = (
                str(self.python_executable),
                "-I",
                "-S",
                str(_helper_path()),
                str(self.work_root / task_id / "structural-manifest.json"),
            )
            raw_response = bundle.get("raw_response")
            operations = bundle.get("operations")
            if (
                bundle.get("contract") != "torq-candidate-bundle-v1"
                or bundle.get("manifest_hash") != disk.manifest_hash
                or not isinstance(raw_response, str)
                or digest_bytes(raw_response.encode("utf-8")) != generated["raw_response_hash"]
                or not isinstance(operations, list)
                or check.get("status") != "passed"
                or set(check) != {"contract", "checked", "status"}
                or check.get("contract") != "torq-structural-result-v1"
                or check.get("checked") != expected_checked
                or checked["helper_hash"] != plan["helper_hash"]
                or _hash_file(_helper_path()) != checked["helper_hash"]
                or checked["candidate_manifest_hash"] != disk.manifest_hash
                or checked["argv_hash"] != digest_json(list(expected_argv))
            ):
                raise ValueError("task_result_artifact_mismatch")
            parsed_operations, _ = parse_candidate_output(
                raw_response.encode("utf-8"),
                plan_hash=ready["plan_hash"],
                input_hash=ready["input_hash"],
            )
            expected_ops = []
            for operation in parsed_operations:
                raw_content = str(operation["content"]).encode("utf-8")
                expected_ops.append(
                    {
                        **operation,
                        "content_bytes": len(raw_content),
                        "content_hash": digest_bytes(raw_content),
                    }
                )
            if operations != expected_ops:
                raise ValueError("task_candidate_artifact_mismatch")
            disk_by_path = {entry.path: entry.content_hash for entry in disk.entries}
            if any(
                disk_by_path.get(str(operation["path"])) != operation["content_hash"]
                for operation in expected_ops
            ):
                raise ValueError("task_candidate_content_mismatch")
            return {"verified": True, "candidate_files": [item.path for item in disk.entries], "check": {"profile": CHECK_PROFILE_ID, "exit_code": checked["exit_code"]}}
        except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
            return {"state": "untrusted", "finding": "task_result_verification_failed", "verified": False}


__all__ = ["CandidateTaskService", "TaskProject"]
