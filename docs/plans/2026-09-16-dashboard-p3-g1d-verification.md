# P3 independent verification

Status: **PASS for the bounded P3 local delivery gates**, verified 2026-09-17 America/Chicago. GitHub CI and merge are subsequent delivery gates; this receipt does not claim them in advance.

Baseline: P2 main `e32ecdc3ed2ab3df1f6fa00e3d49994532c4474a`. G1D: GPT-6; G1R: GPT-5.6-Sol; backend and UI builders: GPT-Daybreak-Blue-Latest; G2A: GPT-5.6-Terra. G1R approved the contract and amendments; G2A approved the implementation in the separate review receipt.

## Repository and package checks

- Full repository suite: **1,055 passed, 13 skipped**, 1,068 collected, zero failures/errors. JUnit: `tmp/p3-release-tests.xml`; Windows runtime, network denied.
- Ruff `src tests`: passed. Mypy `src` for `win32`, `linux`, and `darwin`: passed, 96 source files on each target. These are static platform checks, not native Linux/macOS execution.
- Node task/review and resumed-task harnesses pass through the full Python suite. Delayed selection, project recovery, history pagination, draft preservation, rejected/rolled-back outcomes, and lost-response request identity are covered.
- Source distribution and wheel build passed. Clean-install `scripts/wheel_smoke.py tmp/p3-release-dist` passed.
- Verified every packaged `torq_cli/` file against its source counterpart: no mismatches.
- Wheel SHA-256: `0954d29f26cc9dc41eaddb23ae44c5c15607d6e91cc37cbaecc1d4eba15b1a79`.
- Named security mutants: 30/30 killed. These are the repository's existing security mutants, not a claim of mutation coverage for every new P3 branch.

## Installed-wheel browser flow

Served an isolated loopback dashboard from `tmp/p3-wheel-installed/torq_cli/__init__.py`, not the source checkout. Used an offline fixture provider through actual Windows `OwnedProcess`, followed by the packaged structural checker. No live paid-provider authentication or generation is claimed.

1. Saved a goal/scope, reviewed the plan, and built an actual sealed candidate.
2. Opened Plan, Changes, and Checks. Requested a docstring correction, reviewed the child plan, and started a separate child candidate. The child showed parent lineage and the full original-base diff, including CRLF-to-LF facts.
3. Accepted the child; exact source bytes still contained the original subtraction. Applied it; source bytes exactly matched the reviewed addition/docstring candidate.
4. Continued after Apply; a fresh draft opened with focus on the goal. Edited that goal to multiplication and built another candidate. Its diff base was the newly applied docstring/addition bytes, and its lineage identified the applied child. This third build left primary source unchanged.
5. Reloaded and reopened history. All three candidate chains, the correction and acceptance decisions, and the application independently verified and were sealed. Exactly six process launches were recorded: three explicit provider starts and three structural checks. Review, Accept, Apply, and Continue added no provider launches.
6. Verified desktop and 320px layouts, light/dark presentation, keyboard arrow navigation between tabs, search, and 200% CSS zoom. Mobile document width remained 320px; zoomed layout did not overflow the viewport. CSS zoom is not browser-toolbar zoom.

Evidence: `tmp/p3-wheel-browser/wheel-verification.json`, `tmp/p3-wheel-browser-*.json`, and `tmp/p3-wheel-*.png`. One automation attempt selected the previous candidate while the next Start response was pending; reopening after completion verified the correct continuation. It is not counted as a successful navigation attempt.

Separately, the real browser recovery button restored an interrupted two-file source fixture and cleared its marker (`tmp/p3-cut-b9c96f26`).

## Independent transaction and authority checks

A fresh two-file process-death matrix ran against the final production code. Every case passed its expected outcome, with exact source inventories, verified evidence, and idempotent repeat recovery. `tmp/p3-final-matrix.json` records all fixture roots.

| Process death boundary | Verified outcome |
|---|---|
| Reserved marker, before prepared, after prepared | Signed no-write rejection; original source preserved; marker cleared |
| After started | Verified rollback; original source preserved |
| Before signed stage receipt | Unknown temp preserved; recovery block retained |
| After signed stage, before rename | Exact signed temp reconciled; original source restored |
| After first file write | Partial application rolled back exactly |
| After both file writes | Full scope verified; application completed and sealed |
| Applied terminal before seal | Existing outcome sealed without rewriting source |
| Seal before marker cleanup | Sealed outcome projected; marker cleared without reapplication |

Additional actual rollback process deaths passed before rollback rename (`tmp/p3-cut-51d8dc53`) and after rollback rename before restored receipt (`tmp/p3-cut-cc83685e`). Recovery reused signed rollback identities, restored exact bytes, sealed rollback, cleared the marker, and returned idle on repetition. A same-byte identity substitution after rollback rename was refused and preserved (`tmp/p3-cut-97a343df`). Durable repository tests also exercise native rollback restart and continuation crash/CAS recovery.

Other independent checks passed:

- Native replacement/restoration/create/remove, supported metadata, owner-only creates, missing parents, separate-process exclusion, hard-link/ADS refusal, and third-byte/same-byte-identity conflict refusal.
- A second TORQ installation sharing the authority refused new work while a crashed transaction's marker remained active.
- Unchanged scoped context modified after a crash was preserved and prevented false application success (`tmp/p3-cut-21584987`).
- Tampered encrypted journal and missing signer key blocked recovery without creating or changing any files or identity (`tmp/p3-cut-53c82388`, `tmp/p3-cut-45d56318`).
- Two disjoint signed applications resisted mutable-result swaps and cross-record locator substitution; exact retries under a new session caused no source writes (`tmp/p3-replay-8144a319`).
- Historical P2 evidence generated before the P3 implementation remained readable with exact CRLF/no-final-newline facts after cache removal and live-source drift. Wrong sequence and artifact tampering were refused; original P2 evidence hashes remained unchanged (`tmp/p3-legacy-a720f551`).

## Practical limits

P3 creates/replaces bounded UTF-8 files in existing directories. It does not execute generated code, run project unit tests, change credentials, or perform Git operations. Structural-check success is labelled accordingly. Generation retains the existing production platform containment restrictions.

Application is recoverable per file, not a multi-file atomic transaction. The kernel lease coordinates TORQ installations for the same user/host. Pause non-TORQ editors, autosave, formatters, and other writers during Apply/recovery: a write racing the final check and native rename can be overwritten. Observed/preexisting third-party bytes or substituted identities are rejected. Unknown staging and a create-rollback crash after deletion but before its restored receipt remain explicitly recovery-blocked; absence alone is not identity evidence.

## macOS CI follow-up

The initial PR head `b46b448` passed Windows and Linux CI but failed the native macOS transaction test because Python on Darwin does not expose `os.listxattr`. This was a fail-closed platform defect, not a passing platform gate. The follow-up uses descriptor-bound Darwin `flistxattr` and ACL APIs, bounded parsing, strict errors, and before/after BSD-flag checks; it does not skip the test or substitute path-based reads.

Root reran the native adapter, hermetic boundary, and candidate integration selection: **50 passed**; Ruff and Mypy for all three target platforms passed. G2A separately approved the adapter and distinguished mocked API tests from real Darwin execution. Native macOS CI must pass on the updated PR head before merge. The detailed browser/wheel evidence above describes the pre-follow-up package; dashboard assets and Windows execution behavior are unchanged by this Darwin-only fix.
