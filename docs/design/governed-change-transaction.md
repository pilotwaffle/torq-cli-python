# Governed Change Transaction — Design

**Status:** Draft for operator review. Design only — **no implementation is
included**. Resolves blocker decisions D1–D3.
**Provenance:** designed against `origin/main`
`72e86932168f93b8c77a65b4da76b9f06008d27b` and reconciled onto `main` via
PR #55 after PR #54 squash-merged the pre-Review-7 revision. The original
design history (initial design, Review 7 revision, independent re-review) is
archived on the historical branch `design/governed-change-transaction`
(commits `5260f5e`, `03c5d8e`, `da4d9f6`); this document is the internally
authoritative revision and does not depend on that branch.
**Product intent:** SPEC.md §1.1, §7 Step 1. **Authoritative for implemented
behavior:** `src/torq_cli/safety/{approval,workspace,receipts}.py`,
`src/torq_cli/application/{orchestrator,e2e,run_command}.py`,
`src/torq_cli/domain/{evidence_transitions,run_evidence,stage_response}.py`,
`src/torq_cli/interfaces/{cli,fleet_http}.py`.

> **Terminology.** Today the governed run records an approval outcome without
> performing a governed application of the audited change. This is a
> **non-enforcing approval path and a product-integrity gap** — not a
> conventional fail-open that writes unauthorized changes (nothing is written
> today). The gap is that "approved" is reported as "completed" with no
> governed write. This design closes that gap.

---

## 0. The three events (read first)

| Event | Receipt (schema v3) | Meaning | Producer (writer role; §2.6) |
|---|---|---|---|
| **approval_granted** | `action_resolved` (resolution=`approved`) | A verified actor authorized the proposal. **Nothing is written to the primary tree.** `action_resolved` is **non-terminal** for an approval. | `operator_gateway` (submitted to the broker process) |
| **apply_started** | `change_apply_started` | The transaction has begun under the per-primary apply lease; journal prepared and hashed. Outcome unknown. | `applier` (inside the broker process) |
| **change_applied** | `change_applied` | The transaction committed: every operation applied, final primary tree hash re-verified, manifest `result_tree_hash` matched. **Non-terminal — the mandatory verified-success prerequisite for `run_decision(completed)`.** | `applier` (inside the broker process) |

**Invariant E0 (approval ≠ application):** an `approval_granted` MUST NOT be
reportable as, or imply, successful application. `completed` is forbidden
unless a valid `change_applied` receipt exists and references the approved
manifest. Today's `outcome_map {"approved":"completed"}` is replaced by the
state machine in §2.

---

## 1. Current gap (code-verified)

- `ApprovalBoundary.apply()` exists and is correct (`safety/approval.py:30-52`).
- Exercised in **one place**: the fixture `application/e2e.py:69-71`.
- The governed run reaches `awaiting_approval` (`orchestrator.py:587`), opens
  `action_opened` with `outcome_map {"approved":"completed","rejected":"blocked"}`
  (`:1272-1276`), and `resolve_action` (`:371-387`) **only appends a receipt**.
- `resolve_action` then `terminalize`+`seal()`s the chain (`receipts.py`), so
  nothing can follow approval in the same chain.
- The builder's `proposal` (`orchestrator.py:531-534`) is a status dict, not a
  `ChangeProposal` (no pinned hash, no file map).
- `primary` is not a parameter on `RunController`/`GovernedOrchestrator`/
  `EvidenceBrokerProcess` — the apply site has no primary path today.
- The receipt transition vocabulary is **closed and machine-enforced**
  (`evidence_transitions.py`, `run_evidence.py`); new lifecycle receipts
  require a schema change, not an addition.

**Net:** approval records an outcome without performing a governed application.

---

## 2. Receipt schema v3 (Decision D1)

`schema_version = "3.0.0"` — using the **existing** receipt/manifest
`schema_version` field (Review 7, Finding 2). **No parallel
`receipt_schema_version` is introduced.** Major version because approval and
terminalization semantics change incompatibly: approval is no longer terminal.
Certificate schema versions remain separate (they describe certificate
structure, not receipt lifecycle). Verification dispatches from the existing
receipt/manifest `schema_version`; mixed receipt versions in one chain remain
invalid.

### 2.1 Lifecycle transitions (schema v3) — `run_decision` is the ONLY terminal

```
change_proposed             # non-terminal — proposal lifecycle transition (§2.5)
action_opened               # non-terminal
action_resolved             # non-terminal (even when resolution=approved)
approval_invalidated        # non-terminal (Review 7, Finding 8) — pre-apply authz denial
change_apply_started        # non-terminal
change_applied              # NON-TERMINAL — mandatory verified-success prerequisite for completed
change_apply_failed         # non-terminal evidence
change_recovery_started     # non-terminal
change_recovery_completed   # non-terminal
change_recovery_required    # non-terminal — keeps the run open and unsealed
run_decision                # THE ONLY terminal run transition (completed|blocked|failed)
seal
```

**`run_decision` is the only terminal run transition (Review 7, Finding 3).**
Every other lifecycle transition — `change_proposed`, `action_opened`,
`action_resolved`, `approval_invalidated`, `change_apply_started`,
`change_applied`, `change_apply_failed`, `change_recovery_started`,
`change_recovery_completed`, `change_recovery_required` — is **non-terminal**.
`change_applied` is verified-success evidence and the mandatory prerequisite
for `run_decision(completed)` — it is not itself terminal. Recovery
transitions are non-terminal until a final `run_decision`.
`change_recovery_required` keeps the run open and unsealed.

**Proposal ordering (mandatory, §2.5):**

```
builder or repair output
→ derive and encrypt change manifest
→ change_proposed
→ final G2A audit of that exact generation
→ action_opened
```

A repair that changes output produces another `change_proposed` with the same
`change_set_id` and an incremented `change_manifest_generation`; approval for
the earlier generation is invalidated (§8.3).

Every governed transaction lifecycle event is declared in `TRANSITION_RULES`.
`CURRENT_AUDIT_TRANSITIONS` is changed **only** for genuinely non-lifecycle
observed evidence; **no** lifecycle transition is accepted through the generic
audit-event exception. The closed-schema consistency audit (§2.6) covers every
transition above.

### 2.2 Resolution behavior

**Rejection** (no workspace/primary mutation):
```
action_resolved(resolution=rejected) → run_decision(decision=blocked) → seal
```

**Approval + successful application:**
```
action_resolved(resolution=approved)
  → change_apply_started
  → change_applied
  → run_decision(decision=completed)
  → seal
```

**Approval + recoverable apply failure:**
```
action_resolved → change_apply_started → change_apply_failed
  → rollback/recovery → change_recovery_completed
  → run_decision(decision=failed) → seal
```

**Uncertain / interrupted filesystem state:**
```
action_resolved → change_apply_started → change_recovery_required
```
The chain remains **unsealed** until recovery proves either the original tree
was restored **or** the complete intended transaction was applied and verified.
`completed` is never reported, and no successful decision is sealed, while FS
state is uncertain.

### 2.3 Required schema-v3 contract work (not "automatically compatible")

The design mandates changes to (each specified at implementation time, not
hand-waved):
- **`TRANSITION_RULES`** — new `TransitionRule(writer_role, transition,
  evidence_basis, precondition)` rows for each §2.1 transition (complete rows
  in §2.6). `evidence_basis` is exactly one of the closed enum values
  `observed` / `derived` / `submitted` — never prose; the descriptive proof
  inputs belong in the payload/precondition documentation. Writer roles and
  bases follow the §2.6 authority matrix; there is no `broker` writer role
  (the broker is a process boundary, not a receipt authority identity).
  `run_decision` is the ONLY terminal lifecycle transition;
  `change_proposed`, `action_opened`, `action_resolved`,
  `approval_invalidated`, `change_apply_started`, `change_applied`,
  `change_apply_failed`, `change_recovery_started`,
  `change_recovery_completed`, and `change_recovery_required` are all
  **non-terminal**. `change_applied` is verified-success evidence and the
  mandatory prerequisite for `run_decision(completed)`, not a terminal
  transition.
- **Lifecycle vs audit-event ownership** — every governed transaction
  lifecycle event is declared in `TRANSITION_RULES` (the rows above).
  `CURRENT_AUDIT_TRANSITIONS` is changed **only** for genuinely non-lifecycle
  observed evidence; no lifecycle transition is accepted through the generic
  audit-event exception.
- **Payload-key allowlists** — new bounded whitelists alongside
  `ACTION_OPENED_KEYS`/`ACTION_RESOLVED_KEYS`/`RUN_DECISION_KEYS` for each new
  transition (e.g. `CHANGE_APPLIED_KEYS`).
- **Local payload validators** — `validate_receipt_payload` branches for each
  new transition's bounded fields.
- **Lifecycle validators** — `validate_v3_receipt_contract`: `run_decision` is
  the only terminal run transition (Review 7, Finding 3). `completed` is
  forbidden without a valid `change_applied` (a non-terminal prerequisite)
  referencing the approved change manifest (§4-change_set). `change_proposed`,
  `action_resolved(approved)`, `approval_invalidated`,
  `change_apply_started`, `change_applied`, `change_apply_failed`, and all
  recovery transitions are **non-terminal**. `change_recovery_required`
  keeps the chain unsealed until a final `run_decision`.
