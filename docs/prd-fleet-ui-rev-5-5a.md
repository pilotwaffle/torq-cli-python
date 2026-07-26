# TORQ Fleet UI PRD — Rev 5.5A implementation status

Status: **implemented on `agent/rev55a-fleet-ui`; pending protected-main merge
and CI at the time of this document commit.**

This document is the status delta for
[`prd-fleet-ui.md`](prd-fleet-ui.md). It supersedes conflicting historical
implementation-status statements in that file; the base requirements remain
normative where they do not conflict with this delta.

## 1. Outcome

The Rev 5.5A backend contract and Fleet control UI are implemented. The UI may
build against the v3 envelope now: the evidence vocabulary, migration boundary,
snapshot shape, eligibility model, pending correlations, SSE transport, mutation
routes, fixture corpus, rollback anchor, and recovery semantics are pinned.

The packaged `torq fleet --serve` command remains safe when no mutation service
is attached: the server publishes explicit ineligibility reasons and does not
pretend a read-only launch can write evidence. A live orchestrator supplies the
run-bound context, action, and recovery controllers to enable those controls.

## 2. Completed backend delta

| Item | Status | Implemented contract |
|---|---|---|
| H1 | Complete | Receipt payload key allowlists, bounded operator-facing labels, MIME validation, and verifier-side rejection of undeclared/unbounded prose. |
| B7 | Complete | Certificate schemas 1 and 2 are explicit legacy read-only classes; new runs use schema 3. Existing evidence remains readable but cannot be extended under the corrected contract. |
| B1 | Complete | Authority lookup is keyed by `(writer_role, transition, decision)` and the decision domain is closed. |
| B2 | Complete | Current receipts carry `run_id`; append and verification bind it to the certificate and run root. |
| B3 | Complete | Catalog lanes carry `required: bool`; terminal-decision preconditions derive required lanes from authenticated evidence. |
| B6 | Complete | `stage_interrupted` is `observed`; `evidence_basis` has a closed transition-specific vocabulary. |
| B8 | Complete | `torq-fleet-envelope-v3` contains `{snapshot, annotations, session, eligibility, pending}`; the nested snapshot is `torq-fleet-snapshot-v3`. |
| B9 | Complete | The server computes session capabilities, per-mutation eligibility/reasons, and pending correlation IDs. |
| B10 | Complete | Bounded SSE with polling fallback; governed context, action-resolution, recovery-confirmation, and recovery routes. |
| B11 | Complete | Generated authority corpus with a declared completeness rule, pinned digest, per-rule precondition fixtures, negative lifecycle cases, and named mutants. |
| B4 | Complete | Certificate schema 3, monotonic manifest generations, predecessor hashes, and a root-signed external head detect stale-manifest replay and downgrade. |
| B5 | Complete | Empty abandonment is allowed only for a planned/cataloged run with no attempt history and all approval/action/terminal/coverage/pending-command guards satisfied. |

## 3. Completed Fleet UI

The wheel-bundled frontend implements:

- board/detail rendering, six-lane signal rail, mini monitor, and all nine lane
  states with text and glyphs rather than color alone;
- exactly 54 state color tokens: nine states × foreground/background/border ×
  dark/light, with contrast tests;
- system, dark, and light themes; one 48rem responsive breakpoint; no horizontal
  overflow at 320 CSS pixels;
- semantic list/listitem structure, roving keyboard focus, Home/End/Escape,
  deduplicated live announcements, reduced motion, and accessible aggregate
  monitor labels;
- v3 eligibility/pending rendering, action resolution, and two-step recovery;
- recovery confirmations held only in lexical memory and cleared after use,
  cancellation, error, navigation, or state/coverage/generation changes;
- SSE reconnect with polling fallback, and a durable notification-dedup ledger
  keyed by `(run_id, action_id)` and persisted before notification delivery.

## 4. Security boundary

The manifest head is outside the run directory and signed by the long-lived root
identity. It detects rollback of only the run directory, stale valid manifests,
head deletion/substitution, and certificate downgrade. It does **not** defend:

- compromise of the same-user root private identity; or
- rollback of the entire filesystem volume containing both run and external
  head.

Those cases require a hardware-backed monotonic counter, OS keystore service
with non-exportable identity and monotonic storage, or a remote transparency
log. No same-filesystem counter is represented as stronger than it is.

## 5. Delivery and verification evidence

Implementation commits, in dependency order:

1. `b83dabb` — receipt prose bounds (H1).
2. `ce72b73` — legacy class and evidence corrections (B7, B1, B2, B3, B6).
3. `166f13d` — v3 snapshot/control envelope (B8, B9).
4. `8cbe408` — routes, SSE, and conformance corpus (B10, B11).
5. `5545a38` — manifest anchor and empty abandonment (B4, B5).
6. `27d83b3` — accessible Fleet control UI.

The merge gate requires all of the following after documentation reconciliation:

- Ruff, mypy, full pytest, and all named mutants;
- JavaScript syntax and package-asset tests;
- real-browser desktop/dark and 320px/mobile/light checks;
- action-pending, recovery-token, notification-dedup, keyboard, and no-overflow
  browser probes;
- parallel security, maintainability, and styling audits; and
- protected-main GitHub CI.

Final command counts and the protected-main merge reference are recorded in the
pull request and release handoff rather than forecast here.
