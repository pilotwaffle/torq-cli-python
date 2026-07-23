# TORQ CLI r5 Full PRD Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use `executing-plans` and TDD to implement this plan phase by phase.

**Goal:** Complete T-02, T-03, T-05, T-07, and T-08 through T-36 with evidence-backed phase gates.

**Architecture:** Preserve the existing offline Foundation contracts, then add a standalone `torq_cli.core` shared engine, connector adapters behind injected transports, isolated execution and receipt services, and CLI commands as thin application boundaries. Live-provider, clean-machine, branch-protection, signing, and release tasks remain evidence gates: implementation may prepare them, but completion requires the actual external exercise named by the PRD.

**Tech Stack:** Python 3.11+, PyYAML, stdlib dataclasses/JSON/SQLite/subprocess/pathlib, pytest, Ruff, mypy, setuptools/build, GitHub Actions.

---

## Phase 1 — T-02, T-03, T-05, T-07

1. Audit `E:\TORQ-CONSOLE\torq_console\conductor` and MMH paths with a read-only inventory script; write failing tests for completeness of required subsystem verdicts.
2. Exercise the available resume contract against an interrupted fixture or record a precise unavailable finding; produce `docs/architecture/extraction-viability-audit.md` with REUSE/WRAP/REBUILD decisions and quantified rebuild scope.
3. Add a closed provider-surface matrix schema, probe runner, and six-provider decision document. Tests require nine surfaces per provider, explicit unavailable findings, dated ToS citations, usage/attestation states, and downstream decision flags.
4. Execute every locally available provider surface without extracting credentials; record unavailable surfaces honestly.
5. Write the T-05 runtime/repository/packaging decision citing T-02, including namespaced tags, version source, and OS implications.
6. Complete T-07 hermetic test enforcement: frozen environment fixture, socket/network deny with mock allowlist, invalid-test-mode fail-closed behavior, encrypted-fallback headless fixture, and four-job CI declaration.
7. Run Phase 1 gate: focused phase tests, full pytest, Ruff, mypy, named mutants, build, wheel smoke, and evidence-schema validation.

## Phase 2 — T-08 through T-12

1. Write failing tests for normalized provider requests/responses, provenance, retry taxonomy, budget preflight, cumulative cost, and run/stage metadata; implement `torq_cli.core.provider_engine` minimally.
2. Write failing graph tests for COMPILE→VALIDATE→POLICY→EXECUTE→OBSERVE→RECOMPILE, strategy profiles, dry-run default, live double opt-in, gate stops, conditional routing, and prohibited push/merge operations; implement graph compiler/executor.
3. Write policy fixtures for every bucket/defect class, test-only-fix rejection, HIGH queue pause, bounded repair, and policy-stamped receipts; implement v3.1.3 executable policy.
4. Write outbound and pre-persistence redaction tests using fake secrets and configurable blocking patterns; implement one shared pattern registry with two enforcement points.
5. Build deterministic shared conformance fixtures and a seeded divergence test for provider normalization, routing, retry, and redaction.
6. Run the full Phase 2 gate twice plus static, mutation, build, and wheel checks.

## Phase 3 — T-13 through T-21

1. Define one connector protocol with injected auth/session/transport interfaces and normalized status/response models.
2. For Claude, Codex, Grok, Kimi, Z.ai, and DeepSeek, write failing mock-transport tests for auth states, grants, attestation, cancellation/resume where supported, taxonomy, rate limiting, and fail-closed surface selection; implement adapters without ambient secret discovery.
3. Add credential-vault interface implementations/stubs per OS contract; direct connectors accept only injected credential values and never serialize them.
4. Implement `auth status` and `harness inspect` with four-state health, profile satisfiability, mismatch/unattestable findings, and secret-free rendering.
5. Add six-connector hermetic conformance fixtures including unreported usage, malformed usage, malformed response, and stream drop.
6. Implement a manual-only live-smoke command/report schema, execute all locally available connectors independently, and preserve unavailable findings for missing authority or credentials.
7. Run Phase 3 mock gate on the local OS, full suite/static/build checks, then assess live and three-OS evidence separately.

## Phase 4 — T-22 through T-27

1. Write failing sandbox tests for copy/worktree isolation, primary hash preservation, protected reads/writes, traversal/symlink escape, workspace locking, and dirty-primary policy; implement the sandbox manager.
2. Write failing execution-policy tests for command/network allowlists, environment filtering, process-tree cancellation, and all four ceilings; implement fail-closed enforcement.
3. Write walking-skeleton and fault-routed governed-run tests, including mandatory G2A evidence, escalation receipts, timeline messages, loop exhaustion, and off-contract halt; assemble the orchestrator.
4. Write receipt-chain tests for sequence/hash/artifact tamper, atomic append, manifest seal, redaction, encrypted artifacts, permissions, and retention; implement local tamper-resistant storage using injected signing/encryption backends.
5. Write approval tests for untouched primary, pinned-tree apply, drift refusal, rejection/timeout preservation, and absence of push/merge code paths; implement proposal/apply boundary.
6. Write receipt-derived usage/budget tests including `unreported`; implement reporting.
7. Run Phase 4 safety gate, full suite/static/mutation/build checks, and OS-capability evidence audit.

## Phase 5 — T-28 through T-31

1. Write interactive-driver tests for zero-to-valid setup, grant/eligibility refusal, and idempotent rerun; implement `torq setup` with injected I/O and explicit output path.
2. Write CLI tests for dry/live opt-ins, prime-directive attestation, cancellation/checkpoint, all resume divergence classes, timelines, verdict, usage, proposal, and receipt pointers; implement `torq run`.
3. Extend status tests for disconnected, ungranted, ineligible, degraded, and fully effective states; implement runtime-effective status without weakening offline status.
4. Write pristine/tampered/incomplete evidence tests and implement `torq evidence verify` with distinct exit codes.
5. Run Phase 5 gate plus full suite/static/mutation/build/wheel checks.

## Phase 6 — T-32 through T-36

1. Generate a dated production-readiness audit tied to an actual source baseline, covering all six required areas; retest every sandbox finding and resolve/defer High items explicitly.
2. Implement the pinned E2E fixture and run repo-compat dry-run. Run the live heterogeneous-provider path only with available configured credentials and explicit operator-safe application target.
3. Write `SECURITY.md` from actual behavior and findings, including all providers/OS backends, honest signing scope, no-product-telemetry wording, and vendor endpoint disclosure.
4. Add `torq --version`, platform install documentation, artifact manifest/hash verification, and three-OS packaging workflows; execute every locally possible artifact test.
5. Prepare release notes and signed-tag command. Tag, branch-protection validation, clean-machine installs, and publication require the real standalone Git/release environment and must not be fabricated.
6. Run Phase 6 gate and report exact implemented, verified, unavailable, and operator-blocked items.

## Universal completion rules

- Write and observe each failing test before production code.
- Run focused tests after each task and the complete gate after each phase.
- Never print or persist credentials; never discover ambient secrets for tests.
- Never push, merge, release, alter branch protection, bill providers, or apply primary changes without the explicit operator action required by the PRD.
- Delete only inside `E:\Torq-CLI\tmp`.
- Do not mark live, multi-OS, signing, release, or clean-machine criteria complete without actual evidence.
