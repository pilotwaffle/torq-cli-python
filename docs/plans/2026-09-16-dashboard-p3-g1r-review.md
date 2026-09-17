# Dashboard P3 G1R review

Reviewed `2026-09-16-dashboard-p3-contract.md` and its closed-schema/crash-order
addendum against merged P2 baseline
`e32ecdc3ed2ab3df1f6fa00e3d49994532c4474a`, the sealed candidate evidence
contract, current task projection/store, Fleet session handling, and the existing
unsafe approval helper. This is a contract review; no native source mutation,
provider call, or recovery exercise was performed.

## Judgment

**APPROVE — `READY_FOR_P3_BUILDER`.** The amended contract is implementable as a
bounded task-specific review, refinement, acceptance, and explicit application
service. It pins the bytes reviewed to the bytes eligible for application,
separates historical evidence from current applicability, and defines a
recoverable per-file primary transaction without claiming the broader design-only
schema-v3 governed-run migration.

## Findings

1. **Historical review has one evidence authority.** Plan, Changes, and Checks
   resolve from exact `(task_id, candidate_ready_sequence)` P2 receipts and
   encrypted artifacts. The review hash covers a canonical immutable core, all
   displayed diff components come from that core, and live source, disposable
   candidate cache, or upgraded checker bytes cannot silently replace history.
   Missing, substituted, cross-task, unsealed, or unresolved evidence fails
   closed. The normalized structural command is labelled accurately because P2
   retained its argv hash rather than raw historical argv.

2. **Review and application evidence are closed and independently linked.**
   `CandidateRef`, `DecisionRef`, `ArtifactRef`, review decisions, application
   events, journals, and the global marker have exact bounded shapes. Append and
   portable verification enforce receipt hashes, manifest hashes, sequence/order,
   common-context immutability, terminal/seal rules, and cross-chain verification.
   Generic signed `audit` content, mutable task state, or an opaque matching ID
   cannot authorize acceptance or application.

3. **Acceptance and Apply are separate consequences.** `candidate_accepted`
   records the exact reviewed candidate and a fresh scoped-source match but makes
   no project write. Apply requires a second authenticated idempotent request and
   independently verifies the accepted decision, P2 evidence, project mapping,
   complete scoped preimages, and absent create targets. Replays return the
   recorded result; a new request ID cannot apply an already-applied decision
   twice. No provider or checker is dispatched by review, Apply, or recovery.

4. **The primary transaction closes cross-process and crash windows.** A
   physical-root kernel lease coordinates aliases, while a fixed per-user global
   marker keeps other installations blocked after process death. The final order
   durably reserves the request and creates, flushes, and verifies the signed
   one-receipt apply authority before any shared marker. Therefore a
   pre-journal recovery can authenticate the existing identity and seal the
   no-mutation `apply_rejected` result without minting keys or certificates
   during recovery. Unknown, corrupt, or foreign-owner markers remain blocked.

5. **Writes and recovery are byte- and path-constrained.** Apply v1 requires
   existing safely anchored parent directories and creates or removes no primary
   directories. POSIX dirfd/nofollow and Windows no-reparse handle-relative
   operations replace only evidence-derived changed files. The immutable journal
   binds ordered preimages/results and fixed metadata policy before
   `apply_started`. Recovery changes a target only when its bytes match an
   authenticated preimage or intended result; any third state, unknown temp,
   identity substitution, or unverifiable metadata stays recovery-required.
   Applied and rolled-back terminals require exact full scoped verification.

6. **Refinement is a new candidate, not a rewrite of sealed P2 history.** Closed
   plan/input v2 shapes bind the parent candidate, original base, correction
   revision/hash/decision, and verified parent context without a circular plan
   commitment. A child emits a complete desired operation set against the
   original base, including explicit visibility of dropped parent changes.
   Durable reservation precedes dispatch and restart never redispatches. After a
   verified Apply, continuation uses a freshly reviewed live-source base.

7. **The user-facing scope remains truthful.** Search is navigation rather than
   authority; opening a result re-verifies evidence. Delayed responses cannot
   enable a stale selection. Accept copy says it records approval; Apply copy
   identifies source modification. P3 performs no Git commit, push, merge,
   deployment, arbitrary shell, generated-code execution, credential change, or
   project test runner, and structural validation is not presented as tests.

## Implementation-seam addendum

The `apply_authority_created` amendment preserves the existing general receipt
verifier invariant: a zero-count anchor is initialization state, never verified
application evidence. Original Apply must append and flush the unique signed
begin event and require ordinary verification of its one-receipt unsealed
manifest/anchor before creating the shared primary marker.

A restart before the marker distinguishes outcomes by read-only verification. A
fully committed begin prefix may be reopened without creation and terminalized as
the bounded no-mutation `apply_rejected`; a partial or unverifiable first commit
is quarantined as preparation-failed. Neither path recreates keys, certificates,
trust anchors, receipts, or artifacts, and neither permits a source write.