- **⚠ Load-bearing v2 rewrite (review R1.2; precision gaps closed in re-review
  Vector 3):** making `action_resolved(approved)` non-terminal is **not**
  achievable by adding transitions alone. The v2 code at `run_evidence.py:1116-
  1168` maps an approved action to `mapped_decision="completed"` via
  `action_outcomes[action_id]["approved"]` (sourced from
  `orchestrator.py:1273-1276`'s `outcome_map {"approved":"completed"}`), and the
  operator-gateway `run_decision` branch then sets `terminal_decision=True`.
  `validate_v3_receipt_contract` MUST change **three** coupled things in that
  `1149-1168` window: (i) `outcome_map` for approval transitions no longer maps
  to `completed`; (ii) the operator-gateway `run_decision` terminal-decision
  logic must make `run_decision` — not `change_applied` — the sole terminal run
  transition; (iii) the outcome-comparison at **`run_evidence.py:1156-1157`**
  (`resolved_action[1] != payload.get("decision")`) must be reworked — once
  `outcome_map` stops mapping `approved→completed`, that comparison fires
  `operator_decision_outcome_mismatch` unless updated to the v3 non-terminal
  approval model. Additionally, the **writer_role for v3
  `run_decision(completed)`** must be pinned (the `applier` writer role, R1.1),
  and the `validate_v3` branch that emits `completed` — whether operator_gateway
  *or* the generic `run_decision` branch at `run_evidence.py:1168-1280` — MUST
  enforce the `change_applied` prerequisite (the generic v2 branch sets
  `terminal_decision=True` for `completed` at `:1259-1261` with no such
  prerequisite). The seal-time `validate_v3(sealed=True)` terminal-state
  validator (next bullet) is an independent backstop.
- **Terminal-state validators** — enforce §2.2 ordering and the no-seal-while-
  uncertain rule: `seal()` (`receipts.py:1654` calls `validate_*_receipt_contract
  (..., sealed=True)`) is rejected if the chain ends in `change_recovery_required`
  or any non-terminal transition without a final `run_decision`; sealing is
  permitted only after a terminal `run_decision`.
- **Schema cross-check (re-review Vector 2):** the manifest's `schema_version`
  MUST equal the receipts' `schema_version`; any mismatch rejects as
  `version_inconsistency` (the existing finding). Dispatch pins to the receipts'
  value (`versions[0]`), so a `2.0.0`-receipts/`3.0.0`-manifest store cannot
  mis-route to `validate_v3`.
- **Transition preconditions (re-review Vector 8):** each new `TransitionRule`
  carries an explicit `precondition` (complete rows in §2.6). In particular
  `approval_invalidated` has precondition `action_resolved(approved) in chain
  AND no change_apply_started in chain` — it is permitted only after
  `action_resolved(approved)` and forbidden once the FS transaction has begun
  (§6.2); `change_applied` has precondition `valid change_apply_started
  referencing the same (change_set_id, change_manifest_generation,
  change_manifest_hash) tuple`; and `change_proposed` carries the
  generation/ordering precondition (§2.5). These are machine-enforced by the
  lifecycle validator, not just prose.
- **Payload-key allowlists (review R1.6) — concretely specified:** each new
  transition gets an exact `*_KEYS` frozenset (matching the codebase pattern at
  `run_evidence.py:138-140,282-302`) and a `validate_receipt_payload` branch.
  Every field that identifies the governed change proposal uses the canonical
  names `change_set_id` / `change_manifest_generation` /
  `change_manifest_hash`; the stale generic names `manifest_hash` /
  `manifest_generation` never appear in change payloads, and the receipt-store
  `_manifest_generation` never appears in a change-proposal or apply payload.
  Every governed transaction lifecycle receipt carries `provider_dispatch`
  with value exactly **false** (E13; a true value is rejected as
  `provider_dispatch_forbidden` — the provider never receives an apply
  capability, §3). Fields referencing `ReceiptChain.write_artifact()` output
  (`change_manifest_artifact`, `actor_artifact`) validate as `_ARTIFACT_PATH`
  — bounded relative artifact paths with traversal rejection and artifact-hash
  verification — never as opaque IDs:
  - `CHANGE_PROPOSED_KEYS = {change_set_id, change_manifest_generation,
    change_manifest_hash, change_manifest_artifact,
    change_manifest_artifact_hash, workspace_tree_hash, result_tree_hash,
    provider_dispatch}` — full definition, validators, and ordering in §2.5.
    This frozenset replaces the orphaned `MANIFEST_SEALED_KEYS` (removed):
    manifest sealing is proven by `change_proposed`, and the G2A binding
    remains the final G2A receipt's work (§8.3), so no separate
    `manifest_sealed` transition exists.
  - `APPROVAL_INVALIDATED_KEYS = {action_id, change_set_id,
    change_manifest_generation, change_manifest_hash, subject_id,
    policy_version, reason, provider_dispatch}` — full definition and rules
    in §6.2.
  - `CHANGE_APPLY_STARTED_KEYS = {change_set_id, change_manifest_generation,
    change_manifest_hash, journal_hash, journal_sequence, prior_tree_hash,
    provider_dispatch}`
  - `CHANGE_APPLIED_KEYS = {change_set_id, change_manifest_generation,
    change_manifest_hash, journal_hash, prior_tree_hash, post_tree_hash,
    operation_count, applied_subject_id, applied_assurance_level,
    actor_artifact, actor_artifact_hash, authorization_policy,
    authorization_result, provider_dispatch}` — bounded `operation_count`
    plus the authenticated `journal_hash` replace any unbounded file list;
    per-operation detail lives in the journal, not the receipt.
  - `CHANGE_APPLY_FAILED_KEYS = {change_set_id, change_manifest_generation,
    change_manifest_hash, journal_hash, reason, recoverable,
    provider_dispatch}` — `reason` bounded enum; `recoverable` bool.
  - `CHANGE_RECOVERY_STARTED_KEYS = {abandoned_run_id, change_set_id,
    change_manifest_generation, change_manifest_hash, journal_hash, lock_key,
    operator_subject_id, recovery_reason, provider_dispatch}`
    (operator-acknowledged). `lock_key` is the `primary_path_hash` naming the
    kernel lock (§3.2); `recovery_reason` is a bounded enum
    (`abandoned_journal`, `interrupted_apply`, `uncertain_fs_state`). PID,
    executable path, and process-start data are **not** receipt fields — they
    may be written to a separate diagnostic artifact only, and never control
    lock ownership or recovery authorization (§3.2.1).
  - `CHANGE_RECOVERY_COMPLETED_KEYS = {change_set_id,
    change_manifest_generation, change_manifest_hash, journal_hash, outcome,
    restored_tree_hash, result_tree_hash, journal_reconciled,
    provider_dispatch}` — `outcome` is a bounded enum with outcome-dependent
    hash requirements: `restored` REQUIRES `restored_tree_hash` (equal to the
    pre-apply `primary_tree_hash`) and forbids `result_tree_hash`;
    `completed_verified` REQUIRES `result_tree_hash` (equal to the
    `change_applied` `post_tree_hash`) and forbids `restored_tree_hash`.
    `journal_reconciled` is bool.
  - `CHANGE_RECOVERY_REQUIRED_KEYS = {change_set_id,
    change_manifest_generation, change_manifest_hash, journal_hash,
    uncertain_since_sequence, provider_dispatch}`
  - `VERIFIED_ACTOR_KEYS` = the §6.1 actor field set, all
    `_OPAQUE_ID`/`_ARTIFACT_PATH`/enum.
  In every frozenset above, `provider_dispatch` MUST be exactly false.
  The floor validator (`_oversized_value`, `run_evidence.py:396`) alone is
  insufficient — every transition MUST have its exact whitelist or the receipt
  becomes a bounded-but-open signed-prose channel. The closed-schema
  consistency audit (§2.6) confirms every lifecycle transition now has one.
- **Modified existing schemas — exact schema-v3 allowlists (not
  descriptions):**
  - `ACTION_OPENED_KEYS_V3 = {action_id, type, scope, target, summary,
    allowed_resolutions, caused_by_sequence, change_set_id,
    change_manifest_generation, change_manifest_hash,
    change_manifest_artifact_hash, workspace_tree_hash, result_tree_hash,
    g2a_receipt_hash, g2a_attempt_id, repair_cycle, provider_dispatch}` —
    binds the exact approved target (§8.3 tuple + G2A binding); the approved
    outcome no longer maps directly to `completed` (R1.2).
  - `ACTION_RESOLVED_KEYS_V3 = {action_id, resolution, opened_sequence,
    change_set_id, change_manifest_generation, change_manifest_hash,
    subject_id, assurance_level, actor_artifact, actor_artifact_hash,
    provider_dispatch}` — `resolver_identity` removed; `subject_id` +
    `assurance_level` mandatory (§6.1).
  - `RUN_DECISION_KEYS_V3` — decision-specific subsets:
    - `completed`: `{decision, outcome, change_set_id,
      change_manifest_generation, change_manifest_hash,
      change_applied_sequence, result_tree_hash, provider_dispatch}` —
      rejected without a valid `change_applied` at `change_applied_sequence`
      (E2).
    - `failed` after apply/recovery: `{decision, outcome, change_set_id,
      change_manifest_generation, change_manifest_hash,
      terminal_receipt_sequence, reason, provider_dispatch}` —
      `terminal_receipt_sequence` names the causative `change_apply_failed` /
      `change_recovery_completed` receipt; `reason` is a bounded enum.
    - `blocked` after rejection or approval invalidation: `{decision,
      outcome, action_sequence, reason, provider_dispatch}` —
      `action_sequence` names the causative `action_resolved(rejected)` /
      `approval_invalidated` receipt; `reason` is a bounded enum.
- **Schema-version dispatch (Review 7, Finding 2)** — `verify_receipt_store`
  (`receipts.py:1695`) branches on the **existing** receipt/manifest
  `schema_version` (`receipts.py:1721-1756`): a chain stamped `3.0.0` routes to
  `validate_v3_receipt_contract`; `2.0.0` retains `validate_v2_receipt_contract`
  unchanged. **No parallel `receipt_schema_version` field is introduced** — the
  existing field is authoritative. `_writer_contract_finding`
  (`receipts.py:117-122`) dispatches to the matching payload validator. The
  portable/external-trust verifier path (`receipts.py:1768`) gains the same
  branch. Certificate schema versions remain separate (certificate structure).
  Mixed receipt versions in one chain remain invalid (today's
  `version_inconsistency` at `receipts.py:1753`). During migration, v2 and v3
  runs coexist by stamping their chains; a v2 chain is never extended with v3
  transitions.
- **Portable verification** — the offline verifier (`verify_receipt_store`)
  gains v3 rules; v3 receipts remain exportable and third-party-verifiable.
- **Certificate compatibility — introducing `applier` is a contract change:**
  the `applier` writer role requires ALL of: addition to `_WRITER_ROLES`; an
  applier signing key in the run certificate; a `_CERTIFICATE_SCHEMA_VERSION`
  bump; broker capability authorization for the role; and portable-verifier
  support for the new role. Existing keys are preserved. There is no separate
  `broker` writer role — the broker is a process boundary, not a receipt
  authority identity (§2.6).
- **Mutation coverage** — new named mutants (§11) cover each new invariant.
- **Schema-v2 backward verification** — see §2.4.

### 2.4 Schema-v2 evidence — verifiable, exportable, read-only, non-resumable

Schema-v2 evidence MUST remain:
- **verifiable** under v2 rules (the v2 verifier is retained unchanged);
- **exportable** (portable verification still works for v2 chains);
- **read-only** (no rewrite in place);
- **unsupported for resuming a schema-v3 change transaction** — a v2 chain
  cannot be extended with v3 apply transitions. A run that began under v2 and
  needs governed apply must be re-run under v3.

`schema_version` (the existing field) is `3.0.0` for v3 receipts and their
terminal manifest, recorded at chain creation and checked at verify dispatch
(Review 7, Finding 2). Mixed-version chains are rejected
(`version_inconsistency`, the existing finding).

### 2.5 `change_proposed` — the proposal lifecycle transition

`change_proposed` is a governed, **non-terminal** lifecycle transition that
proves the change-manifest artifact was created, encrypted, hashed, and made
immutable for its generation. It replaces any separate manifest-sealing event:
sealing the manifest is not a distinct state-machine state, so there is **no
`manifest_sealed` transition** (and no `MANIFEST_SEALED_KEYS`). The G2A
binding remains the final G2A receipt's work (§8.3).

**Mandatory ordering (§2.1):**

```
builder or repair output
→ derive and encrypt change manifest
→ change_proposed
→ final G2A audit of that exact generation
→ action_opened
```

**Repair:** a repair that changes output produces another `change_proposed`
with the same `change_set_id` and an incremented
`change_manifest_generation`; approval targeting the earlier generation is
invalidated (§8.3).

**Transition authority (`TRANSITION_RULES` row):**

```
TransitionRule(
  writer_role="orchestrator",    # governed orchestrator derives the manifest
  transition="change_proposed",
  evidence_basis="derived",      # closed enum: observed | derived | submitted
  precondition="change_manifest_generation == max recorded generation + 1
                (or 1 for the first proposal of the change_set_id) AND
                no action_opened exists for this (change_set_id,
                change_manifest_generation)")
```

The derivation's proof inputs — the sealed `workspace_tree_hash` and the
recomputed `change_manifest_artifact_hash` — are bound in the payload below
and checked by the validators; they are payload/precondition facts, not
`evidence_basis` prose. Manifest derivation runs inside the broker process
boundary; the broker is not a receipt writer role (§2.6).

**Payload — exact closed allowlist (`CHANGE_PROPOSED_KEYS`); all fields
mandatory, using bounded IDs, hashes, artifact paths, and integers consistent
with the existing closed receipt-schema conventions:**

```
CHANGE_PROPOSED_KEYS = {
  change_set_id,                 # _OPAQUE_ID — immutable per proposal lineage
  change_manifest_generation,    # bounded positive integer, begins at 1
  change_manifest_hash,          # sha256:… (§8.2 domain-separated)
  change_manifest_artifact,      # _ARTIFACT_PATH — encrypted artifact path
  change_manifest_artifact_hash, # sha256:… recomputed on load
  workspace_tree_hash,           # sha256:… captured at workspace seal
  result_tree_hash,              # sha256:… == workspace_tree_hash in v1 (§8.1)
  provider_dispatch,             # bool — MUST be false (E13)
}
```

**Field validators (`validate_receipt_payload` branch):** `change_set_id`
validates as `_OPAQUE_ID` (`run_evidence.py:64`); `change_manifest_artifact`
validates as `_ARTIFACT_PATH` — a bounded relative artifact path with
traversal rejection, and the stored artifact hash is verified against
`change_manifest_artifact_hash`; every `*_hash` field validates as a
`sha256:` digest; `change_manifest_generation` validates as a bounded
positive integer; `provider_dispatch` validates as bool with value exactly
false (a true value is rejected as `provider_dispatch_forbidden` — the
provider never receives an apply capability, §3).

**Lifecycle-order rule:** `action_opened` is rejected unless a
`change_proposed` referencing the exact `(change_set_id,
change_manifest_generation, change_manifest_hash)` tuple precedes it and the
final successful G2A audit binds that same generation (§8.3).

**Sealed-state rule:** `change_proposed` is non-terminal; a chain ending in
`change_proposed` (or any other non-terminal transition) cannot seal without a
final `run_decision` (§2.3 terminal-state validator).

**Tests / mutants:** §11.1 generation-ordering and repair tests; mutants
**M45** (generation/ordering bypass) and **M47** (`provider_dispatch=true`
accepted).

### 2.6 Closed-schema consistency audit (reconciliation revision)

Mechanical enumeration of every schema-v3 governed **receipt** transition —
**11 receipt transitions, audited one row at a time — plus the `seal` chain
operation, audited separately** (`seal` is not a receipt and has no
`TRANSITION_RULES` row). Every `evidence_basis` is the exact closed enum
(`observed` / `derived` / `submitted`) — never prose. There is **no `broker`
writer role**: the broker is a process boundary, not a receipt authority
identity. The audit confirms that there is **no** transition defined only in
prose, **no** payload allowlist without a transition, **no** lifecycle
transition routed through `CURRENT_AUDIT_TRANSITIONS`, **no** stale generic
`manifest_hash`/`manifest_generation` field in any change payload, and **no**
terminal transition other than `run_decision`.

**Authority matrix (writer role × evidence basis):**

| Transition | Writer role | Evidence basis |
|---|---|---|
| `change_proposed` | `orchestrator` | `derived` |
| `action_opened` | `orchestrator` | `derived` |
| `action_resolved` | `operator_gateway` | `submitted` |
| `approval_invalidated` | `applier` | `derived` |
| `change_apply_started` | `applier` | `observed` |
| `change_applied` | `applier` | `observed` |
| `change_apply_failed` | `applier` | `observed` |
| `change_recovery_started` | `applier` | `observed` |
| `change_recovery_completed` | `applier` | `observed` |
| `change_recovery_required` | `applier` | `observed` |
| `run_decision(completed)` | `applier` | `derived` |
| `run_decision(failed)` after apply/recovery | `applier` | `derived` |
| `run_decision(blocked)` after rejection or approval invalidation | `operator_gateway` | `derived` |

`action_opened` stays with the `orchestrator` — the governed orchestrator
creates the audited proposal and opens the action; `operator_gateway`
resolves the action, it does not open it. `run_decision(blocked)` is written
by `operator_gateway` in both pre-apply blocked paths (rejection and
`approval_invalidated`), because no apply/recovery evidence exists at that
point; every post-apply decision is written by `applier`.

| Transition | Writer role | Evidence basis | Precondition | `*_KEYS` allowlist | Field validators | Lifecycle-order rule | Sealed-state rule | Test | Named mutant |
|---|---|---|---|---|---|---|---|---|---|
| `change_proposed` | `orchestrator` | `derived` | generation == max+1 (or 1); no `action_opened` for the tuple | `CHANGE_PROPOSED_KEYS` (§2.5) | `_OPAQUE_ID`/`_ARTIFACT_PATH`/sha256/bounded int/bool=false | precedes G2A audit and `action_opened` | non-terminal | §11.1 ordering/repair tests | M45, M47 |
| `action_opened` | `orchestrator` | `derived` | preceding `change_proposed` + final successful G2A audit for the exact tuple | `ACTION_OPENED_KEYS_V3` (§2.3) | bounded ID/enum/sha256/bounded int/bool=false; approved no longer maps to `completed` | after `change_proposed` + G2A | non-terminal | E0/E2 tests | M31, M33 |
| `action_resolved` | `operator_gateway` | `submitted` | `action_opened` for the same `action_id` | `ACTION_RESOLVED_KEYS_V3` (§2.3) | `_OPAQUE_ID`/`_ARTIFACT_PATH`/sha256/enum/bool=false | after `action_opened` | non-terminal even when approved | §6.1 identity tests | M31, M43 |
| `approval_invalidated` | `applier` | `derived` | `action_resolved(approved)` in chain AND no `change_apply_started` | `APPROVAL_INVALIDATED_KEYS` (§6.2) | `_OPAQUE_ID`/sha256/bounded int/enum reason/bool=false | followed by terminal `run_decision(blocked\|failed)` | non-terminal; no seal from here | §6.2/§11.1 denial tests | M46 |
| `change_apply_started` | `applier` | `observed` | `action_resolved(approved)` for the same tuple + authz recheck allowed + generation == max + journal hashed | `CHANGE_APPLY_STARTED_KEYS` (§2.3) | sha256/bounded int/bool=false | only after approval of the max generation | non-terminal | §11.1 crash/durability tests | M34 |
| `change_applied` | `applier` | `observed` | valid `change_apply_started` for the same tuple; recomputed primary hash == `result_tree_hash` | `CHANGE_APPLIED_KEYS` (§2.3) | sha256/`_OPAQUE_ID`/`_ARTIFACT_PATH`/enum/bounded int/bool=false | after `change_apply_started`; at most one per run (E4) | NON-terminal prerequisite for `completed` | E0/E2 tests | M33, M36 |
| `change_apply_failed` | `applier` | `observed` | `change_apply_started` in chain | `CHANGE_APPLY_FAILED_KEYS` (§2.3) | sha256/bounded enum reason/bool/bool=false | after `change_apply_started` | non-terminal | §9.5 rollback tests | — (covered by E5) |
| `change_recovery_started` | `applier` | `observed` | kernel lock acquirable (§3.2.1) + operator acknowledgment | `CHANGE_RECOVERY_STARTED_KEYS` (§2.3) | `_OPAQUE_ID`/sha256/bounded int/enum/bool=false | only after abandonment | non-terminal | §9.5 recovery tests | — (lock authority: §3.2.1) |
| `change_recovery_completed` | `applier` | `observed` | `change_recovery_started` in chain | `CHANGE_RECOVERY_COMPLETED_KEYS` (§2.3) | sha256/enum/bool/bool=false; outcome-dependent required hash (`restored` ⇒ `restored_tree_hash`; `completed_verified` ⇒ `result_tree_hash`) | after `change_recovery_started`; followed by `run_decision` | non-terminal | §9.5/E5 tests | — |
| `change_recovery_required` | `applier` | `observed` | `change_apply_started` in chain + uncertain FS state | `CHANGE_RECOVERY_REQUIRED_KEYS` (§2.3) | sha256/bounded int/bool=false | keeps the chain open | non-terminal; **seal rejected** until a final `run_decision` | §9.5 no-seal-while-uncertain test | — |
| `run_decision` | decision-specific (authority matrix above) | `derived` | `completed` ⇒ valid `change_applied` at `change_applied_sequence`; `failed` ⇒ causative apply/recovery receipt; `blocked` ⇒ causative rejection/invalidation receipt | `RUN_DECISION_KEYS_V3` decision subset (§2.3) | enum + decision-specific prerequisite + bool=false | THE ONLY terminal transition | terminal; enables seal | E0/E2 tests | M33 |

**`seal` — audited separately (chain operation, not a receipt; no
`TRANSITION_RULES` row):** performed by the seal machinery inside the broker
process boundary (not a receipt writer role); precondition: the chain ends in
a terminal `run_decision`; never permitted while the chain ends in
`change_recovery_required` or any other non-terminal transition; validator
`validate_v3_receipt_contract(sealed=True)`; tests: §9.5 seal-resume.

**Audit result:** **11 governed receipt transitions audited + 1 seal
operation audited separately.** 11/11 receipt rows carry an exact writer
role, evidence-basis enum, precondition, payload allowlist, validator set,
ordering rule, sealed-state rule, test reference, and named mutant where
security-relevant; every defect class listed above is empty.

---

## 3. Broker owns the transaction (Decision D2)

The apply transaction executes **inside the trusted broker process** — not the
CLI process, not an untrusted provider process. The provider/CLI never receive
an apply capability.

### 3.1 Immutable run context (established at broker startup)

```text
GovernedRunContext
  run_id
  primary_root              # established at startup, NOT accepted later via RPC
  governance_state_root     # §4
  workspace_root            # §4
  change_manifest_artifact  # encrypted artifact reference (§8)
  change_manifest_hash
  policy_version
```

The primary path is fixed when the broker starts. The operator-gateway approval
RPC sends only:
```text
action_id
resolution
actor assertion / reference   # §6
```
The broker **independently loads and verifies** the approved manifest and audit
binding from authenticated run evidence; it never trusts a manifest hash
supplied by the RPC.

### 3.1.1 Broker plumbing (concrete signature changes — review R2.1)

Today `EvidenceBrokerProcess.start(evidence_root, run_id, *, profile_version,
policy_version, allowed_roles, startup_timeout)` (`evidence_broker.py:661-671`)
and `_broker_process_main(sender, evidence_root, run_id, profile_version,
policy_version, allowed_roles)` (`:676-683`) take **no primary**, and
`RunController.__init__` (`run_command.py:62-68`) has none either. The
`GovernedRunContext` is therefore greenfield plumbing, threaded as:
```
cli.py _handle_run  →  RunController(primary_root, governance_state_root, …)
                   →  create_active_runtime  →  EvidenceBrokerProcess.start(
                         evidence_root, run_id, *,
                         governed_run_context=GovernedRunContext(...),   # NEW
                         profile_version, policy_version, allowed_roles)
                   →  _broker_process_main(..., governed_run_context)     # NEW arg
```
`primary_root` enters only at `RunController`/`_handle_run`, is placed in the
immutable `GovernedRunContext`, and crosses the process boundary as a **spawn
argument** (not an RPC parameter). No approval RPC accepts or can mutate
`primary_root`. This is Phase-A work and is mandatory before any v3 apply.

### 3.2 Per-primary apply lease — kernel-held lock (Review 7, Finding 1)

A process-local `RLock` is insufficient, and `O_CREAT|O_EXCL` + stored-PID
probing is **also insufficient**: a process can exit and its PID can be reused
before the requesting broker probes (`pidfd_open`/`OpenProcess`/libproc would
then pin the *replacement* process and a dead owner would appear live).

**Authoritative exclusion is a kernel-held per-primary file lock.** The owning
broker keeps the file descriptor / handle **open for the entire apply/recovery
transaction**; process termination automatically releases the kernel lock.

```text
ApplyLease (kernel-held)
  canonical_primary_path
  primary_path_hash           # sha256(canonical primary path) -> lock filename
  owning_run_id               # diagnostic only — NOT ownership authority
  owning_process_id           # diagnostic only — NOT ownership authority
  acquired_at                 # diagnostic only
  lease_version               # diagnostic only
```

Stored under `<governance_state_root>/locks/<primary_path_hash>`. Lock primitive:
- **POSIX:** `fcntl` record lock (`F_SETLK` exclusive on a byte range) or
  `flock(LOCK_EX)` on the lock fd. The fd is held open by the owning broker for
  the whole transaction; on process exit the kernel releases it.
- **Windows:** `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK)` on the lock handle; the
  handle is held open; on process exit the kernel releases it.

Requirements:
- **atomic exclusive acquisition across processes** (kernel-enforced, not
  metadata-based);
- **shared by every TORQ run targeting the same primary** (keyed by primary);
- **no silent steal of a live owner** — the kernel lock IS the liveness proof.
  PID, executable path, and process-start time are **diagnostic only** and must
  never decide ownership;
- **never steal a lock merely because metadata looks old**;
- **explicit recovery for an abandoned journal** — recovery begins only *after*
  the kernel lock becomes acquirable (the prior owner process is gone). The
  abandoned journal remains on disk for recovery regardless of the lock state;
- **bounded lock metadata**; **no dependence on shared memory between brokers**.

**The lease is NOT held during model execution.**

### 3.2.1 Stale-owner — kernel lock is the authority (Review 7, Finding 1)

The earlier PID-probe model (`pidfd_open`/`proc_pidpath`/`OpenProcess`) is
**removed**. Process-identity probing cannot defeat PID reuse between exit and
probe. Instead:

- **Acquisition** is `fcntl`/`flock`/`LockFileEx`. If acquisition fails with
  `EAGAIN`/`ERROR_LOCK_VIOLATION`, another live broker holds it → do not steal.
- **Live owner test** = "the kernel lock is held." There is no separate probe.
- **Abandonment** = "the kernel lock is acquirable" (the prior owning process
  has terminated and the kernel released it). An abandoned journal may then
  exist; recovery proceeds, operator-acknowledged (`change_recovery_started`).
- The lock file's PID/path/start-time fields are written for *diagnosis only*
  (logging, attribution). They are never read to decide ownership.

This satisfies the four required properties:
- PID reuse cannot make a dead owner appear live (no PID-based liveness).
- A live broker prevents a second broker from acquiring (kernel enforces).
- Broker termination releases the lock (kernel auto-release) **and** leaves the
  journal for recovery (journal is on disk, independent of the lock).
- Metadata tampering does not grant ownership (kernel lock is authoritative).

The per-platform process-authentication for the *operator-gateway RPC caller*
(§6.1.1) is a separate concern (authenticating a caller to a broker) and is not
used for lease ownership.

### 3.3 Lifecycle (lock acquisition order — deadlock-free)

```
1. capture initial primary hash (no lock)
2. create isolated workspace                      # snapshot/setup lock (short)
3. release snapshot/setup lock
4. perform builder + audit work OUTSIDE primary   # no primary lock held
5. acquire per-primary apply lease
6. revalidate primary hash
7. prepare durable transaction journal
8. append change_apply_started
9. apply / recover
10. verify final state
11. append final evidence
12. release apply lease
```

**Acquisition order:** snapshot/setup lock (step 2) is always released before
the apply lease (step 5) is taken — they never nest. The apply lease is the
sole long-held primary lock. Two runs targeting the same primary serialize on
the apply lease. **Concurrency caveat (review R2.4):** the workspace
provisioning lock (`WorkspaceManager`, `workspace.py:103-109`) is keyed by
`sha256(primary)`, so two runs against the same primary **serialize at
provisioning** (step 2), not just at apply — they cannot build/audit fully in
parallel; build/audit concurrency holds only across runs with *different*
primaries. The apply-lease ordering itself is deadlock-free because no run
holds the apply lease while requesting another lock.

---

## 4. Governance state root outside primary (Decision D3)

**No `tree_hash` ignore list in the first implementation.** All mutable TORQ
state lives under a configurable `governance_state_root` that is **provably
outside the primary tree**.

### 4.1 Contents

```
<governance_state_root>/
  evidence/           # receipt chains, artifacts
  workspaces/         # isolated builder sandboxes
  scratch/            # builder/adapter/analysis scratch per run (§8.1)
  apply-journals/     # write-ahead journals (§9)
  apply-backups/      # prior-content backups (§9.4)
  locks/              # ApplyLease records (§3.2)
  staging/            # per-file staging temp files (§4.3)
```

### 4.2 Containment fail-closed (at run creation)

Reject, with the listed finding, when any mutable TORQ state root is inside
the primary OR the primary is inside a mutable state directory (overlapping):
```
governance_state_inside_primary
workspace_inside_primary
evidence_inside_primary
journal_inside_primary
lock_root_inside_primary
staging_inside_primary
primary_inside_governance_state   # overlapping/unsafe
```
Containment is checked by **path containment** using `os.path.realpath(...,
strict=False)` (review 4-7 — `Path.resolve()` follows symlinks and can
mis-classify; `realpath` with explicit symlink rejection at run creation is
safer and matches the `st_nlink==1`/`S_ISREG` rigor applied to signing keys at
`receipts.py:791`): reject if either root resolves inside the other OR either
root is itself a symlink. **Not** by same-volume checks. **Default location:**
an OS-appropriate user data dir (`platformdirs.user_data_dir("torq-cli")`)
outside the repository — never under the working tree.

### 4.3 Filesystem staging (single-file temp)

The durable journal, backups, and workspace live **outside primary** (§4.1).
For an individual file replacement, a temp file may be created in the target
file's **parent directory** so `os.replace` is same-filesystem (atomic-rename
eligible). This is allowed **only**:
- after the apply lease is held (§3.2);
- after the primary hash was revalidated;
- with a reserved TORQ temp filename (e.g. `.torq-apply-<run_id>-<nonce>.tmp`);
- after the replacement bytes **and** old-state backup are durably recorded in
  the journal;
