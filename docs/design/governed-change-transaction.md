# Governed Change Transaction — Design

**Status:** Draft for operator review. Design only — **no implementation is
included**. Resolves blocker decisions D1–D3.
**Branch:** `design/governed-change-transaction`, based exactly on
`origin/main` `72e86932168f93b8c77a65b4da76b9f06008d27b`.
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

| Event | Receipt (schema v3) | Meaning | Producer |
|---|---|---|---|
| **approval_granted** | `action_resolved` (resolution=`approved`) | A verified actor authorized the proposal. **Nothing is written to the primary tree.** `action_resolved` is **non-terminal** for an approval. | operator gateway → broker |
| **apply_started** | `change_apply_started` | The transaction has begun under the per-primary apply lease; journal prepared and hashed. Outcome unknown. | broker (trusted process) |
| **change_applied** | `change_applied` | The transaction committed: every operation applied, final primary tree hash re-verified, manifest result hash matched. **This is the terminal success transition.** | broker |

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

`receipt_schema_version = "3.0.0"`. **Major** because approval and
terminalization semantics change incompatibly: approval is no longer terminal.

### 2.1 Lifecycle transitions (schema v3)

```
action_opened
action_resolved              # non-terminal when resolution=approved
change_apply_started
change_applied               # terminal success
change_apply_failed
change_recovery_started
change_recovery_completed
change_recovery_required     # leaves the chain unsealed until FS state is proven
run_decision                 # terminal (completed|blocked|failed)
```

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
  evidence_basis, precondition)` rows for each §2.1 transition. `change_applied`
  and the recovery transitions are terminal where indicated.
- **Audit-transition declarations** — extend the closed
  `CURRENT_AUDIT_TRANSITIONS` set.
- **Payload-key allowlists** — new bounded whitelists alongside
  `ACTION_OPENED_KEYS`/`ACTION_RESOLVED_KEYS`/`RUN_DECISION_KEYS` for each new
  transition (e.g. `CHANGE_APPLIED_KEYS`).
- **Local payload validators** — `validate_receipt_payload` branches for each
  new transition's bounded fields.
- **Lifecycle validators** — `validate_v3_receipt_contract`: `completed` is
  forbidden without a valid `change_applied` referencing the approved manifest;
  `action_resolved(approved)` is non-terminal; terminalization only via
  `change_applied`/`change_apply_failed`→recovery→`run_decision`.
- **⚠ Load-bearing v2 rewrite (review R1.2):** making `action_resolved(approved)`
  non-terminal is **not** achievable by adding transitions alone. The v2 code at
  `run_evidence.py:1116-1168` maps an approved action to `mapped_decision=
  "completed"` via `action_outcomes[action_id]["approved"]` (sourced from
  `orchestrator.py:1273-1276`'s `outcome_map {"approved":"completed"}`), and the
  operator-gateway `run_decision` branch then sets `terminal_decision=True`.
  `validate_v3_receipt_contract` MUST change two things: (i) `outcome_map` for
  approval transitions no longer maps to `completed` (Phase A removes this), and
  (ii) the operator-gateway `run_decision` terminal-decision logic at
  `run_evidence.py:1149-1168` must NOT fire on an approval — `terminal_decision`
  is set only by `change_applied`/`change_apply_failed`→recovery→terminal
  `run_decision`. Without naming and rewriting this exact path, an implementer
  could add all the new transitions and approval would still collapse to
  terminal. This is the single most important v3 verifier change.
- **Terminal-state validators** — enforce §2.2 ordering and the no-seal-while-
  uncertain rule: `seal()` (`receipts.py:1654` calls `validate_*_receipt_contract
  (..., sealed=True)`) is rejected if the last non-recovery transition is
  `change_recovery_required` or `change_apply_started`; sealing is permitted
  only after `change_applied` or `change_recovery_completed`→terminal
  `run_decision`.
- **Payload-key allowlists (review R1.6) — concretely specified:** each new
  transition gets an exact `*_KEYS` frozenset (matching the codebase pattern at
  `run_evidence.py:138-140,282-302`) and a `validate_receipt_payload` branch:
  - `CHANGE_APPLY_STARTED_KEYS = {journal_hash, journal_sequence,
    manifest_hash, prior_tree_hash}`
  - `CHANGE_APPLIED_KEYS = {manifest_hash, manifest_generation,
    post_tree_hash, files_written, applied_subject_id, applied_assurance_level,
    actor_artifact, actor_artifact_hash, authorization_policy,
    authorization_result}`
  - `CHANGE_APPLY_FAILED_KEYS = {manifest_hash, reason, recoverable}`
  - `CHANGE_RECOVERY_STARTED_KEYS = {abandoned_run_id, abandoned_pid,
    probe_method, operator_subject_id}` (operator-acknowledged)
  - `CHANGE_RECOVERY_COMPLETED_KEYS = {outcome, restored_tree_hash,
    journal_reconciled: bool}`
  - `CHANGE_RECOVERY_REQUIRED_KEYS = {manifest_hash, uncertain_since_sequence}`
  - `MANIFEST_SEALED_KEYS = {manifest_hash, manifest_generation,
    workspace_tree_hash, g2a_binding}`
  - `VERIFIED_ACTOR_KEYS` = the §6.1 actor field set, all `_OPAQUE_ID`/enum.
  The floor validator (`_oversized_value`, `run_evidence.py:396`) alone is
  insufficient — every transition MUST have its exact whitelist or the receipt
  becomes a bounded-but-open signed-prose channel.
- **Receipt schema version dispatch (review R1.4)** — `verify_receipt_store`
  (`receipts.py:1695`) branches on the chain's `schema_version`
  (`receipts.py:1721-1756`): a chain stamped `3.0.0` routes to
  `validate_v3_receipt_contract`; `2.0.0` retains `validate_v2_receipt_contract`
  unchanged. `_writer_contract_finding` (`receipts.py:117-122`) dispatches to the
  matching payload validator. The portable/external-trust verifier path
  (`receipts.py:1768`) gains the same branch. **`receipt_schema_version` is a
  new per-chain field distinct from the existing manifest `schema_version`**
  (review R1.3) — recorded in the sealed manifest head, set at chain creation,
  immutable; mixed-version chains are rejected (`mixed_receipt_schema_version`,
  specialized from today's `version_inconsistency` at `receipts.py:1753`). During
  migration, v2-capable and v3-capable runs coexist by stamping their chains;
  a v2 chain is never extended with v3 transitions.
- **Portable verification** — the offline verifier (`verify_receipt_store`)
  gains v3 rules; v3 receipts remain exportable and third-party-verifiable.
- **Certificate compatibility** — if a new writer role (e.g. an `applier`
  broker role) is introduced, the run certificate's writer-key set and
  `_CERTIFICATE_SCHEMA_VERSION` are bumped; existing keys are preserved.
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

`receipt_schema_version` is recorded in the sealed manifest head and checked at
verify dispatch. Mixed-version chains are rejected
(`mixed_receipt_schema_version`).

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

### 3.2 Per-primary apply lease (cross-process exclusion)

A process-local `RLock` is insufficient. The design uses a per-primary OS lock:

```text
ApplyLease
  canonical_primary_path
  primary_path_hash           # sha256(canonical primary path)
  owning_run_id
  owning_process_id
  acquired_at
  lease_version               # journal/lease schema version