Post-marker recovery likewise uses existing identity only. Before committing an
authenticated uncovered tail, it verifies the covered begin prefix, every tail
signature and semantic link, and the nested
`detail.journal_artifact` cipher/plain hashes plus closed journal schema. The
legacy automatic tail path checks only a top-level artifact reference and cannot
run first for this contract. `live_catching_up` remains a recovery input state;
it never projects accepted or applied success by itself.

### Staged-identity recovery addendum

**APPROVE.** The amended recovery seam closes the attribution gap between an
atomic rename and its durable written receipt. Every journal operation now binds
the exact stable `parent_identity` and nullable preimage `before_identity`, each
with only nonnegative integer `volume` and `file_id` fields; the preimage identity
is null only for a create. Content hashes and metadata checks remain separate
from these stable identities.

Before rename, the narrow native callback runs with the finalized exclusive temp
handle still held and durably appends and verifies the exact
`apply_file_staged` event. Native commit then rechecks the staged identity/hash
and journal-bound parent identity. `apply_file_written` crosslinks the staged
receipt by sequence and receipt hash, so recovery inherits the signed result
identity without relying on process memory. Desired bytes are attributable to
TORQ only when their stable identity matches that signed staged identity. An
unsigned crashed temp or equal desired bytes under another identity remains
`recovery_required`; it is never deleted, overwritten, or promoted by content
alone. This amendment is builder-ready within the contract's cooperating-host,
non-hostile-writer boundary.

### Rollback staged-identity addendum

**APPROVE with the following closed protocol.** A replacement rollback creates a
temp in a deterministic rollback-only namespace, distinct from the forward-stage
leaf. Before rename it appends and verifies exactly one
`apply_rollback_file_staged` detail containing `rollback_started_sequence`,
`operation_index`, `path`, `base_hash`, and `staged_identity`. After rename it
appends exactly one `apply_rollback_file_restored` detail containing those first
four fields plus `staged_sequence` and `staged_receipt_hash`. The restored event
must crosslink the matching staged receipt. Events follow the unique rollback
start and process affected operations in strict reverse index order.

For a replacement, `base_hash` equals the journal preimage hash and
`staged_identity` is the finalized rollback temp identity. Restart reuses that
single signed temp; it never creates a second stage identity. It may commit the
exact revalidated temp while the target still has the signed forward identity,
or append a missing restored receipt when the base bytes have the signed rollback
identity. A missing temp while the forward target remains, or base-equivalent
bytes under any other identity, stays `recovery_required`. An untouched preimage
is accepted only with the journal's original `before_identity`.

For a create rollback, the staged event is the signed delete intent:
`base_hash` is null and `staged_identity` must equal the forward signed result
identity. Anchored deletion may proceed only while that exact target remains. The
restored crosslink is appended after verified absence. If the process crashes
after deletion but before that receipt, absence alone cannot prove the delete and
the transaction remains `recovery_required`; no equal-content or equal-state
identity waiver is allowed. Tests must inject crashes on both sides of rollback
stage, rename, restored receipt, delete intent, unlink, and delete-restored
receipt, including repeated recovery attempts.

### Concurrent external-writer boundary

**APPROVE with explicit disclosure.** The per-primary kernel lease serializes
cooperating TORQ installations, while the held root/parent handles, nofollow or
no-reparse traversal, content and identity checks, signed stage identities, and
post-result verification reject drift that the transaction observes. These
controls do not provide a portable atomic compare-and-replace of the destination
file identity. A non-TORQ process can replace a target after the final identity
check and before the anchored rename, causing that racing edit to be displaced.

Apply and recovery therefore require other editors, formatters, watchers, and
tools that can write the project to remain paused for the operation. The contract,
README, UI, and tests must state this precondition and must not claim protection
against arbitrary concurrent external writers. `Third-party edits are never
overwritten` applies only to preexisting or otherwise observed third states that
fail the anchored content/identity checks. This limitation does not permit any
weakening of root or parent anchoring, staged provenance, revalidation, or
recovery refusal for an observed mismatch.

## Builder and G2A release gate

Builder may begin within this contract. Its first executable gate is the native
isolated fixture: prove anchored replacement, cross-installation exclusion,
injected crashes at every journal/evidence/write boundary, exact application,
exact restoration or persistent block, and sealed cross-chain projection before
enabling UI Apply. The complete integration must then exercise review ->
correction -> child -> Accept with unchanged source -> Apply exact bytes ->
reload/history -> Continue using the real fixture provider, checker, receipt
authority, and native primary writes.

G2A must independently verify the contract's tamper, stale-source, link/reparse/
hardlink/case, third-state recovery, idempotent replay, cross-installation,
session/origin, UI race/accessibility, package, platform, lint/type, full-suite,
and installed-wheel gates. Fixture execution is not live-vendor validation, and
no live paid-provider claim is authorized or needed for this P3 release.