- with journaled cleanup and recovery behavior (§9).

This per-file temp does not pollute `tree_hash(primary)` for long: it is
created and renamed away (or cleaned up) within the leased apply window, and
the final tree hash (§9 step 12) is computed after all temps are resolved. If a
crash leaves a stray temp, recovery (§9.5) removes it before the final hash.
**Cross-run temp scoping (review R3.4):** cleanup matches only
`.torq-apply-<own_run_id>-<nonce>.tmp` — recovery never deletes another run's
temps (a maliciously-named temp cannot cause deletion of a concurrent run's
staging file). A prior crashed run's temps are cleaned only by that run's own
recovery (operator-acknowledged, §3.2.1).

**Terminology:** the multi-file operation is a **crash-consistent, journaled
change transaction** — not "atomic."

---

## 5. Workspace isolation lifecycle

Source: `safety/workspace.py` (reused), broker orchestrates.

```
absent → provisioning → ready → sealed → released → (purged)
                                 ↘ abandoned → recovered
```
- **Provision** (broker startup): `WorkspaceManager(state_root/workspaces).create(
  primary, run_id, dirty=False)` → `WorkspaceHandle` with `pinned_tree_hash`.
- **Ready:** builder/refiners write **only** under `handle.root` (enforced by
  `GuardedPaths(handle.root)` + the typed change channel §7). Primary untouched.
  Builder/adapter/analysis scratch is written to
  `<governance_state_root>/scratch/<run_id>/` (§8.1), never into the
  workspace — the sealed workspace is the exact intended resulting primary
  tree (E14).