```

Stored under `<governance_state_root>/locks/<primary_path_hash>`. Requirements:
- **atomic exclusive acquisition** (`O_CREAT|O_EXCL`, 0600);
- **usable across processes** (file lock, not in-memory);
- **shared by every TORQ run targeting the same primary** (keyed by primary,
  not run);
- **stale-owner detection that never silently steals a live lock** — see
  §3.2.1 for the concrete per-platform kernel liveness probe. A live owner is
  never stolen; only an *abandoned* owner is recoverable, via explicit recovery.
- **explicit recovery for an abandoned owner** (`change_recovery_started`
  receipt, operator-acknowledged);

### 3.2.1 Stale-owner liveness — concrete per-platform probe (reviews R2.3, 5-1)

The earlier "kernel-authenticated broker peer-PID channel" was a placeholder.
The intra-broker Pipe/authkey (`evidence_broker.py:124,164,196,233`)
authenticates a caller *to one broker*; it cannot reach a *different* broker
that owns the lease. Stale-owner detection therefore uses a real kernel liveness
probe against the `owning_process_id` recorded in the lease:

- **Linux:** `pidfd_open(pid)` (or fall back to `kill(pid, 0)` + `/proc/<pid>`
  owner check). `pidfd_open` is safe against PID reuse: the fd pins the kernel
  process identity. If `pidfd_open` succeeds, the owner is alive → do not steal.
- **macOS:** `proc_pidpath(pid)` / `proc_listallpids` (libproc) to confirm the
  PID still resolves to a torq broker process path; combined with the lease's
  `acquired_at` + `owning_run_id` for attribution.
- **Windows:** `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, pid)` +
  `GetProcessId`+`QueryFullProcessImageName`; the handle pins identity (no PID
  reuse while held).

A lease is stealable only when the probe reports the owner **dead** AND the
recovery is operator-acknowledged (`change_recovery_started`). The probe runs
inside the *requesting* broker; no shared memory between brokers is required.
PID in the lease file is **never** trusted alone (PID reuse) — the kernel
handle/fd is the authority. This mechanism is the same one used to authenticate
the operator-gateway RPC's calling process (§6.1.1).
- **bounded lock metadata**; **no dependence on shared memory between brokers**.

**The lease is NOT held during model execution.**

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
actor_artifact           # reference to the encrypted artifact (_OPAQUE_ID)
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
- **Process authentication (reviews 5-1, R2.3):** the broker authenticates the
  *calling process* — not the human — via the §3.2.1 kernel liveness/probe
  mechanism (`SO_PEERCRED`/`LOCAL_PEERCRED`/`GetNamedPipeClientProcessId`). The
  calling process's identity is bound to the run at first RPC; a string is never
  accepted as a process identity. The human actor comes only from the encrypted
  `VerifiedActor` artifact.
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

`ApprovalPolicy.can_approve(actor, manifest_hash, policy_version) ->
AuthorizationResult{allowed: bool, reason: _OPAQUE_ID}`. Enforced at two points
and **bound to the same tuple** (reviews 5-4): (a) the operator-gateway approval
RPC before `action_resolved(approved)` is recorded, and (b) the broker before
`change_apply_started` — the broker **re-runs** `can_approve(actor,
manifest_hash, policy_version)` and aborts unless the result is `allowed` AND
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

**Persistent-denial terminal path (review 5-5):** a denied authorization at the
broker gate does not leave the run stuck non-terminal forever. The lifecycle
(§2.1) gains: broker re-check denied → `change_apply_failed` (reason
`authorization_denied`) → `change_recovery_completed` → `run_decision(failed)` →
seal. Alternatively the operator may explicitly `action_resolved(rejected)`.
Every run reaches a sealed decision; no unbounded `awaiting_approval`.

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

### 8.1 ChangeSetManifest (versioned, explicit operations)

```text
ChangeSetManifest
  schema_version            # "1.0.0"
  run_id
  primary_tree_hash         # pinned, captured before any write
  workspace_tree_hash       # at seal
  operations[]              # canonical: sorted by path
  manifest_hash            # domain-separated hash (§8.2)
  created_at
