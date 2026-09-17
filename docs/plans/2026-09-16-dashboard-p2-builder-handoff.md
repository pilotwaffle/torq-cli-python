# Dashboard P2 builder handoff

Date: 2026-09-16
Builder: GPT Daybreak Blue latest
Contract: `2026-09-16-dashboard-p2-contract.md`
Gate: Sol G1R `APPROVE — READY_FOR_P2_BUILDER`

## Delivered scope

P2 now provides a real bounded goal-to-candidate path. A configured Fleet launch can open **New task**, persist a project-scoped goal and file scope, review an immutable plan, submit one idempotent task, watch or stop it, and inspect a verified candidate result. The provider returns a closed JSON file-content envelope. TORQ validates and materializes those contents in a separate candidate root and launches only the packaged `structural-v1` helper through `OwnedProcess`.

The source project remains unchanged. `candidate_ready` is released only from a verified and sealed task receipt chain after artifact decryption and independent identity checks. P2 does not expose an Apply or Accept action and does not claim unit tests, arbitrary command execution, OS sandboxing, or P3 delivery.

## Implementation

- `application/candidate_tasks.py` owns plan review, replay-before-revalidation, durable reservation, dispatch, cancellation, restart recovery, evidence append/seal, and independent candidate projection.
- `application/task_store.py` provides owner-only bounded durable drafts, plans, request reservations, monotonic tombstones, and closed-schema reads and writes.
- `domain/task_evidence.py` and `domain/run_evidence.py` enforce the discriminated `torq-candidate-task-v1` event schema, task/run identity, provider/model continuity, legal lifecycle ordering, and exactly one terminal before seal.
- `safety/task_workspace.py` and `safety/state_lock.py` enforce explicit UTF-8 scope, protected-path denial, held-ancestor guarded reads, exact candidate inventory, disjoint roots, and a kernel-backed single owner.
- `adapters/candidate_provider.py` supports the configured native Claude executable and existing supported CLI bridges with tools disabled and a closed JSON output contract. Binary and helper identities are pinned in the reviewed plan and rechecked before dispatch.
- `checkers/structural.py` performs fixed Python syntax, strict JSON, and UTF-8 checks without executing candidate code.
- `safety/receipts.py` adds no-create key loading and verified artifact readback so projection cannot create keys or trust mutable task-cache state.
- `interfaces/fleet_http.py` exposes authenticated, same-origin, closed-schema task routes. Mutation rotation is serialized in the browser. Exact start replay can recover one ambiguous rotated response without granting the old session general access or permitting a new dispatch.
- `interfaces/cli.py` accepts explicit project allowlists and derives safe state/work roots outside the source project when task roots are omitted. Ordinary Fleet still requires its run root. Native wrapper scripts are rejected.
- `data/fleet/index.html`, `task.css`, and `task.js` add the task workspace, durable scoped drafts, immediate plan invalidation, serialized mutations, stable request identity, active-task resume, Stop, and selectable history. The form stays disabled until its authoritative draft revision is loaded.

The main dashboard plan now records P2 as a separately reviewed candidate-only implementation. README documents the launch shape, 32-file/64-KiB-per-file limits, structural checks, and the P3 boundary.

## Repository tests added or extended

- `tests/test_task_evidence.py`
- `tests/test_candidate_tasks.py`
- `tests/test_candidate_task_integration.py`
- `tests/test_task_http.py`
- `tests/test_task_cli.py`
- `tests/test_task_ui_runtime.py`
- `tests/js/task_runtime.test.cjs`
- `tests/js/task_resume.test.cjs`
- `tests/fixtures/candidate_provider.py`
- `tests/test_fleet_ui.py`
- `tests/test_hermetic.py`
- affected chat HTTP/CLI regressions

The tests cover closed task evidence, no-create projection, unsealed/tampered downgrades, exact candidate inventory, empty-project creation, source immutability, accepted replay across draft changes and restarts, durable unknown termination, cancellation versus late completion, store ABA/reserved-key injection, provider output validation, HTTP authentication/origin/schema/recovery, CLI default task roots, and browser mutation/edit/request identity primitives.

## Checks run by the builder

- Focused P0/P1/P2 regression command: `python -m pytest -q -p no:cacheprovider tests/test_chat_http.py tests/test_task_http.py tests/test_task_ui_runtime.py tests/test_candidate_task_integration.py tests/test_task_evidence.py tests/test_candidate_tasks.py tests/test_task_cli.py tests/test_fleet_ui.py tests/test_hermetic.py` — **83 passed**.
- Final UI subset after the initialization gate: `python -m pytest -q -p no:cacheprovider tests/test_fleet_ui.py tests/test_task_ui_runtime.py` — **10 passed**.
- Terminal capability regression after installed-wheel review: `python -m pytest -q -p no:cacheprovider tests/test_task_ui_runtime.py` — **2 passed**; its DOM harness proves a resumed task reaching `candidate_ready` refreshes capabilities and permits review/start of the next task.
- `python -m ruff check src tests` — passed.
- `python -m mypy --cache-dir tmp/mypy-p2-final src` — passed, 90 source files.
- `node --check src/torq_cli/data/fleet/task.js` — passed.
- `node tests/js/task_runtime.test.cjs src/torq_cli/data/fleet/task.js` — passed.
- Independent slow-response DOM harness — passed: fields remained disabled through delayed capability/draft reads and the first autosave used the loaded revision.
- `git diff --check` — passed.
- `python -m build --wheel --outdir tmp/p2-final-dist` — passed.

The restricted test run first encountered the known Windows temporary-directory ACL denial. The identical focused commands passed with normal temporary-directory access; no test was skipped or represented as a product failure.

## Independent evidence available at handoff

G1D's real fixture run used the production coordinator, real `OwnedProcess`, a separate provider fixture process, the packaged structural checker process, EvidenceBroker, and receipt verification. It produced a verified candidate, left the original source unchanged, used exactly two process launches, returned the same accepted task on replay and restart without redispatch, and rejected candidate-byte tampering and extra inventory. A separate empty-project run created a candidate file while leaving the original empty. These results are recorded in `2026-09-16-dashboard-p2-g1d-verification.md` and the referenced local reports.

Authenticated source-browser checks covered the happy path, delayed plan response, durable reload/history, mobile width, auth/origin denials, and source immutability. Installed-wheel browser and final full-suite validation are owned by G1D after this freeze.

No live Claude or paid provider request was made. Local provider work was limited to executable discovery and help/version preflight supplied by G1D.

## Build artifact

`E:\Torq-CLI\tmp\p2-final-dist\torq_cli-0.2.0-py3-none-any.whl`

The wheel build log confirms inclusion of the task coordinator, evidence/store/workspace/provider modules, packaged structural checker, and `task.css`/`task.js`/`index.html` assets.

## Remaining boundary

P2 ends at a sealed candidate. The dashboard does not show a full source diff, run arbitrary project tests, refine a candidate conversationally, or apply it to the source tree. P3 requires its own reviewed transaction and must bind any later review/application to this immutable candidate result.