- **Sealed** (`awaiting_approval`): workspace tree hash captured into the
  `ChangeSetManifest` (§8); G2A binds it (§8.3). No further workspace writes.
- **Released** on terminal outcome; copy retained for evidence until run seal,
  then purged.
- **Abandoned → recovered** (crash): lease is stale; recovery (§9.5) restores
  primary or proves completion, independent of the workspace.

Lock file (`WorkspaceManager`) stores `{run_id, pid, pinned_tree_hash,
created_at}` for crash diagnosis (PID advisory only — §10). **Note (review
R2.6):** today's `WorkspaceManager` lock (`workspace.py:103-109`) is created
**empty** — storing this metadata is new work in Phase A, not a description of
the current code. The `ApplyLease` (§3.2) is also new.

---

## 6. Actor identity and authorization

### 6.1 VerifiedActor — assertion stored as evidence, not a string

A bare string MUST NOT be treated as verified human identity. The full actor
assertion is an **encrypted evidence artifact**; receipts carry only bounded
machine fields and references.

```text
VerifiedActor (encrypted artifact)
  subject_id
  session_id
  authentication_method
  assurance_level
  roles
  provider
  issued_at
```
Receipts carry bounded fields:
```text
subject_id               # _OPAQUE_ID: [A-Za-z0-9][A-Za-z0-9:._-]{0,127}  (NOT free text)
assurance_level          # enum {none, local_unverified, verified}  (NOT a string)
actor_artifact           # _ARTIFACT_PATH — bounded relative path of the encrypted artifact (traversal rejected, hash-verified)
actor_artifact_hash      # sha256:...
authorization_policy     # _OPAQUE_ID (policy id, not free text)
authorization_result     # enum {allowed, denied}  (NOT a string)
```
- **`subject_id` is an opaque token from the IdP**, validated as `_OPAQUE_ID`
  (`run_evidence.py:64`: `[A-Za-z0-9][A-Za-z0-9:._-]{0,127}`) — **never** free
  text. This closes review 5-2 / proof 4: a `subject_id` carrying prose or
  control chars is rejected (`subject_id_not_opaque`). `assurance_level` and
  `authorization_result` are enums, not strings. The §2.3 work list is extended
  with explicit `VERIFIED_ACTOR_KEYS` (the actor fields above) and per-field
  validators; mutant **M43** (a `subject_id` carrying a sentence) must be killed.
- **RPC-caller process authentication (distinct from lease ownership):** the
  broker authenticates the *calling process* of the operator-gateway RPC — not
  the human — via kernel peer-credential mechanisms on the RPC socket
  (`SO_PEERCRED`/`LOCAL_PEERCRED`/`GetNamedPipeClientProcessId`). The calling
  process's identity is bound to the run at first RPC; a string is never
  accepted as a process identity. The human actor comes only from the encrypted
  `VerifiedActor` artifact. **This is separate from §3.2.1** (which governs
  lease ownership via a kernel-held lock, not PID probing).
- The current `resolver_identity` interface becomes a **compatibility adapter**
  producing `local_unverified` (subject_id=`operator:local-session`,
  assurance_level=`local_unverified`, method=`none`). In v3, `resolver_identity`
  is **removed** from `ACTION_RESOLVED_KEYS` and replaced by `subject_id` +
  `assurance_level` (always present, never defaulted) — so unverified status is
  machine-readable and cannot be confused with the legacy field.
- `local_unverified` may be allowed **only by an explicit development policy**
  (`allow_local_unverified: true`); see §6.2 for the default-deny rule.
- **Authorization failure resolves nothing and applies nothing** — the action
  stays open; no `change_apply_started`. (Terminal handling of persistent denial:
  §6.2.)

### 6.2 Authorization policy

`ApprovalPolicy.can_approve(actor, change_manifest_hash, policy_version) ->
AuthorizationResult{allowed: bool, reason: _OPAQUE_ID}`. Enforced at two points
and **bound to the same tuple** (reviews 5-4): (a) the operator-gateway approval
RPC before `action_resolved(approved)` is recorded, and (b) the broker before
`change_apply_started` — the broker **re-runs** `can_approve(actor,
change_manifest_hash, policy_version)` and aborts unless the result is `allowed` AND
`policy_version` equals the immutable value fixed in `GovernedRunContext` (§3.1).
Because `policy_version` is pinned at broker startup and the manifest hash is
load-by-generation (§8.3), the two checks see identical inputs; a role revoked
between approval and apply is caught at (b), and a policy bump cannot silently
invalidate a valid approval.

**Default-deny (review 5-3):** the default production `ApprovalPolicy`
**refuses** any actor with `assurance_level < verified` (matching the codebase's
fail-closed default pattern, e.g. `workspace.py:99` `dirty_policy="refuse"`).
`allow_local_unverified: true` is an explicit, logged, non-default development
knob. E3 is testable as "default policy rejects `local_unverified`."

**Pre-apply authorization denial (Review 7, Finding 8):** a denied
authorization at the broker gate **before** `change_apply_started` is NOT an
apply failure — no journal, backup, `change_apply_started`, or recovery receipt
is emitted, because the filesystem transaction never began. Instead:
```
action_resolved(approved)
  → broker authorization recheck denied
  → approval_invalidated                          # §2.1 transition
  → run_decision(blocked|failed, per policy)
  → seal
```
**Exact closed allowlist (`APPROVAL_INVALIDATED_KEYS`); all fields
mandatory:**
```
APPROVAL_INVALIDATED_KEYS = {
  action_id,                   # _OPAQUE_ID
  change_set_id,               # _OPAQUE_ID
  change_manifest_generation,  # bounded positive integer
  change_manifest_hash,        # sha256:… of the denied generation
  subject_id,                  # _OPAQUE_ID (§6.1)
  policy_version,              # _OPAQUE_ID
  reason,                      # bounded machine reason code (enum, NOT prose)
  provider_dispatch,           # bool — MUST be false
}
```
Rules: permitted **only after** `action_resolved(approved)`; **forbidden
after** `change_apply_started` (machine-enforced precondition, §2.3); followed
by terminal `run_decision(blocked)` or `run_decision(failed)` according to the
selected policy; emits **no** journal, backup, apply-failure, or recovery
evidence. `reason` validates as a bounded machine reason code enum — prose is
rejected. The four Review-7 properties hold: PID reuse cannot deadlock it
(kernel lock not yet acquired at the denial point); it produces no false
recovery evidence; it cannot be confused with a `change_apply_failed` that did
begin the FS transaction; and it reaches a terminal `run_decision` (no stuck
state).