```
**Initial operations:** `create`, `update`, `delete`. **Defer `rename` and
mode changes** until cross-platform semantics are fully defined — unsupported
operations fail closed.
Each operation:
```text
operation                 # create|update|delete
path                      # UTF-8 forward-slash relative
prior_content_hash        # when applicable
result_content_hash       # when applicable
content_artifact          # when applicable (reference to encrypted artifact)
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
- **Reject Unicode-normalization collisions** — NFC-normalize before compare
  (unconditional; correct on all filesystems).
- **Reject case-fold collisions (review 4-3):** case-fold compare is
  **conditional on a real per-directory probe** (Python stdlib cannot portably
  report case sensitivity): at apply time, create a probe file and attempt to
  stat a case-flipped name; cache the result per-directory. On case-sensitive
  filesystems (ext4) the probe reports sensitive → do NOT case-fold (so
  distinct `Foo.py`/`foo.py` are not over-rejected); on case-insensitive
  volumes (default APFS, NTFS) → case-fold compare.
- **Reject duplicate operation targets.**
- **Symlink creation/modification denied in v1** (operations are regular-file
  bytes only) — AND enforced at apply per-target via `O_NOFOLLOW` (§9.3, review 4-5).
- **Protected paths denied** (`GuardedPaths._PROTECTED_NAMES` + `.env.*`).
- Canonical JSON serialization; **domain-separated hashing (review 4-1, E9) —
  concrete construction:** `manifest_hash = "sha256:" + sha256(
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
manifest_hash
manifest_artifact_hash
workspace_tree_hash
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

**Manifest-swap defense (review 4-6):** the approval assertion artifact embeds
`manifest_hash` AND `manifest_generation`. The broker loads the manifest **by
generation** (the signed manifest anchor's monotonic `_manifest_generation`,
`receipts.py:1070-1085,1087-1113` is the single source of "which generation is
current") and **aborts** if its recomputed hash ≠ the approved `manifest_hash`.
The signed anchor — not "whichever manifest artifact the broker finds on disk" —
is authoritative. This closes the window where two valid manifests exist during
a repair and approval could be re-pointed. Mutant **M38** covers reduction
tamper; a generation-swap mutant (**M44**) covers loading the wrong generation.

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
13. Compare to the manifest's expected result hash.
14. Append `change_applied`.
15. Append terminal `run_decision(decision=completed)` and seal.
16. Mark/archive the journal as committed.
17. Release the apply lease.

### 9.3 Single-file replacement (within §9.2 step 10)

For each `update`/`create`:
1. **Resolve target via `GuardedPaths`** and verify with `lstat` it is a regular
   file with `st_nlink==1` — **immediately before** the rename (review 4-5). Use
   `os.open(target, O_NOFOLLOW)` (the receipt writer already does this at
   `receipts.py:208-210`; reuse it). This defends against a symlink planted at
   the target between lease+hash-revalidation (§9.2 step 3/6) and apply —
   `os.replace` onto an `O_NOFOLLOW`-opened regular file cannot follow an
   attacker symlink out of the tree.
2. Write reserved temp (`.torq-apply-<run_id>-<nonce>.tmp`) in target's parent
   dir (§4.3), with `O_NOFOLLOW|O_CREAT|O_EXCL`.
3. **Write-ahead ordering (review R3.1):** record `{op, path, sha256, sequence}`
   in the journal **and fsync the journal BEFORE** `os.replace` — not after.
   This eliminates the mid-fsync window where the journal is stale relative to
   the bytes: on recovery, either the journal has the op (so replay knows it may
   be applied — verify by hash) or it does not (so the bytes were never
   replaced). The discriminator R3.1 asked for is "journal precedes rename."
4. fsync temp + parent (reuse `_fsync_directory`, `receipts.py:151-194`, with
   its Windows error-code tolerance 1/5/87 — review R3.3; do NOT reimplement).
5. `os.replace(temp, target)`.
6. fsync target + parent (again via `_fsync_directory`).

For a `delete`: `lstat`+`O_NOFOLLOW` verify, backup bytes (§9.4), journal
write-ahead (`{op:"delete", path, prior_sha256, sequence}`), fsync journal,
`os.unlink(target)`, fsync parent. Rollback restores bytes for `update`/`delete`
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
`(manifest_hash, path)` and idempotent — re-applying an already-applied op is a
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
- PID in lock/lease: advisory only; never used to decide a process is dead (PID
  reuse). Stale-owner detection uses the kernel-authenticated broker peer-PID
  channel (§6.1), not PID liveness.
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
  → killed by the `O_NOFOLLOW`/`lstat` apply test (§9.3).
- **M43** a `subject_id` carrying prose/control chars accepted → killed by the
  opaque-token validator test (§6.1, proof 4).
- **M44** broker loads the wrong manifest generation on recovery → killed by the
  generation-swap test (§8.3).

---

## 12. Phased migration

**Phase A — Plumbing (no apply).** `primary` + `GovernedRunContext` threaded
through broker; `governance_state_root` with containment fail-closed; typed
change-artifact channel for one provider; `ChangeSetManifest` built and sealed
(without apply). `awaiting_approval` emits the manifest hash. No
`change_applied`; `completed`-on-approval mapping **removed** in the same phase
(approval yields a non-terminal `action_resolved(approved)` awaiting apply —
Phase A runs therefore cannot reach `completed`, which is honest: apply isn't
implemented yet). This avoids shipping a `manifest_sealed` that promises
application it can't deliver.

**Phase B — The transaction (core).** Schema v3 lifecycle; broker apply; per-
primary lease; journal + recovery; receipts; mutants M31–M40. `completed`
restored, gated on `change_applied`.

**Phase C — Hardening.** Dirty-primary policy; workspace purge cadence;
additional provider adapters for the typed channel; verified-actor IdP (§6)
beyond `local_unverified`.

Identity (full RBAC/OIDC, SPEC §7 Step 3) layers on the §6 seam and is out of
scope here.

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

## 15. Independent review findings and dispositions

Six independent reviews ran against this document and the `72e8693` code. Every
finding is recorded with severity · evidence · disposition · section changed ·
remaining risk. The two initially-blocking facets — (a) the "kernel-authenticated
peer-PID channel" being a placeholder and (b) `subject_id` being unconstrained
(proof 4) — are resolved in §3.2.1, §3.1.1, and §6.1.

### Review 1 — Receipt schema v3 and lifecycle
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| R1.1 | High | writer_role for apply/recovery transitions unspecified | Resolved: §2.3 states a new `applier` broker role is required (not conditional), with certificate bump | §2.3 | Implementer must pin one role per transition at build time |
| R1.2 | **Critical** | non-terminal approval requires rewriting `run_evidence.py:1149-1168` operator-gateway terminal logic | Resolved: §2.3 now names the exact code path + the two required changes (outcome_map removal + terminal-decision rewrite) | §2.3 | Load-bearing; if missed, approval still collapses to terminal |
| R1.3 | High | `receipt_schema_version` field invented, not in code | Resolved: §2.3 states it is a new per-chain field, distinct from manifest `schema_version`, set at creation | §2.3 | Migration tooling must stamp it |
| R1.4 | High | v3 dispatch unspecified | Resolved: §2.3 specifies the `verify_receipt_store` branch, portable verifier path, v2/v3 coexistence by stamping | §2.3 | None |
| R1.6 | High | all 7 new transitions lack payload whitelists | Resolved: §2.3 lists exact `*_KEYS` frozensets for every new transition + validators | §2.3 | None |
| R1.7 | Medium | `change_recovery_required` terminality rule unspecified | Resolved: §2.3 terminal-state validator bullet | §2.3 | None |

### Review 2 — Broker/process ownership and per-primary locking
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| R2.1 | **Critical** | broker has no `primary` parameter today | Resolved: §3.1.1 specifies the concrete `RunController`/`EvidenceBrokerProcess.start`/`_broker_process_main` signature changes | §3.1.1 | Phase-A plumbing; primary must enter at `_handle_run` only |
| R2.2 | High | lease shared only if `governance_state_root` shared across runs | Resolved: §4.2 default is a single shared user-data dir; cross-root exclusion is documented as a precondition | §3.2, §4.2 | Multi-user hosts with per-user roots need a shared lock location |
| R2.3 | **Critical** | "peer-PID channel" cannot do cross-broker liveness | Resolved: §3.2.1 specifies concrete per-platform probes (`pidfd_open`/`proc_pidpath`/`OpenProcess`) | §3.2.1 | macOS libproc availability |
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
| 4-3 | Medium | case-fold over-rejects / under-detects | Resolved: §8.2 — conditional on a real per-directory probe; NFC unconditional | §8.2 | Probe cost |
| 4-4 | Medium | `result_content_hash` ambiguous per op | Resolved: §8.2 per-op requirement table + M41 | §8.2 | None |
| 4-5 | High | apply TOCTOU vs planted symlink | Resolved: §9.3 — `lstat`+`O_NOFOLLOW` per target before replace + M42 | §9.3 | None |
| 4-6 | Medium | manifest-swap window during repair | Resolved: §8.3 — load-by-generation, abort on hash mismatch + M44 | §8.3 | None |
| 4-7 | Low | containment check via `resolve()` follows symlinks | Resolved: §4.2 uses `os.path.realpath` + symlink rejection | §4.2 | None |

### Review 5 — Actor identity and authorization
| ID | Sev | Finding | Disposition | Section | Remaining risk |
|---|---|---|---|---|---|
| 5-1 | **Critical (blocker)** | peer-PID mechanism asserted, absent in code | Resolved: §3.2.1 + §6.1 specify concrete per-platform probes | §3.2.1, §6.1 | None |
| 5-2 | **Critical (blocker)** | `subject_id` unconstrained re-opens injection (proof 4) | Resolved: §6.1 pins `subject_id` to `_OPAQUE_ID`, enums for `assurance_level`/`authorization_result`, `VERIFIED_ACTOR_KEYS` + M43 | §6.1, §2.3 | None |
| 5-3 | Medium | `local_unverified` gate not default-deny | Resolved: §6.2 default-deny rule | §6.2 | None |
| 5-4 | High | TOCTOU between gateway and broker authz | Resolved: §6.2 — both checks bound to `(actor, manifest_hash, policy_version)` with `policy_version` pinned at startup | §6.2 | None |
| 5-5 | Medium | denied authz leaves run stuck forever | Resolved: §6.2 persistent-denial terminal path | §6.2, §2.1 | None |
| 5-6 | Low | `resolver_identity`/`subject_id` collision in migration | Resolved: §6.1 — `resolver_identity` removed in v3, replaced by `subject_id`+`assurance_level` | §6.1 | None |

### Review 6 — Adversarial final (the 8 proofs)
| Proof | Verdict | Note |
|---|---|---|
| 1 approval recorded without application | PREVENTS (Phase A/B) | Phase A removes `approved→completed`; Phase B gates on `change_applied`. Live in code today until Phase A ships. |
| 2 completion after partial application | PREVENTS | §9.5 + E11 receipt/journal/tree agreement |
| 3 two runs concurrently apply one primary | PREVENTS | §3.2 per-primary lease (conditional on 5-1 probe) |
| 4 actor forged through a string | PREVENTS (after 5-2 fix) | §6.1 `_OPAQUE_ID` + enum + M43; today's `resolver_identity` string is replaced |
| 5 repaired proposal reuses older approval | PREVENTS | §8.3 manifest invalidation + load-by-generation (conditional on 4-6 fix) |
| 6 mutable evidence pollutes primary hash | PREVENTS | §4 governance-state-outside-primary + containment fail-closed |
| 7 redaction alters bytes after audit | PREVENTS | §7 redaction-free bytes channel + content-hash pinning |
| 8 crash recovery double-applies an op | PREVENTS | §9.3 write-ahead journal + §9.5 idempotent-per-op (conditional on 4-4 table) |

**All 8 proofs now PREVENTED** by named mechanisms; the initially-blocking proof 4
is resolved via §6.1 + §3.2.1.

---

## 16. Residual risks (after revisions)

1. **Phase A is mandatory before any v3 approval is accepted.** Until Phase A
   removes `approved→completed`, today's code still records approval as
   completed (proof 1, live). The design's correctness assumes Phase A ships first.
2. **The identity story is `local_unverified` until a real IdP lands.** §6
   codifies `local_unverified` honestly (default-denied in production), but a
   verified human actor requires the future IdP (SPEC §7 Step 3). The
   process-authentication (§3.2.1/§6.1) authenticates the *calling process*, not
   the human — that gap is intentional and documented, not closed.
3. **macOS liveness probe** depends on libproc availability; the Linux/Windows
   probes are stdlib/OS-API. A fallback (operator-acknowledged recovery only) is
   acceptable if libproc is unavailable.
4. **Cross-user hosts** with per-user `governance_state_root` defaults break
   per-primary lease sharing (R2.2). Mandate a shared lock location on multi-user
   hosts, or document single-operator use.
5. **Schema v3 is proposed, not implemented.** Every `*_KEYS` whitelist,
   `TransitionRule` row, and the `validate_v3_receipt_contract` rewrite
   (especially §2.3 R1.2) are implementation work; this document specifies them
   but they do not exist in code.
