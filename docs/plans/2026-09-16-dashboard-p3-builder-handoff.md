# Dashboard P3 builder handoff

Status: **IMPLEMENTED — READY FOR FINAL PACKAGE AND G1D VERIFICATION**

This implementation delivers the bounded P3 contract approved in
`2026-09-16-dashboard-p3-g1r-review.md`. It preserves P2 candidate creation and
adds artifact-backed review, readable exact-byte changes and check results,
immutable correction and continuation tasks, bounded searchable history,
separate signed acceptance, and an explicit evidence-backed Apply transaction.

Apply remains candidate-only until the user accepts and applies a verified
candidate. It never invokes a provider. It uses anchored filesystem access,
per-user/host exclusion, signed staged identities, a shared pending marker, and
restart recovery. P3 does not claim a portable destination compare-and-swap:
external editors and other non-TORQ writers must pause writes and atomic saves
during Apply. All checks immediately before and after the rename remain in
place, and observed drift fails closed.

## Main implementation

- `src/torq_cli/domain/task_review.py`: sealed historical candidate resolver,
  exact artifact and lineage validation, complete byte-aware diff projection.
- `src/torq_cli/domain/candidate_decision_evidence.py`: closed semantic review
  and Apply lifecycles, including forward and rollback staged identities.
- `src/torq_cli/application/review_store.py`: durable versioned request/index
  store with CAS and no-follow bounded reads.
- `src/torq_cli/application/candidate_review.py`: review, correction, immutable
  child tasks, continuation, accept/apply replay, eligibility, and recovery.
- `src/torq_cli/safety/primary_transaction.py` and `primary_marker.py`: anchored
  POSIX/Windows primary transactions, semantic metadata preservation, staging,
  leases, markers, and recovery primitives.
- `src/torq_cli/interfaces/fleet_http.py`, `interfaces/cli.py`, and Fleet assets:
  authenticated closed HTTP routes plus the full-height Plan/Changes/Checks,
  correction, acceptance, Apply, recovery, and paginated history UI.
- Candidate task/store, receipt, and evidence modules carry the minimal P3
  lineage, stable chronology, semantic validation, and authenticated-prefix
  support while preserving the P2 schemas and evidence.

## Durable regressions

Repository tests cover exact resolver and evidence schemas, authenticated HTTP
and replay, task history, platform-native transaction behavior, source
unchanged before Apply, signed acceptance/application, correction child tasks,
stale continuation plans, continuation process-death reconstruction, a CAS race
with an ordinary draft, and restart after a native rollback rename before its
restored receipt. The rollback test proves exact base restoration, marker
clearance, idempotent idle recovery, and no extra provider/check launches.

Independent fixtures additionally exercised all ten forward crash boundaries,
unsigned and substituted staging files, third-byte conflicts, source-context
drift, legacy P2 evidence compatibility, replay-store substitution, and real
process death during rollback. Those results are recorded in
`2026-09-16-dashboard-p3-g1d-verification.md`.

## Builder verification

- `python -m pytest -q -p no:cacheprovider --basetemp E:\tmp\torq-p3-builder-final-focus tests/test_candidate_review.py tests/test_candidate_decision_evidence.py tests/test_primary_transaction.py tests/test_task_http.py tests/test_candidate_history.py tests/test_hermetic.py` — **56 passed**.
- `python -m ruff check src tests` — **passed**.
- `python -m mypy --platform win32 --cache-dir tmp/mypy-p3-final-win32 src/torq_cli` — **passed, 96 source files**.
- `python -m mypy --platform linux --cache-dir tmp/mypy-p3-final-linux src/torq_cli` — **passed, 96 source files**.
- `python -m mypy --platform darwin --cache-dir tmp/mypy-p3-final-darwin src/torq_cli` — **passed, 96 source files**.

The independent G2A verdict is **APPROVE** in
`2026-09-16-dashboard-p3-g2a-review.md`. Packaging, installed-wheel browser
verification, and delivery remain with G1D/root.