**Persistent-denial after apply has begun** (review 5-5): if authorization is
somehow denied after `change_apply_started` (defense-in-depth failure), the run
takes the §9.5 apply-failure/recovery path → `run_decision(failed)`. Every run
reaches a sealed decision; no unbounded `awaiting_approval`.

---

## 7. Typed change-artifact channel (no base64 in visible text)

The builder's **visible JSON response remains bounded metadata** and references
— it does **not** carry source bytes or base64:
```text
status
change_bundle_id
change_bundle_hash
change_bundle_schema_version
```
Actual file bytes arrive through a **separate typed change-artifact channel**
owned by the provider adapter / tool protocol. Requirements:
- **raw bytes are hashed before encryption**;
- **redaction must never rewrite bytes after hashing** — redaction operates only
  on bounded visible text; the bytes channel is redaction-free, with its own
  integrity (the content hash);
- sensitive-content policy may **reject, quarantine, or flag** a change bundle,
  but may **not silently mutate** the proposed source;
- bundle artifacts are **encrypted**;
- the canonical manifest references content artifacts and hashes (§8);
- **adapters without the typed artifact channel fail closed** for governed
  mutation;
- **no base64-in-visible-text fallback.**

Provider support may be phased, but the **security contract is complete** from
day one. This closes the review finding that redaction over base64 would
corrupt decoded bytes and invalidate the pinned content hashes.

---

## 8. Canonical manifest + audit binding

### 8.1 ChangeSetManifest (versioned, explicit operations) — Review 7, Finding 4 & 7

The receipt-store's `_manifest_generation` (which advances as the signed
*evidence* manifest is rewritten) is **not** the change-set generation. A
separate, bounded change-set identity is introduced:

```text
change_set_id              # immutable per proposal lineage
change_manifest_generation # begins at 1; advances ONLY when builder/repair
                           # output changes the proposed change set
change_manifest_hash       # hash of this generation's ChangeSetManifest
```

Rules (Review 7, Finding 4):
- `change_set_id` is immutable per proposal lineage.
- `change_manifest_generation` begins at 1 and advances only when builder/repair
  output changes the proposed change set.
- **Receipt-store manifest generation (`_manifest_generation`) must NEVER select
  the approved change manifest.** The two generation counters are distinct.
- A governed `change_proposed` receipt binds the change-set ID, generation,
  artifact path, and artifact hash (exact closed schema
  `CHANGE_PROPOSED_KEYS`, §2.5).
- The final G2A receipt references that exact tuple.
- `action_opened` references that exact tuple.
- Broker application loads the artifact named by authenticated evidence and
  recomputes its hash.
- A repaired proposal creates a new change-manifest generation and invalidates
  any approval target for the earlier generation.

```text
ChangeSetManifest
  schema_version            # manifest schema "1.0.0" (distinct from receipt schema_version)
  run_id
  change_set_id             # Review 7, Finding 4
  change_manifest_generation
  primary_tree_hash         # pinned, captured before any write
  workspace_tree_hash       # at seal
  result_tree_hash          # Review 7, Finding 7 — expected post-apply primary hash
  operations[]              # canonical: sorted by path
  created_at
  change_manifest_hash      # domain-separated hash of this manifest (§8.2)
```
**`result_tree_hash` (Review 7, Finding 7; v1 invariant, reconciliation
revision):** the sealed workspace IS the exact intended resulting primary
tree. Therefore, in v1:

```text
result_tree_hash == workspace_tree_hash
```

There is **no workspace-exclusion list** in v1 — builder, adapter, and
analysis scratch MUST live outside the sealed workspace, under
`<governance_state_root>/scratch/<run_id>/` (§4.1), so nothing ever needs to
be "hidden" from the derivation. Scratch inside the sealed workspace is
rejected at audit (`workspace_scratch_at_seal`). Manifest derivation is
concrete:

1. Workspace creation produces an exact copy of the pinned primary.
2. Builder/refiner tools modify only the workspace.
3. Scratch is written outside the workspace
   (`<governance_state_root>/scratch/<run_id>/`).
4. At seal, `operations[]` are derived by comparing the initial workspace
   inventory with the final workspace inventory.
5. `workspace_tree_hash` is computed from the final workspace.
6. `result_tree_hash = workspace_tree_hash`.
7. G2A audits the operation manifest and the sealed workspace hash (§8.3).
8. After application, the recomputed primary hash must equal both hashes
   (§9.2 step 13).

A hash alone contains no inventory — the tree is never "reconstructed from
`primary_tree_hash`". The operation manifest comes from the workspace
inventory diff, and post-apply success is proven by recomputing the primary
tree hash and comparing it to `result_tree_hash`.

**Initial operations:** `create`, `update`, `delete`. **Defer `rename` and
mode changes** until cross-platform semantics are fully defined — unsupported
operations fail closed.
Each operation:
```text
operation                 # create|update|delete
path                      # UTF-8 forward-slash relative
prior_content_hash        # per §8.2 table
result_content_hash       # per §8.2 table
content_artifact          # when applicable (_ARTIFACT_PATH — bounded relative
                          # path of the encrypted artifact, hash-verified)
content_size
```

### 8.2 Path and operation invariants (enforced at manifest build AND at apply)

- UTF-8 forward-slash relative paths; **no absolute paths**; **no `..`**; **no
  empty path components**; **no trailing dots or spaces**.
- **No Windows device names (review 4-2):** inherit the authoritative set from
  `receipts.py:61-65` — `{con, prn, aux, nul, clock$} ∪ {com1..com9} ∪
  {lpt1..lpt9}` — with the **extension rule**: strip the final `.`-suffix, then
  casefold-compare the stem to the device set (so `CON.txt`, `NUL.log` are
  rejected). (The earlier "CON, PRS, NUL" was a typo for PRN and incomplete.)
- **Reject Unicode-normalization collisions** — NFC-normalize every path.
- **Reject case-fold collisions unconditionally (Review 7, Finding 5):** across
  the entire change manifest, case-fold-compare normalized paths and reject any
  collision. **No write-based case probing inside the protected primary tree**
  — the earlier per-directory probe (create-a-file-and-stat) mutated primary
  and was non-portable. The unconditional rule may over-reject legitimately
  distinct `Foo.py` and `foo.py` on a case-sensitive filesystem, but it is
  deterministic, portable, and safe. Add a test proving path validation
  performs **no primary mutation**. Case-distinct path support is recorded as a
  **future feature** requiring a non-mutating filesystem-capability
  implementation.
- **Reject duplicate operation targets.**
- **Symlink creation/modification denied in v1** (operations are regular-file
  bytes only) — AND enforced at apply per-target via the handle-relative,
  `dir_fd`-bound model (§9.3; review 4-5 superseded by Review 7 / 7-6:
  final-component `O_NOFOLLOW` alone does not protect pathname replacement).
- **Protected paths denied** (`GuardedPaths._PROTECTED_NAMES` + `.env.*`).
- Canonical JSON serialization; **domain-separated hashing (review 4-1, E9) —
  concrete construction:** `change_manifest_hash = "sha256:" + sha256(
  b"torq-changeset-manifest-v1\0" ‖ canonical_json(manifest_without_hash)
  ).hexdigest()`. The tag `torq-changeset-manifest-v1` is distinct from the
  artifact-content hash tag (`torq-artifact-v1`) and the receipt hash domain,
  so a manifest hash cannot collide with an artifact hash or a different schema
  version. `operation` boundaries are part of the canonical JSON itself (sorted
  operations array), so they are covered by the digest. Mutant **M37** is
  defined against this exact tag.
- **Per-operation hash requirements (review 4-4):**

  | operation | `prior_content_hash` | `result_content_hash` | rollback action |
  |---|---|---|---|
  | `create` | forbidden (must not exist) | **required** (hash of new bytes) | **delete** the created file |
  | `update` | **required** (must match on-disk) | **required** (new bytes) | restore backup bytes |
  | `delete` | **required** (must match on-disk) | **forbidden** (no result bytes) | restore backup bytes |

  "When applicable" is replaced by this table; an `update` omitting
  `result_content_hash` or a `delete` carrying one is rejected
  (`operation_hash_fields_invalid`). Mutant **M41** (a `delete` carrying a
  `result_content_hash`) must be killed.

### 8.3 Audit binding (final successful G2A binds the exact approved manifest)

The final successful G2A audit binds:
```text
change_set_id              # Review 7, Finding 4
change_manifest_generation
change_manifest_hash
change_manifest_artifact_hash
workspace_tree_hash
result_tree_hash           # Review 7, Finding 7
g2a_receipt_hash
g2a_attempt_id
repair_cycle
```
**Repair invalidates the manifest.** When a repair changes the workspace or
manifest:
1. invalidate the prior manifest;
2. derive a new manifest;
3. write a new encrypted manifest artifact;
4. run a new G2A audit;
5. open approval **only** for that final successful audit.

An approval target MUST include the exact final manifest and audit hashes. The
operator-facing approval summary displays the same manifest identifier the
broker later loads — so the operator approves what the broker applies. This
closes the "repaired proposal reuses an older approval" attack.

**Manifest-swap defense (Review 7, Finding 4; supersedes review 4-6):** the
approval assertion artifact embeds `(change_set_id, change_manifest_generation,
change_manifest_hash)` — the change-set identity, NOT the receipt-store
`_manifest_generation`. The broker loads the change-manifest artifact named by
the `change_proposed` receipt for that exact `(change_set_id,
change_manifest_generation)` tuple and **aborts** if its recomputed hash ≠ the
approved `change_manifest_hash`. The receipt-store `_manifest_generation` is
**never** used to select the approved change manifest (Finding 4). A repaired
proposal creates a new `change_manifest_generation` under the same
`change_set_id` and invalidates approval for the earlier generation.
**Broker-side latest-generation enforcement (re-review Vector 4):** at apply,
the broker rejects any approval whose `change_manifest_generation` is not the
maximum generation recorded in `change_proposed` receipts for that
`change_set_id` — closing the repair-after-approval race (gen-1 approved, then
repair creates gen-2): a stale-but-internally-consistent gen-1 artifact cannot
be applied because its generation < max. Mutant **M38** covers reduction
tamper; mutant **M44** (updated) covers loading the wrong `(change_set_id,
change_manifest_generation)` tuple or confusing it with `_manifest_generation`.

---

## 9. Crash-consistent apply + recovery

### 9.1 Receipt vs journal authority

- **Receipts** prove governed decisions and authenticated lifecycle events.
- **The journal** proves low-level filesystem progress.
- **Final success requires agreement among:** approved manifest + final audit
  binding + receipts + journal + recomputed primary tree. **Neither a receipt
  alone nor a journal alone proves successful application.**

### 9.2 Ordering (17 steps)

1. Acquire the per-primary apply lease (§3.2).
2. Verify no incompatible active or abandoned journal exists (§9.5).
3. Recompute and compare the primary tree hash to `primary_tree_hash`.
4. Load and verify the approved manifest, G2A binding, and actor authorization.
5. Validate every path and operation (§8.2) before modifying any target.
6. Materialize and hash-check all replacement content (typed channel §7).
7. Capture prior metadata and durable backups (`<governance_state_root>/apply-backups/`).
8. Write and fsync a prepared transaction journal (`apply-journals/<run_id>`).
9. Append `change_apply_started` with the journal hash.
10. Perform operations one at a time, with journal progress updates after each.
11. Flush file and directory metadata where supported (fsync files + parent dirs).
12. Recompute the resulting primary tree hash (after stray-temp cleanup, §4.3).
13. Compare to the manifest's `result_tree_hash` (Review 7, Finding 7). Mismatch
    → rollback (§9.5) → `change_apply_failed` → recovery → `run_decision(failed)`.
14. Append `change_applied` (NON-terminal — verified-success prerequisite).
15. Append `run_decision(decision=completed)` — the ONLY terminal transition
    (Review 7, Finding 3) — then seal.
16. Mark/archive the journal as committed.
17. Release the apply lease.

### 9.3 Single-file replacement (within §9.2 step 10) — path-race model (Review 7, Finding 6)

**⚠ The earlier claim that `O_NOFOLLOW` on the target makes a later
pathname-based `os.replace` safe is RETRACTED (Finding 6).** The opened
descriptor is not used by `os.replace`, and parent path components may change
after validation. The design now uses a handle/directory-fd-relative model:

**POSIX (preferred, Review 7 Finding 6):**
1. **Open the primary root directory once** (`open(primary, O_DIRECTORY|O_NOFOLLOW)`).
2. **Traverse to each target's parent with handle-relative operations** using
   `O_DIRECTORY | O_NOFOLLOW` at each component (`openat`, `fstatat`,
   `O_NOFOLLOW` on every component denies symlink traversal mid-path).
3. Use **`dir_fd`-relative `open`, `unlink`, and rename** (`renameat2`/
   `os.rename` with `src_dir_fd`/`dst_dir_fd`) where Python and the platform
   support them, so operations are bound to the validated directory handle, not
   a re-resolved pathname.
4. **Revalidate directory identity before and after each operation** (`fstat`
   the dir fd; compare `st_dev`/`st_ino`).

**Windows:**
- Handle-based directory and target validation using reparse-point-safe flags
  (`FILE_FLAG_BACKUP_SEMANTICS` for dirs, `FILE_FLAG_OPEN_REPARSE_POINT` to
  detect—not follow—reparse points), then a replacement API whose destination is
  bound to the validated handle model (`ReplaceFile`/`MoveFileEx` with the
  validated handle-derived path).

**Honest residual (Finding 6):** where the standard library cannot provide
equivalent protection (e.g. older Python without `dir_fd` support, or a
filesystem that does not provide `renameat2`), the design states the residual
threat honestly and **fails closed under a policy requiring strong path-race
resistance** (`strong_path_race_required`). It does **not** claim the
final-component `O_NOFOLLOW` check eliminates malicious concurrent
parent-directory replacement. A test (or explicit platform limitation) covers
parent-directory swap attacks, not only a final-target symlink.

**Within that model**, for each `update`/`create`:
1. Resolve target's parent via the handle-relative traversal above; verify the
   target with `fstatat(parent_fd, target, …, AT_SYMLINK_NOFOLLOW)` (re-review
   Vector 6 — use the validated `parent_fd`, NOT `AT_FDCWD`, so verification and
   the rename share the same validated handle and a symlink planted at the
   target is caught) as a regular file `st_nlink==1` immediately before the
   rename.
2. Write reserved temp (`.torq-apply-<run_id>-<nonce>.tmp`) in the validated
   parent dir (§4.3), with `O_NOFOLLOW|O_CREAT|O_EXCL` (`openat`).
3. **Write-ahead ordering (review R3.1):** record `{op, path, sha256, sequence}`
   in the journal **and fsync the journal BEFORE** the rename. Recovery
   discriminator: "journal precedes rename."
4. fsync temp + parent (reuse `_fsync_directory`, `receipts.py:151-194`, with
   its Windows error-code tolerance 1/5/87 — review R3.3; do NOT reimplement).
5. `renameat(temp_fd, target_fd)` (or `os.rename` with `*_dir_fd`).
6. fsync target + parent (again via `_fsync_directory`).

For a `delete`: handle-relative `fstatat` verify, backup bytes (§9.4), journal
write-ahead (`{op:"delete", path, prior_sha256, sequence}`), fsync journal,
`unlinkat(target)`, fsync parent. Rollback restores bytes for `update`/`delete`
and `unlink`s for `create` (per the §8.2 table).

### 9.4 Backups (review R3.5)

At journal prepare (step 7), per the §8.2 table:
- `update`/`delete` (path exists in primary): read+hash current bytes (via
  `O_NOFOLLOW`), copy to `apply-backups/<run_id>/<path>` (outside primary),
  fsync. `prior_content_hash` MUST match the on-disk hash or the op fails closed
  (`prior_hash_mismatch`).
- `create` (path must NOT exist): no backup; rollback is `unlink`.
Backups are used by rollback (§9.5) and are themselves journaled
(`{op:"backup", path, prior_sha256}`).

### 9.5 Crash recovery at every boundary

Recovery reconciles **journal with authenticated receipts** — it trusts
neither alone. Boundaries and behavior:

- **After journal prepare, before `change_apply_started`** (between steps 8–9):
  no primary writes occurred. Delete the prepared journal; primary ==
  `primary_tree_hash`. Run → `change_recovery_completed` → `run_decision(failed)`.
- **After `change_apply_started`, before first write** (9–10): no writes yet;
  same as above.
- **Between file operations** (mid-10): journal records which ops completed
  **before** each rename (write-ahead, §9.3 step 3 — review R3.1's discriminator
  is "journal precedes rename"). Replay is **idempotent per operation**: re-check
  the on-disk hash against `result_content_hash` (for `create`/`update`) or
  absence (for `delete`); skip if already matching. Because the journal precedes
  the rename, an op recorded-but-not-yet-applied is detected by hash and applied
  exactly once; an op not-recorded was never started. No "ambiguous partial"
  branch is needed.
- **After all writes, before final tree verification** (11–12): all bytes
  written; recompute hash; if it matches expected, proceed to `change_applied`;
  if not, roll back from backups.
- **After `change_applied`, before seal** (14–15): the apply succeeded; the
  journal proves it. Recovery verifies `tree_hash(primary) ==
  change_applied.post_tree_hash` and re-emits the terminal decision. This
  boundary's correctness **depends on the existing manifest-anchor machinery**
  (`_write_manifest(sealed=True)` + `_recover_authenticated_uncovered_tail`,
  `receipts.py:1115-1166,1168-1279,1647-1659` — review R3.2/R3.8): a crash
  during `seal`'s `_write_manifest` leaves the store in
  `manifest_anchor_update_pending`, which the existing resume path handles by
  re-writing the anchor. The design does **not** re-implement this; it inherits
  it. E4's "at most one `change_applied`" is enforced by the v3 lifecycle
  validator (§2.3) **before** seal — recovery re-emits the terminal
  `run_decision(completed)`, **never** a second `change_applied`.
- **After seal, before journal archival** (15–16): run is sealed `completed`;
  archive the journal on recovery entry.

**Double-apply prevention (E4):** every operation is keyed by
`(change_manifest_hash, path)` and idempotent — re-applying an already-applied op is a
no-op because the `result_content_hash` already matches. A second
`change_applied` for the same run is forbidden by the lifecycle validator.

---

## 10. Cross-platform behavior

- `tree_hash`, `GuardedPaths`, `os.replace`, `os.open(O_EXCL)`, `fsync`: stdlib,
  work on Windows/macOS/Linux.
- `os.replace` atomic only same-filesystem → the per-file temp lives in the
  target's parent dir (§4.3), constrained to the lease window.
- fsync on Windows flushes via `FlushFileBuffers`; reuse the existing
  `O_BINARY` fidelity (covered by mutant M23); a new mutant covers the apply
  fsync (§11).
- File modes: advisory on Windows, enforced on POSIX (existing pattern).
- PID/path/start-time in the lease file: **diagnostic only** — never used to
  decide ownership or process liveness (PID reuse). Stale-owner detection uses
  the **kernel-held per-primary lock** (§3.2.1): "the lock is held" is the sole
  liveness authority. The operator-gateway RPC-caller peer-PID channel (§6.1)
  authenticates a caller *to one broker* and is **never** used for lease
  ownership.
- Path separators: manifest paths POSIX; resolved via `PurePath.as_posix()`.

---

## 11. Test and named-mutant plan

### 11.1 Tests (extend `tests/test_phase4_orchestrator.py`, `test_phase4_safety.py`, `test_run_evidence*.py`)
- `test_governed_run_applies_audited_manifest_on_approval` — drive to approval,
  resolve, assert ops applied under pinned hash; broker apply.
- `test_approval_recorded_without_application_is_not_completed` (E0) — approve
  then crash before `change_apply_started`; assert run is **not** `completed`,
  no `change_applied`.
- `test_completion_requires_change_applied_referencing_approved_manifest` (E2).
- `test_partial_application_crash_rolls_back_to_pinned_hash` (E5).
- `test_two_concurrent_runs_one_primary_serialize_on_apply_lease` (E4).
- `test_string_identity_not_treated_as_verified` — `local_unverified` rejected
  by production policy.
- `test_repaired_proposal_cannot_reuse_older_approval` — manifest hash mismatch
  → approval invalid.
- `test_governance_state_inside_primary_fails_closed` (§4.2).
- `test_stray_apply_temp_cleaned_before_final_hash` (§4.3).
- `test_double_apply_is_idempotent_per_operation` (E4 double-apply).
- `test_change_proposed_precedes_action_opened_and_binds_exact_generation` (§2.5).
- `test_repair_emits_new_change_proposed_with_incremented_generation` (§2.5).
- `test_approval_invalidated_forbidden_after_change_apply_started` (§6.2, M46).
- `test_approval_invalidated_emits_no_journal_backup_or_recovery_evidence` (§6.2).
- `test_governed_receipts_reject_provider_dispatch_true` (E13, M47).
- `test_builder_scratch_outside_sealed_workspace_and_result_equals_workspace`
  (E14, §8.1) — scratch lands in `<governance_state_root>/scratch/<run_id>/`,
  a workspace containing scratch at seal is rejected, and
  `result_tree_hash == workspace_tree_hash`.
- Schema-v2 back-compat: `test_v2_chain_verifies_under_v2_rules`, `test_v2_chain_cannot_be_extended_with_v3_transitions`.

### 11.2 Named mutants (extend M31+)
- **M31** drop `approved_by`/authorization check → killed by approval test.
- **M32** skip G2A-binding hash compare → killed by tampered-manifest test.
- **M33** allow `completed` without `change_applied` → killed by E0/E2 test.
- **M34** remove post-write fsync → killed by durability/crash test.
- **M35** disable protected-path check at replace → killed by protected-path test.
- **M36** allow a second `change_applied` → killed by idempotency test.
- **M37** diverge manifest hash from content hashes → killed by divergence test.
- **M38** (audit-binding reduction) alter the field-subset reduction used to
  recompute the bound manifest hash → killed by reduction-tamper test (closes
  the review's M1 finding).
- **M39** allow base64-in-visible-text builder channel → killed by channel-type test.
- **M40** allow mutable state inside primary → killed by containment test.
- **M41** allow a `delete` op carrying `result_content_hash` → killed by the
  per-op hash-requirement test (§8.2 table).
- **M42** plant a symlink at the apply target between revalidation and replace
  → killed by the handle-relative apply test (`fstatat(parent_fd, …,
  AT_SYMLINK_NOFOLLOW)` + parent-directory swap attack, §9.3).
- **M43** a `subject_id` carrying prose/control chars accepted → killed by the
  opaque-token validator test (§6.1, proof 4).
- **M44** broker loads the wrong manifest generation on recovery → killed by the
  generation-swap test (§8.3).
- **M45** accept `change_proposed` with a non-monotonic
  `change_manifest_generation`, or `action_opened` without a preceding
  `change_proposed` for the exact tuple → killed by the §2.5
  generation-ordering tests.
- **M46** accept `approval_invalidated` after `change_apply_started`, or emit
  journal/backup/recovery evidence for it → killed by the §6.2 precondition
  and no-recovery-evidence tests.
- **M47** accept any governed lifecycle receipt with `provider_dispatch=true`
  → killed by the provider-dispatch sentinel test (E13).

---

## 12. Phased migration (Review 7, Finding 9 — dark launch)

**Phase A — disabled plumbing (dark).** Add `governance_state_root` + run-context
plumbing (`primary`, `GovernedRunContext`), the typed change-artifact channel,
and manifest derivation **behind a disabled capability flag**. **Preserve current
schema-v2 behavior unchanged.** **Do not expose or accept schema-v3 approval** —
no user can create a half-supported v3 run. The v2 `approved→completed` mapping
stays in place (do NOT remove it while no apply engine exists, Finding 9). No
`change_proposed`/v3 transitions are emitted.

**Phase B — complete schema-v3 transaction.** Land schema-v3 lifecycle, broker
apply, the **kernel-held per-primary lock** (§3.2), journal, and recovery
**together** as one end-to-end unit. **Capability gate (concrete predicate,
re-review Vector 9):** v3 is enabled only when this predicate is true,
evaluated at run creation *and* re-checked at `change_apply_started`:
```text
v3_enabled =
    kernel_lock_available              # §3.2 fcntl/flock/LockFileEx adapter present
  AND journal_recovery_implemented     # §9 write-ahead journal + recovery
  AND verified_actor_provider_present  # minimal signed local credential (below)
  AND dir_fd_relative_apply_supported  # §9.3 *at/dir_fd apply on this platform
  AND v3_lifecycle_validator_present   # validate_v3_receipt_contract (§2.3)
```
Enforcement: v3 chain stamping and every v3 transition are **unreachable**
unless `v3_enabled` is true (the `applier` writer role is not certified, the v3
`TransitionRule` rows are inert, and the schema-3.0.0 dispatch path refuses).
If any component is missing, the run fails closed
(`v3_end_to_end_gate_not_satisfied`) and falls back to v2 behavior. v3 is off
by default. `completed` is gated on `change_applied`. **Minimal verified actor
(Finding 9):** include a **minimal signed local/operator credential provider**
in Phase B so production policy can require `assurance_level >= verified` —
without it, v3 apply is development-only. Mutants M31–M44. Keep v2 verification
read-only. **Do not permit new v2 governed mutation runs once v3 is
production-ready**, unless explicitly supported as legacy behavior.

**Phase C — production identity & hardening.** Full IdP (OIDC/SAML) per SPEC §7
Step 3, replacing the Phase-B minimal local credential provider. Dirty-primary
policy; workspace purge cadence; additional provider adapters for the typed
channel.

**Consequence made explicit (Finding 9):** until a verified actor provider
exists, v3 apply is development-only. Phase B includes the minimal signed
local/operator credential provider so this is not "development-only forever";
OIDC/SAML remains Phase C.

---

## 13. Security invariants (testable)

- **E0** approval ≠ application (`completed` ⇔ valid `change_applied`).
- **E1** G2A audit bound to the exact approved manifest (+artifact+workspace+g2a hashes).
- **E2** `completed` ⇔ `change_applied` references the approved manifest.
- **E3** no unauthenticated actor (`local_unverified` masquerading as verified).
- **E4** at most one `change_applied` per run; double-apply idempotent per op.
- **E5** recovery yields only `primary_tree_hash`-or-`change_applied.post_tree_hash`.
- **E6** builder/refiner never write to primary (workspace-only, outside primary).
- **E7** protected paths/symlinks enforced at build, stage, and replace.
- **E8** apply is crash-consistent via journal + fsync + leased same-volume rename.
- **E9** manifest hash domain-separated from content/artifact hashes.
- **E10** mutable TORQ state never inside primary (containment fail-closed).
- **E11** receipts + journal + recomputed tree must all agree for `completed`.
- **E12** `change_proposed` precedes G2A audit and `action_opened`; repair
  increments `change_manifest_generation` under an immutable `change_set_id`;
  generations are monotonic from 1 (§2.5).
- **E13** no governed lifecycle receipt carries `provider_dispatch=true`
  (the provider never receives an apply capability, §3).
- **E14** `result_tree_hash == workspace_tree_hash` in v1; no scratch inside
  the sealed workspace — builder/adapter/analysis scratch lives in
  `<governance_state_root>/scratch/<run_id>/` (§8.1).

---

## 14. SPEC ↔ code contradictions to record (separate spec-edit PR)

This design implies spec/code edits to be made in a **separate review-owned
PR**, not folded into implementation:
- **SPEC §1.1** "primary worktree is **not** written to during a governed run"
  and "no `sandbox`/`worktree`/`copy_tree` symbols in `src/`" become false
  post-Phase-B (more precise than the SECURITY.md item).
- **SPEC §7 Step 1** "isolated worktree/copy sandbox … is a build target"
  becomes implemented (with scope: copy sandbox under governance state root,
  not a git worktree).
- **`orchestrator.py:1272-1276`** `outcome_map {"approved":"completed"}` is
  replaced by §2.2.
- **`SECURITY.md`** "isolated worktree" claim becomes true post-Phase-B with
  precise scope.
- **Receipt schema** `_RECEIPT_SCHEMA_VERSION` 2.0.0 → 3.0.0; `STAGE_RESPONSE_CONTRACT_VERSION` bumps for the typed channel reference;
  `outcome_map`/`decision` enumerations widen.

---

## 15. Independent review findings and dispositions (historical record)

Six independent reviews ran against this document and the `72e8693` code. Every
finding is recorded with severity · evidence · disposition · section changed ·
remaining risk. **This section is a historical record of each review round:
dispositions describe the document as of that round, and several are
superseded by Review 7 (§17), the independent re-review (§18), or the
reconciliation closed-schema pass (§2.5/§2.6). Superseded rows carry an
explicit note; they must not be read as the normative design, which is
§1–§14.** The two initially-blocking facets — (a) the "kernel-authenticated
peer-PID channel" being a placeholder and (b) `subject_id` being unconstrained
(proof 4) — are resolved in §3.2.1, §3.1.1, and §6.1.

### Review 1 — Receipt schema v3 and lifecycle
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| R1.1 | High | writer_role for apply/recovery transitions unspecified | Resolved: §2.3 states a new `applier` role is required (not conditional), with certificate bump (reconciliation revision: `applier` is a writer role; the broker is a process boundary, not a receipt authority identity — §2.6) | §2.3, §2.6 | Implementer must pin one role per transition at build time |
| R1.2 | **Critical** | non-terminal approval requires rewriting `run_evidence.py:1149-1168` operator-gateway terminal logic | Resolved (updated by re-review Vector 3): §2.3 names the exact code path + the **three** coupled changes (outcome_map removal + terminal-decision rewrite + `run_evidence.py:1156-1157` outcome-compare rework) and pins the `applier` writer_role | §2.3 | Load-bearing; if missed, approval still collapses to terminal |
| R1.3 | High | `receipt_schema_version` field invented, not in code | Resolved — (superseded by Review 7 / 7-2): **no parallel field**; the existing `schema_version` is authoritative (§2) | §2.3 | None (field abolished) |
| R1.4 | High | v3 dispatch unspecified | Resolved: §2.3 specifies the `verify_receipt_store` branch, portable verifier path, v2/v3 coexistence by stamping | §2.3 | None |
| R1.6 | High | all 7 new transitions lack payload whitelists | Resolved: §2.3 lists exact `*_KEYS` frozensets for every new transition + validators (completed by the reconciliation revision §2.5/§2.6: `CHANGE_PROPOSED_KEYS` + `APPROVAL_INVALIDATED_KEYS` added; orphaned `MANIFEST_SEALED_KEYS` removed; stale generic manifest field names normalized) | §2.3, §2.5, §2.6 | None |
| R1.7 | Medium | `change_recovery_required` terminality rule unspecified | Resolved: §2.3 terminal-state validator bullet | §2.3 | None |

### Review 2 — Broker/process ownership and per-primary locking
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| R2.1 | **Critical** | broker has no `primary` parameter today | Resolved: §3.1.1 specifies the concrete `RunController`/`EvidenceBrokerProcess.start`/`_broker_process_main` signature changes | §3.1.1 | Phase-A plumbing; primary must enter at `_handle_run` only |
| R2.2 | High | lease shared only if `governance_state_root` shared across runs | Resolved: §4.2 default is a single shared user-data dir; cross-root exclusion is documented as a precondition | §3.2, §4.2 | Multi-user hosts with per-user roots need a shared lock location |
| R2.3 | **Critical** | "peer-PID channel" cannot do cross-broker liveness | Resolved (superseded by Review 7 / 7-1): §3.2.1 — the kernel-held per-primary lock (`fcntl`/`flock`/`LockFileEx`) is the sole liveness authority; the PID-probe model is removed. RPC-caller peer-PID (§6.1) authenticates a caller to a broker, never lease ownership. | §3.2.1 | None |
| R2.4 | Medium | "build/audit concurrently" overstated | Resolved: §3.3 concurrency caveat — serialization at provisioning is keyed by primary | §3.3 | None |
| R2.6 | Low | §5 over-states current `WorkspaceManager` lock metadata | Resolved: §5 note clarifies it is new Phase-A work | §5 | None |

### Review 3 — Filesystem crash consistency and recovery
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| R3.1 | High | mid-fsync idempotency had no discriminator | Resolved: §9.3 step 3 — journal written + fsynced BEFORE `os.replace` (write-ahead); §9.5 between-ops branch uses hash-recheck | §9.3, §9.5 | None |
| R3.2 | High | recovery-at-seal depends on undocumented manifest-anchor machinery | Resolved: §9.5 cites `_write_manifest`/`_recover_authenticated_uncovered_tail`/`manifest_anchor_update_pending` explicitly | §9.5 | None |
| R3.3 | Medium | Windows dir-fsync reuse unspecified | Resolved: §9.3 mandates reusing `_fsync_directory` (`receipts.py:151-194`) with its error tolerance | §9.3 | None |
| R3.4 | Medium | cross-run stray-temp cleanup unspecified | Resolved: §4.3 — cleanup scoped to own `<run_id>`; never deletes another run's temps | §4.3 | None |
| R3.5 | High | `create`/`delete` rollback unspecified | Resolved: §9.4 + §8.2 per-op table (create→unlink, delete/update→restore bytes) | §8.2, §9.4 | None |
| R3.6 | Medium | E4 second-`change_applied` enforcement | Resolved: §9.5 + §2.3 lifecycle validator before seal | §9.5, §2.3 | None |
| R3.8 | Medium | existing recovery is receipt-only; journal-aware recovery is greenfield | Resolved: §9.5 states journal-aware recovery is new and distinct from tail-recovery | §9.1, §9.5 | None |

### Review 4 — Manifest / path / cross-platform security
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| 4-1 | High | domain-separation hand-waved | Resolved: §8.2 gives the exact tag construction `torq-changeset-manifest-v1\0…` | §8.2 | M37 pins it |
| 4-2 | High | device-name list incomplete/typo | Resolved: §8.2 inherits `receipts.py:61-65` set + extension rule | §8.2 | None |
| 4-3 | Medium | case-fold over-rejects / under-detects | Resolved — (superseded by Review 7 / 7-5): the write-based per-directory probe is **removed**; unconditional case-fold rejection, NFC, no primary mutation (§8.2) | §8.2 | Over-rejects case-distinct paths on case-sensitive FS (accepted; deterministic + safe) |
| 4-4 | Medium | `result_content_hash` ambiguous per op | Resolved: §8.2 per-op requirement table + M41 | §8.2 | None |
| 4-5 | High | apply TOCTOU vs planted symlink | Resolved — (superseded by Review 7 / 7-6): final-component `O_NOFOLLOW` alone is insufficient; handle-relative `dir_fd`-bound apply (§9.3) + M42 | §9.3 | Documented residual under `strong_path_race_required` (§9.3) |
| 4-6 | Medium | manifest-swap window during repair | Resolved: §8.3 — load-by-generation, abort on hash mismatch + M44 | §8.3 | None |
| 4-7 | Low | containment check via `resolve()` follows symlinks | Resolved: §4.2 uses `os.path.realpath` + symlink rejection | §4.2 | None |

### Review 5 — Actor identity and authorization
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| 5-1 | **Critical (blocker)** | peer-PID mechanism asserted, absent in code | Resolved — (superseded by Review 7 / 7-1): the **kernel-held lock** is the sole liveness authority; PID probes removed (§3.2.1). §6.1 keeps peer-credential **RPC-caller** authentication only, never lease ownership | §3.2.1, §6.1 | None |
| 5-2 | **Critical (blocker)** | `subject_id` unconstrained re-opens injection (proof 4) | Resolved: §6.1 pins `subject_id` to `_OPAQUE_ID`, enums for `assurance_level`/`authorization_result`, `VERIFIED_ACTOR_KEYS` + M43 | §6.1, §2.3 | None |
| 5-3 | Medium | `local_unverified` gate not default-deny | Resolved: §6.2 default-deny rule | §6.2 | None |
| 5-4 | High | TOCTOU between gateway and broker authz | Resolved: §6.2 — both checks bound to `(actor, change_manifest_hash, policy_version)` with `policy_version` pinned at startup | §6.2 | None |
| 5-5 | Medium | denied authz leaves run stuck forever | Resolved: §6.2 persistent-denial terminal path | §6.2, §2.1 | None |
| 5-6 | Low | `resolver_identity`/`subject_id` collision in migration | Resolved: §6.1 — `resolver_identity` removed in v3, replaced by `subject_id`+`assurance_level` | §6.1 | None |

### Review 6 — Adversarial final (the 8 proofs)
| Proof | Verdict | Note |
|---|---|---|
| 1 approval recorded without application | PREVENTS (Phase B onward) | Per Finding 9 the v2 `approved→completed` mapping **stays in place** during Phase A and is displaced when Phase B lands the complete v3 gate; live in code today until Phase B ships. |
| 2 completion after partial application | PREVENTS | §9.5 + E11 receipt/journal/tree agreement |
| 3 two runs concurrently apply one primary | PREVENTS | §3.2/§3.2.1 kernel-held per-primary lock (5-1 probe superseded by Review 7 / 7-1) |
| 4 actor forged through a string | PREVENTS (after 5-2 fix) | §6.1 `_OPAQUE_ID` + enum + M43; today's `resolver_identity` string is replaced |
| 5 repaired proposal reuses older approval | PREVENTS | §8.3 manifest invalidation + load-by-generation (conditional on 4-6 fix) |
| 6 mutable evidence pollutes primary hash | PREVENTS | §4 governance-state-outside-primary + containment fail-closed |
| 7 redaction alters bytes after audit | PREVENTS | §7 redaction-free bytes channel + content-hash pinning |
| 8 crash recovery double-applies an op | PREVENTS | §9.3 write-ahead journal + §9.5 idempotent-per-op (conditional on 4-4 table) |

**All 8 proofs now PREVENTED** by named mechanisms; the initially-blocking proof 4
is resolved via §6.1 + §3.2.1.

---

## 16. Residual risks (after revisions)

1. **Phase A is mandatory before any v3 approval is accepted.** Per Finding 9
   the v2 `approved→completed` mapping stays in place through Phase A and is
   displaced only when Phase B lands the complete v3 gate — until then,
   today's code still records approval as completed (proof 1, live). The
   design's correctness assumes Phase A (then Phase B) ships before any v3
   approval is accepted.
2. **The identity story is `local_unverified` until a real IdP lands.** §6
   codifies `local_unverified` honestly (default-denied in production), but a
   verified human actor requires the future IdP (SPEC §7 Step 3). The
   process-authentication (§6.1, RPC-caller peer-PID) authenticates the *calling
   process* of the gateway RPC, not the human — that gap is intentional and
   documented, not closed. (Lease ownership is the kernel lock, §3.2.1, not
   process auth.)
3. **macOS lock primitive** — choose `fcntl` vs `flock` at implementation; both
   auto-release on process exit. (The earlier libproc liveness-probe residual is
   removed; ownership is kernel-lock-based.)
4. **Cross-user hosts** with per-user `governance_state_root` defaults break
   per-primary lease sharing (R2.2). Mandate a shared lock location on multi-user
   hosts, or document single-operator use.
5. **Schema v3 is proposed, not implemented.** Every `*_KEYS` whitelist,
   `TransitionRule` row, and the `validate_v3_receipt_contract` rewrite
   (especially §2.3 R1.2) are implementation work; this document specifies them
   but they do not exist in code.

---

## 17. Review 7 — Operator architecture review (nine findings)

Recorded with severity · evidence · disposition · changed section · residual
risk. **Historical record of the Review-7 round.** All nine were resolved in
the sections cited; the revised doc subsequently passed independent re-review
(§18), followed by the reconciliation closed-schema consistency pass
(§2.5/§2.6).

| ID | Sev | Finding | Disposition | Section | Residual risk |
|---|---|---|---|---|---|
| 7-1 | **Critical** | PID-probed lease ownership fails under PID reuse (process exits, PID reused before probe → dead owner appears live) | Resolved: §3.2/§3.2.1 — kernel-held lock (`fcntl`/`flock`/`LockFileEx`), fd held for whole transaction, auto-released on exit; PID/path/start-time are diagnostic only | §3.2, §3.2.1 | macOS `fcntl` vs `flock` semantics choice at impl |
| 7-2 | High | parallel `receipt_schema_version` invented; existing `schema_version` is authoritative | Resolved: §2/§2.3/§2.4 — use existing `schema_version="3.0.0"`; cert schema stays separate; dispatch from existing field | §2, §2.3, §2.4 | None |
| 7-3 | **Critical** | `change_applied` wrongly called terminal; `run_decision` must be the only terminal | Resolved: §2.1/§2.3/§9.2 — `run_decision` is the sole terminal; `change_applied`/recovery are non-terminal prerequisites; lifecycle not added to `CURRENT_AUDIT_TRANSITIONS` | §2.1, §2.3, §9.2 | None |
| 7-4 | High | `_manifest_generation` confusion could select the wrong approved manifest | Resolved: §8.1/§8.3 — separate `change_set_id` + `change_manifest_generation`; `_manifest_generation` never selects the change manifest; `change_proposed` binds the tuple; M44 updated | §8.1, §8.3 | None |
| 7-5 | Medium | write-based case probing mutates primary and is non-portable | Resolved: §8.2 — removed; NFC + unconditional case-fold reject; case-distinct support recorded as future feature; no-mutation test added | §8.2 | Over-rejects distinct paths on case-sensitive FS (accepted; deterministic+safe) |
| 7-6 | **Critical** | `O_NOFOLLOW`+`os.replace` does not defeat parent-dir replacement | Resolved: §9.3 — handle/`dir_fd`-relative model (`openat`/`renameat`/`O_DIRECTORY\|O_NOFOLLOW` per component, `fstat` revalidation); honest residual + `strong_path_race_required` fail-closed policy | §9.3 | Older Python/FS without `dir_fd` → fail closed (documented) |
| 7-7 | High | no expected final tree hash in manifest → success unverifiable | Resolved: §8.1/§8.3/§9.2 — `result_tree_hash` added; §9.2 step 13 compares to it (reconciliation revision: v1 invariant `result_tree_hash == workspace_tree_hash`; arbitrary workspace exclusion removed — scratch lives outside the workspace, §8.1/E14) | §8.1, §8.3, §9.2 | None |
| 7-8 | High | pre-apply authz denial wrongly routed to apply-failure/recovery | Resolved: §2.1/§6.2 — `approval_invalidated` transition (no journal/backup/recovery emitted); exact `APPROVAL_INVALIDATED_KEYS` schema in §6.2; binds action/change-set/subject/policy/reason | §2.1, §6.2 | None |
| 7-9 | High | phased rollout could half-enable v3; identity consequence unstated | Resolved: §12 — dark launch: Phase A disabled plumbing (v2 unchanged); Phase B complete v3 + capability gate + **minimal signed local credential provider**; Phase C full IdP. v3 off until end-to-end gate present | §12 | None |

---

## 18. Independent re-review — nine vectors (all PREVENTED; doc defects fixed)

A focused re-review attempted to break each of the nine Review-7 attack vectors
against the revised doc + `72e8693` code. **Result: all nine PREVENTED at the
mechanism level — none flipped to ALLOWS.** The re-review found doc-precision
defects (not security holes); all are fixed in this revision:

| Vector | Re-review verdict | Disposition in this revision |
|---|---|---|
| 1 PID-reuse lock ownership | PREVENTS; §10/§15-R2.3/§16.3 described removed PID-probe | Fixed: §10/§15-R2.3/§16.3 now cite the kernel lock (§3.2.1) as sole authority; RPC peer-PID is caller-auth only |
| 2 schema-dispatch disagreement | PREVENTS; precision gap | Fixed: §2.3 schema cross-check (manifest vs receipts `schema_version` must match; dispatch pins to receipts' value) |
| 3 completion before terminal decision | PREVENTS; R1.2 missed 3rd change + writer_role | Fixed: §2.3 R1.2 now lists 3 coupled changes incl. `run_evidence.py:1156-1157`, pins `applier` writer_role, requires the generic branch enforce `change_applied` |
| 4 proposal selection via generation confusion | PREVENTS; broker max-gen un-named | Fixed: §8.3 broker rejects approval whose generation < max in `change_proposed` receipts |
| 5 case-probe primary mutation | PREVENTS (clean) | No change needed; NFC + unconditional casefold, in-memory, no mutation |
| 6 parent-dir replacement | PREVENTS; `AT_FDCWD` inconsistency | Fixed: §9.3 uses `fstatat(parent_fd, …)` so verification + rename share the validated handle |
| 7 success without `result_tree_hash` | PREVENTS; derivation hand-waved | Fixed: §8.1 concrete derivation; final revision: exclusion removed — v1 invariant `result_tree_hash == workspace_tree_hash` with scratch outside the workspace (§8.1/E14) |
| 8 false recovery on pre-apply authz denial | PREVENTS; precondition unstated | Fixed: §2.3 `approval_invalidated` precondition `no change_apply_started` is machine-enforced |
| 9 partially enabled v3 in rollout | PREVENTS (Phase A triple-backstopped); Phase B gate hand-waved | Fixed: §12 concrete `v3_enabled` predicate (5 components) evaluated at run creation + `change_apply_started`; fail-closed `v3_end_to_end_gate_not_satisfied` |

**Overall re-review verdict: SOUND** — all nine vectors prevented; the Phase B
gate is machine-checkable. The PR is marked ready for operator re-review.

**Reconciliation addendum (this revision).** A post-re-review
content-consistency audit still found residual defects in the `da4d9f6`
revision: one normative contradiction (§2.3 "terminal where indicated" vs
Finding 3), stale superseded dispositions in the §15/§16 history tables, and
closed-schema gaps (`change_proposed` absent from the §2.1 inventory with no
`CHANGE_PROPOSED_KEYS`; `APPROVAL_INVALIDATED_KEYS` undefined; orphaned
`MANIFEST_SEALED_KEYS`; stale generic `manifest_hash`/`manifest_generation`
field names; PID-probe residue in `CHANGE_RECOVERY_STARTED_KEYS`). All are
fixed in this revision; the closed-schema consistency audit (§2.6) is the
authoritative completeness check.

**Contract-finalization addendum (final independent review).** A final
independent closed-schema review then found contract-level defects the
mechanical audit missed: prose `evidence_basis` values (only the closed enum
`observed`/`derived`/`submitted` is permitted); an undefined `broker` writer
role (the broker is a process boundary, not a receipt authority identity);
`action_opened` mis-attributed to the operator gateway (it stays
`orchestrator`/`derived`); artifact references validated as opaque IDs
instead of `_ARTIFACT_PATH`; the arbitrary `workspace_excluded` derivation
(replaced by the v1 invariant `result_tree_hash == workspace_tree_hash` with
scratch under `<governance_state_root>/scratch/<run_id>/`); incomplete
apply/recovery payloads (missing change tuples, `journal_hash`,
`provider_dispatch`); missing exact v3 allowlists for `action_opened` /
`action_resolved` / `run_decision`; and a `12/12` audit count that treated
`seal` as a receipt transition. All are fixed in this revision; §2.6 is the
authoritative audit — 11 governed receipt transitions audited + 1 seal
operation audited separately.
