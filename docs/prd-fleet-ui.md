# PRD - TORQ Fleet UI

Status: **Release 1 Fleet Read implemented on protected main.** Revised 2026-07-25, Rev 5.5.

The Release 0 gate in Section 11 has passed. Release 1 completed protected-main
CI on Windows, macOS, Linux, and headless Linux after merge. Release 2
accounting and Release 3 control remain gated by their later build-order steps.

Rev 5 closes gaps found reviewing Rev 4 against the code: certificate role
binding, the missing writer for post-action closure, key storage boundaries,
snapshot read order, canonical encoding, unresolvable attempts, and a meter that
tampering could lower. Changes are listed in Section 16.

Rev 5.1 resolves five contradictions in Rev 5 where a requirement could not be
satisfied as written: an unreachable lane state, key isolation that did not
isolate, a projection rule that never said which receipts it projects, a
coverage meter with no denominator, and a decryption criterion no cipher in the
package can fail. It replaces per-role key files with a local evidence broker.
Changes are listed in Section 17.

Rev 5.2 completes the recovery writer Rev 5.1 authorized but never gave an
identity, makes its preconditions checkable by an offline verifier, narrows
run-decision authority, and adds the durability and anti-rollback properties the
broker and registry depend on. Changes are listed in Section 18.

Rev 5.3 changes no requirement. It reconciles the document with `origin/main` at
56b8bff, where PRs #15-#19 landed seven of the properties this PRD listed as
outstanding, and re-derives every code citation against that commit. Changes are
listed in Section 19.

Rev 5.4 implements build-order steps 0-7, closes canonical-encoding decision 6
in favor of the pinned standard-library encoder, and adds the Release 0
adversarial fixtures. Fleet Read UI implementation remains the next release
step; Release 2 accounting and Release 3 control dependencies remain gated.

Rev 5.5 implements build-order step 8 as a wheel-bundled, loopback-only Fleet
Read board. It adds the six-lane governance rail, orchestrator and action
ledgers, in-place evidence detail, persistent monitor, deduplicated local
notifications, secure static routes, responsive and reduced-motion treatments,
and real-browser verification. Release 2 accounting and Release 3 control
remain gated.

Depends on:

- [`architecture/fleet-backend-contract.md`](architecture/fleet-backend-contract.md)
- [`architecture/plan-entitlement-accounting.md`](architecture/plan-entitlement-accounting.md)
- [`architecture/context-injection-contract.md`](architecture/context-injection-contract.md)

## 1. Product decision

TORQ Fleet is a local, attention-oriented control surface for one governed run.
It answers: what is happening, and what needs the operator?

Fleet renders deterministic state from authenticated evidence. Operational data
may annotate that state but cannot silently change it. Provider stdout is never
a Fleet data source.

The operator must be able to close Fleet without stopping the run, receive one
notification per action, return to identical state, and distinguish refusal,
dispatch attempt, failure, interruption, and completion without opening a
terminal.

## 2. Product principles

1. **Evidence first.** Every governed claim names a verified receipt field.
   Unsupported values render `unknown`.
2. **Pure reduction.** The same verified evidence prefix always produces the
   same normalized state.
3. **Operations are annotations.** Heartbeats, leases, notification delivery,
   and UI preferences are not evidence.
4. **Attempt is not receipt.** Invoking a transport does not prove the provider
   received or accepted a request.
5. **Sequential means sequential.** The core path is
   `g1d -> g1r -> builder -> g2a`; repair lanes are conditional and `g2a` may
   repeat.
6. **No cross-purpose keys.** Trust, writer signing, manifest signing, and
   artifact encryption use separate keys.
7. **Fail closed without false alarms.** Tampering suppresses evidence-backed
   values; a valid active run is not incomplete merely because it is unsealed.

## 3. Users, releases, and non-goals

**Primary user:** one operator running a governed change on their own machine.
Runs may mix subscription-covered and metered providers.

### Release 1 - Fleet Read

- Supervisor-owned background runs.
- Board, orchestrator card, four core rows, and two dormant repair rows.
- Attempt history, blocked/failure/interruption detail, and run-level actions.
- Active and sealed verification.
- Persistent mini monitor and deduplicated notifications.

### Release 2 - Accounting

- Entitlement-account quota meters across verified runs.
- Metered-equivalent, direct-billed, and pricing-coverage figures.
- Reservation expiry and explicit reconciliation.

### Release 3 - Control

- Durable context commands applied at attempt boundaries.
- Lead-brain replan and confirmed direct-lane routing.
- Governed text and supported-file artifacts.

### Out of scope

- Remote or multi-user live runs, mobile, and run-history search.
- Automatic retry after uncertain transport invocation.
- Remote key escrow or artifact decryption.
- Interactive lane Attach. One-shot provider sessions require a separate
  persistent-session transport contract.
- Post-hoc audit workflows already served by TORQ Console.

## 4. Local runtime and security model

`torq fleet` is loopback-only. A supervisor, not the Fleet window, owns each
worker.

**SR-1.** Closing every Fleet window does not stop a registered run.

**SR-2.** The supervisor exposes run ID, lifecycle, heartbeat, worker identity,
last covered receipt sequence, and open actions. This state is atomic and
explicitly non-evidentiary.

**SR-3.** Worker death creates an operational `recovery_required` annotation.
Reduced state changes only when authenticated evidence records interruption.

**SR-3a.** A run with no live worker and no terminal decision is annotated
`orphaned`. Like `recovery_required`, this is operational: it marks a run that
needs operator or recovery attention and changes no reduced state. It is the
signal Fleet surfaces after a double death, where no writer survived to record
what happened (LC-1).

**SR-4.** The supervisor is authorized to request only `stage_interrupted` and
the linked terminal failure decision, under its supervisor capability. It never
asserts provider observation and never automatically retries uncertain transport
attempts.

**SR-4a.** A recovery writer, invoked by the operator, is authorized to request
`run_abandoned`. It is the only path by which an unterminated attempt becomes
`abandoned` in reduced state (VC-6), and it never asserts dispatch, provider
observation, or an outcome for any attempt.

The receipt carries `evidence_basis: submitted`, not `derived`. The claim that
no worker is alive is an operator assertion. It is not observed by the writer
and cannot be derived from evidence, and no independently authenticated
operational attestation exists in this design to make it derivable. Recording it
as `derived` would misrepresent an operator's word as a chain-supported
inference, which is the exact failure FR-5 exists to prevent.

The receipt records the last covered sequence and **enumerates by ID every
attempt it closes**. Enumeration is what makes the receipt self-contained: a
verifier reading an export can check the enumerated set against the chain
without knowing anything about process liveness.

**SR-4b.** `run_abandoned` is a terminal run event. It terminalizes exactly the
attempts it enumerates, sets the workflow state to `workflow_abandoned`, seals
the manifest, and forbids any subsequent receipt in that run. A receipt
appearing after it is `tampered`. A run cannot be un-abandoned; a later decision
to continue the work is a new run, linked by reference (SC-3).

**SR-5.** Every HTTP request validates an exact loopback `Host` and presents a
random, short-lived capability token, read routes included. Mutation routes
additionally require exact same-origin validation. Binding to loopback and
checking `Origin` alone are not authentication.

Requiring the token on reads as well is deliberate. Any local process that can
open a socket can otherwise read run identity, provider bindings, refusal
reasons, and settlement figures from `/api/v1/fleet`, which the loopback bind
does nothing to prevent.

`/healthz` is the single unauthenticated route. It returns a fixed-shape
liveness response and nothing else: no run ID, no counts, no verification
finding, no lifecycle state. A verification finding is itself information about
the run, so Rev 5's version of this route leaked across the boundary it was
exempted from.

**SR-5a.** Delivery is specified, because a browser navigating to a page cannot
attach a bearer header and an unimplementable requirement is not a control. Two
distinct values are involved and they are not interchangeable:

| Value | Lifetime | Where it may appear |
|---|---|---|
| bootstrap nonce | single use, expires on first exchange or short timeout | the launch URL, and consequently shell history and browser history |
| session token | idle and absolute expiry, rotatable | `HttpOnly` cookie only; never a URL, log, or receipt |

`torq fleet` mints a nonce and prints the launch URL carrying it. The bootstrap
response spends the nonce, redirects to a clean URL, and sets the session token
as a cookie that is `HttpOnly`, `SameSite=Strict`, host-scoped, and path-scoped
to the Fleet routes. Same-origin `fetch` carries it automatically; no other
origin can read it.

The nonce is expected to persist in shell and browser history, and a redirect
does not reliably erase the entry a browser already recorded. That is acceptable
precisely because a spent nonce grants nothing. The requirement SR-6 enforces is
narrower and achievable: **no reusable session credential ever appears in a
URL.** Rev 5.1 claimed no token enters any persisted URL while simultaneously
putting one in the launch URL; splitting the values resolves that contradiction
rather than restating it.

Fleet pages set `Referrer-Policy: no-referrer`, so neither value nor any run
identifier reaches an external navigation. Session tokens rotate on
privilege-relevant transitions and are invalidated when the server restarts.

**SR-5b.** Reaching `workflow_closed` or `workflow_abandoned` downgrades the
session to read-only; it does not invalidate it. Closure is the moment the
operator most needs the final state, the settlement figures, and the terminal
decision on screen. Ending the session there would black out the display at
exactly the point the run becomes worth reading. Mutation routes refuse after
closure regardless of session state.

**SR-6.** Session tokens and broker capabilities never enter receipts, logs,
notifications, evidence exports, or any URL. Bootstrap nonces may appear in a
launch URL and its history entries, and nowhere else. Responses use `no-store`,
`nosniff`, `Referrer-Policy: no-referrer`, and a restrictive CSP.

## 5. Cryptographic trust and key separation

Cross-run trust uses a root public trust anchor; it does not reuse one private
key for every run.

**TC-1.** The evidence-root identity certifies per-run public keys. Each run has
separate manifest, orchestrator-writer, supervisor-writer, operator-gateway, and
recovery-writer identities.

All five are minted at run creation, including recovery. Recovery cannot be
issued on demand at the moment it is needed: it is needed precisely when every
process belonging to the run is dead, and a certificate minted then would be
minted by whatever process claims a run needs recovering. Minting at creation
also means a run created before this contract exists can never be recovered, and
must be closed by operator reconciliation rather than by `run_abandoned`.

A certificate binds exactly one public key to exactly one `(run_id, role)` pair
and carries: certificate schema version, `run_id`, `role`, `writer_key_id`,
the public key, the issuing root key ID, and the root signature over that body.
The binding is what makes TC-4 enforceable: without `run_id` in the certificate,
a certified writer from one run could sign into another; without `role`, any
certified key could claim any writer role.

Revocation *infrastructure* is out of scope: no CRL, no OCSP, no distribution
mechanism. The party who needs revocation here is the operator responding to a
key they believe leaked, not an attacker, so the Rev 5 argument that an attacker
able to revoke could already rewrite the anchor was answering the wrong actor
and is withdrawn.

What Release 1 does define is compromise response, and it distinguishes routine
rotation from compromise. A retired root carries one of two states:

| State | Cause | Aggregation | Display |
|---|---|---|---|
| `trusted_legacy` | routine rotation; no compromise suspected | included, at full weight | labeled with its root, no warning |
| `distrusted_compromised` | key believed leaked or misused | excluded; counted in the coverage denominator | flagged; affected runs listed |

Rev 5.1 excluded every retired root from aggregation. That turns ordinary key
rotation into a self-inflicted outage: coverage drops below 100 percent, AR-4b
fails closed, and the operator cannot dispatch until the rolling window ages
out the pre-rotation runs. Rotation is meant to be routine, so it must not cost
availability. Only suspected compromise should, because there the reduced
confidence is real.

A new root identity is issued for subsequent runs in both cases. Sealed evidence
is never re-signed under the new root: rotation moves forward only.

**TC-2.** Each receipt carries:

- `writer_role`: `orchestrator`, `supervisor`, `operator_gateway`, or `recovery`
- `evidence_basis`: `observed`, `derived`, or `submitted`
- `writer_key_id`
- `writer_signature`

The writer signature covers the canonical receipt body excluding
`writer_signature` and `receipt_hash`. `receipt_hash` covers that body plus the
writer signature. The next receipt links to that hash.

"Canonical" is one named function, not a convention. The choice is made before
schema v2 freezes, because it is the input to every signature and hash below it
and cannot be revised afterward without invalidating sealed evidence. The
options are RFC 8785 (JCS) or exactly `json.dumps(value, sort_keys=True,
separators=(",", ":"), ensure_ascii=True)`; decision 6 records the tradeoff.
Whichever is chosen, the stored JSONL line and the signed body derive from the
same function, and the pinned choice is recorded in an import oracle so drift is
a test failure.

The **receipt** path now satisfies this. `_canonical` (`receipts.py:94-100`,
`origin/main` at 56b8bff) is a single function serving both the hash
(`receipts.py:729-730`) and the persisted line (`receipts.py:801`), so stored
bytes and hashed bytes are identical. It implements the `json.dumps` option
above; decision 6 remains open only in the sense that adopting JCS instead would
now require changing a landed function before schema v2 freezes.

Two paths still dual-encode. The run certificate is signed over
`_canonical(body)` (`receipts.py:706`) but persisted as
`json.dumps(signed, sort_keys=True)` (`receipts.py:710`), and the manifest is
signed over `_canonical(manifest)` (`receipts.py:843`) but persisted the same
non-canonical way (`receipts.py:847`). Both are then hashed *as persisted* —
`certificate_hash` at `receipts.py:838` digests the file bytes, not the canonical
bytes. This is not unverifiable: a verifier can parse and re-serialize. The cost
is that an independent implementation must know that for these two objects the
persisted bytes are not the signed bytes, which is precisely the assumption it is
most likely to get wrong, and the drift is silent when it does. Extending
`_canonical` to the certificate and manifest writers removes the class of error
rather than documenting it.

**TC-3.** The signed rolling manifest covers run ID, receipt count, terminal
receipt hash, schema version, and sealed state. Its per-run manifest key is
certified by the evidence-root identity.

**TC-4.** The verifier enforces a transition matrix, not a role table. Each row
is admissible only if every column holds; any violation is `tampered`, not
merely unreadable.

| Writer | Transition | Evidence basis | Required prior state | Referenced sequence |
|---|---|---|---|---|
| orchestrator | `run_planned` | `observed` | no prior `run_planned` | none |
| orchestrator | `stage_attempt_created` | `observed` | lane in catalog; no open attempt for that lane | none |
| orchestrator | `stage_dispatch_started` | `observed` | its attempt created and unterminated | that attempt creation |
| orchestrator | `stage_blocked` | `observed` | its attempt created, unterminated, undispatched | that attempt creation |
| orchestrator | `stage_completed`, `stage_failed` | `observed` | its attempt created and unterminated | that attempt creation |
| orchestrator | `repair_routed` | `derived` | target lane dormant; qualifying defect sealed | the stage result that justifies it |
| orchestrator | `run_decision: completed` | `derived` | every required lane sealed `stage_completed`; zero open actions; no terminal decision | none |
| orchestrator | `run_decision: blocked`/`failed` | `derived` | a qualifying terminal `stage_blocked`/`stage_failed` sealed; no terminal decision | that stage result |
| orchestrator | `run_decision: awaiting_approval` | `derived` | execution complete; at least one open action; no terminal decision | the `action_opened` |
| supervisor | `stage_interrupted` | `derived` | attempt created and unterminated | the attempt creation it closes |
| supervisor | terminal failure decision | `derived` | its own `stage_interrupted` sealed; no terminal decision | that `stage_interrupted` |
| operator gateway | `context_injected` | `submitted` | run not closed | its command ID |
| operator gateway | `action_resolved` | `submitted` | referenced action opened and unresolved | the `action_opened` |
| operator gateway | terminal run decision | `derived` | `execution_complete_action_open`; zero open actions after this resolution | the `action_resolved` that closed the last one |
| recovery | `run_abandoned` | `submitted` | no terminal run decision; at least one created, unterminated attempt; enumerated set equals exactly the unterminated attempts | the last covered sequence |

A role table alone constrains too little. Without the prior-state and referenced
-sequence columns, the operator gateway could seal a terminal decision on a run
with actions still open, or one that never reached execution completion, purely
because "terminal run decision" appeared in its permitted set. The columns are
what make the permission specific to a situation rather than to a verb.

Rev 5.1 carried one `run_decision` row admitting any decision whenever none was
sealed, and one row covering attempts, routing, and stage results together. Both
were too coarse for a security boundary: the first let the orchestrator seal
`completed` on a run whose lanes had failed, and the second let a stage result
be written for an attempt that was never created. Each decision and each
transition now carries its own preconditions.

**Every precondition in this matrix is checkable from the receipt chain alone.**
No row depends on process liveness, wall-clock time, or any operational
annotation, because an offline verifier reading an exported run has access to
none of those. The recovery row is the case that forced the rule: Rev 5.1
required prior state `orphaned`, which SR-3a defines as explicitly
non-evidentiary, so no verifier could ever check it and the row's `tampered`
consequence was unenforceable. Orphan status is now the operator's reason for
invoking recovery, not a cryptographic precondition of it.

**TC-4a.** The matrix is maintained as a machine-readable specification, and the
verifier and broker are both driven from it rather than from independent
hand-written checks. Conformance tests are generated to exercise every row
positively and every precondition negatively. Hand-picked violation samples do
not establish that a matrix is enforced; they establish that the sampled rows
are.

The role check is two-sided. The verifier resolves `writer_key_id` to its
certificate, verifies the signature against the certified public key, and then
requires the receipt's declared `writer_role` to equal the role bound in that
certificate. A receipt whose declared role differs from its certified role is
`tampered`. Without this, `writer_role` is self-asserted and the matrix
constrains nothing: any certified writer could claim any role.

The operator gateway holds the post-action terminal decision because LC-7
requires a run to reach `workflow_closed` after an action resolves, and no other
writer is authorized to do it. The orchestrator has already exited by then, and
the supervisor may write only the terminal failure linked to an interruption it
observed.

**TC-5.** Every run has an independent random artifact-encryption key. It is not
an Ed25519 private key and is not reused across runs. Root-key rotation and
per-run certificate identity are retained in exports.

Artifact encryption is authenticated: AES-256-GCM or ChaCha20-Poly1305, with a
unique nonce per artifact and the run ID bound in as associated data. Decryption
under the wrong key fails with an authentication error rather than returning
bytes.

The current construction is still an HMAC-SHA256 keystream XORed against the
plaintext (`receipts.py:812-822`) with no tag. Its ciphertext integrity is not
actually unprotected — the receipt chain seals the `.enc` file's SHA-256 and
reverifies it at `receipts.py:939` — so the authentication gap is a testability
requirement rather than a live integrity hole. An unauthenticated cipher cannot
distinguish "wrong key" from "plaintext that happens to be garbage", which makes
the cross-run isolation criterion in Section 13 unassertable.

The key-separation half of this requirement is **satisfied as of 56b8bff**. The
`RunKeys` record (`receipts.py:477-483`) holds five independent per-run secrets;
the artifact keystream is keyed from `run_keys.artifact` (`receipts.py:817`) and
the manifest signature from `run_keys.manifest` (`receipts.py:840`). Artifact-read
access and manifest-forgery access are no longer the same capability. What
remains outstanding here is the AEAD construction itself.

**TC-6.** Supervisor-derived interruption records lease ID, expiry, worker
identity, last covered sequence, and `provider_dispatch: unknown` unless verified
evidence establishes `false` or `true`.

**TC-7.** No **client writer** process holds a private key. A single local
**evidence broker** owns every signing key, the manifest key, and the artifact
key for every run, and is the only process that touches the receipt store.
Workers, supervisors, gateways, and the recovery writer are all clients of it.

Rev 5 required per-role key files with owner-only permissions. That does not
enforce the threat model it was written for: a compromised worker running as the
operator's own OS user can read any file that user owns, including the
supervisor's key, and can then forge the interruption evidence FR-5 presents as
independent inference. Filesystem permissions separate users, and every writer
here is the same user.

The broker also resolves a problem Rev 5 created and did not address. Three
writer roles in three processes appending to one hash chain means three private
views of the chain head. `ReceiptChain` serializes with an in-process lock and
holds the sequence number and previous hash in memory, which is correct for one
writer and unsound for three: concurrent appends race on sequence allocation and
on the rolling manifest that TC-3 says only one key may sign.

The broker:

- Owns all private signing keys, the manifest key, and artifact keys. No key
  material is readable by any worker, supervisor, or gateway process.
- Exposes capability-scoped append operations. A caller presents a capability
  naming its role; it never presents a key and never chooses its own
  `writer_role`, which the broker stamps from the capability.
- Enforces the TC-4 matrix at write time — role, transition, evidence basis,
  prior state, and referenced sequence — so unauthorized evidence is refused at
  the source rather than only detected at verification.
- Serializes receipt append, artifact write, and manifest replacement into one
  ordered commit, making the WC-1 ordering invariant a property of the
  implementation rather than a convention writers must observe.

**TC-7a (capability contract).** Withholding keys is not by itself isolation: a
capability that authorizes appends is a credential, and a compromised worker
that steals one gains exactly what the key would have given it. Capabilities
are therefore issued per run and per role, delivered once at client start over
the broker's authenticated channel and never through argv, environment, or a
file readable after start; bound at issue to the client's process identity and
rejected if presented by another; short-lived with explicit renewal; and
single-use per append with a monotonic counter, so a captured message cannot be
replayed.

**TC-7b (stated limit).** None of this defends against a compromised process
running as the operator's own user that can read the memory of a live client
holding a valid capability. Same-user process isolation is not a boundary that
capabilities, key custody, or file permissions can create. The design reduces
the window — the capability is short-lived, role-scoped, replay-protected, and
never covers the supervisor's or recovery writer's transitions — but it does not
close it. Decision 7 is where that residual risk is either accepted or paid down
by running the broker under a separate OS identity.

This makes the broker the single most security-critical component in the
product. It runs as its own process with its own lifetime, it is the only holder
of the keys, and its compromise is equivalent to compromise of the whole
evidence chain.

Two facts this changes: `FileRunKeyStore` today issues one key per evidence root
and discards the run ID, so every writer in every run under that root already
shares one private key. And the artifact key is never derived from any signing
key.

## 6. Receipt schema and compatibility

Rev 4 defines receipt schema `2.0.0`.

Every receipt includes `sequence`, UTC `observed_at`, transition, previous hash,
schema/profile/policy versions, writer metadata/signature, and receipt hash.
Sequence remains authoritative when clocks disagree.

Every attempt receipt includes role, unique `attempt_id`, per-role
`attempt_ordinal`, and repair cycle where applicable.

`run_planned` seals the complete lane catalog: sequential execution, four core
roles, two conditional roles, and each lane's kind, provider/model binding,
prompt ID/version, and contract ID/version. Conditional lanes begin `dormant`;
`repair_routed` activates one and names its cycle and attempt.

### 6.1 Legacy evidence

Schema-v1 evidence remains verifiable under its original contract.

**SC-1.** A v1 receipt missing writer or attempt fields renders
`legacy_unclassified`; it is not rejected as a malformed v2 receipt.

**SC-1a.** A v1 run has no certified recovery identity, so `run_abandoned` is
unavailable to it. An orphaned v1 run is closed by operator reconciliation
recorded outside the chain, never by appending a v2 receipt to a v1 run, which
SC-3 forbids. The same applies to any v2 run created before the recovery
identity was minted at creation (TC-1).

**SC-2.** Fleet may show v1 lane and settlement data supported by the old
receipt. It suppresses writer attribution, attempt-level guarantees, and elapsed
time when unavailable.

**SC-3.** Schema versions never mix inside one run. Migration creates a derived
report or linked successor run; it never rewrites sealed receipts.

## 7. Attempt and action lifecycle

### 7.1 Attempts

| Transition | Meaning | Terminal | Dispatch value |
|---|---|---|---|
| `stage_attempt_created` | attempt allocated before preflight | no | `false` |
| `stage_blocked` | preflight refused the attempt | yes | `false` |
| `stage_dispatch_started` | transport invocation attempted | no | `true` |
| `stage_completed` | response parsed and passed pinned contract | yes | `true` |
| `stage_failed` | attempt failed after creation | yes | actual value |
| `stage_interrupted` | worker or supervisor resolved interruption | yes | `false`, `true`, or `unknown` |
| `run_abandoned` | recovery writer closed an orphaned run | yes, run-level | `unknown` |

**LC-1.** Every created attempt resolves to exactly one terminal transition.

This is a writer obligation, and the reducer must tolerate it being false. The
one case that violates it is unavoidable: a worker killed between
`stage_attempt_created` and any terminal transition, whose supervisor is then
also killed before writing `stage_interrupted`. No writer survives to close the
attempt.

Such a run stays `live_verified`. Its evidence is complete, signed, and covered
by a valid rolling manifest; the only thing wrong with it is that nothing is
alive to continue it, and liveness is not an evidence property (VC-5). Rev 5
routed this case through `incomplete`, which the writer never produces — the
manifest is replaced after every append (`receipts.py:805`), so a valid manifest
always covers the last receipt. That made `abandoned` unreachable.

The correct sequence is: the lane stays `running` in reduced state, the
supervisor's absence raises the operational `orphaned` annotation (SR-3a), and
the lane becomes `abandoned` only when the certified recovery writer seals
`run_abandoned` enumerating it (SR-4a). Evidence changes state; the absence of a
process does not. An unterminated attempt in a `sealed_verified` run remains a
genuine reduction error, because a sealed chain asserts its writers finished.

The orphan annotation is the operator's cue to invoke recovery. It is not a
precondition the verifier checks, because it cannot be: liveness leaves no trace
in an export. What the verifier checks is that the enumerated attempts were in
fact created and unterminated at that point in the chain (TC-4), which is
provable from the receipts alone.

**LC-2.** `stage_completed` is written only after parsing, model attestation,
contract validation, and artifact persistence succeed.

**LC-3.** Timeouts, transport errors, malformed responses, off-contract output,
artifact failures, and unexpected governed exceptions write `stage_failed` when
the writer remains available.

**LC-4.** `provider_dispatch: true` means transport invocation was attempted. It
does not claim network egress, provider receipt, billing, or completion.

**LC-5.** A supervisor terminal decision references the interruption sequence it
closes. It cannot create an unrelated business decision.

### 7.2 Operator actions

Actions have an evidence lifecycle:

- `action_opened`: action ID, type, scope, target, caused-by sequence, summary
- `action_resolved`: action ID, resolution, resolver identity, resolved sequence

**LC-6.** Open-action count is opened minus resolved IDs in verified evidence.

**LC-7.** Execution completion and workflow closure are distinct. A run may be
`execution_complete_action_open`; it becomes `workflow_closed` only after the
action resolves and a terminal run decision is sealed.

That closing decision is written by the operator gateway under TC-4 and
references the `action_resolved` sequence it closes. Naming the writer is not a
detail: the orchestrator has exited by the time the action resolves, so a
contract that requires a sealed terminal decision without authorizing anyone to
write it leaves every action-gated run permanently unclosable.

**LC-7a.** A run reaches exactly one terminal workflow state:

| Workflow state | Reached by | Manifest |
|---|---|---|
| `workflow_closed` | terminal `run_decision` with no open actions | sealed |
| `workflow_abandoned` | verified `run_abandoned` (SR-4b) | sealed |

`workflow_abandoned` is terminal in the same sense as `workflow_closed`: no
receipt may follow it, and it cannot be reversed. It differs in what it claims —
closure asserts the run finished, abandonment asserts only that it stopped and
nobody recorded why.

**LC-8.** Legacy sealed `run_decision: awaiting_approval` maps to
`legacy_execution_complete_action_open`; its resolution must be linked rather
than appended by reopening the sealed chain.

Notification delivery is presentation state keyed by run ID plus action ID. It
does not determine whether an action is open.

## 8. Active verification and deterministic reduction

The writer atomically replaces signed `terminal-manifest.json` after each
receipt. It uses `sealed: false` while active and `sealed: true` only at workflow
closure.

| Verification state | Meaning |
|---|---|
| `live_verified` | unsealed manifest verifies the covered prefix |
| `live_catching_up` | store has complete receipts beyond manifest coverage |
| `sealed_verified` | sealed manifest verifies the complete chain |
| `incomplete` | no valid manifest covers the evidence at all |
| `tampered` | signature, hash, permission, artifact, writer authorization, or covered-length check fails |
| `unreadable` | bytes or supported schema cannot be parsed safely |

**VC-1.** Manifest ahead of store is `tampered`, never catching up.

**VC-2.** One unterminated JSONL fragment may be ignored only beyond manifest
coverage. A fragment, gap, or shortfall inside coverage is tampering.

**VC-3.** Verification and projection use one stable snapshot, and **Fleet
projects exactly the manifest-covered prefix.** Receipts in the store beyond
that prefix are not reduced, not displayed as lane state, and not counted. They
are reported as verification lag.

This is the rule Rev 5 omitted. It pinned a read order but never said which
receipts get projected, which left the tail — signed by a writer but not yet
covered by any signed manifest — eligible for reduction. Projecting it would put
unattested evidence on screen in exactly the state, `live_catching_up`, that
exists to say attestation has not caught up yet. Projecting only the covered
prefix makes the displayed state signed by construction.

Read order is then a consequence, not the fix: manifest first, then store. With
WC-1 below, the manifest read second can never cover receipts the store read
missed, so the false `tampered` disappears without locking, retries, or a
generation check. A re-read-and-compare scheme would additionally starve on an
actively appending run and could never observe `live_catching_up` at all, since
it accepts only reads where nothing was appended.

The read order landed in 56b8bff: `fleet.py:445-446` now reads the manifest
first. The projection rule did not. `fleet.py:455` still requires
`manifest.receipt_count == len(receipts)` exactly, so an append between the two
reads still reports `manifest_coverage_mismatch` on a healthy run. Replacing that
equality with the covered-prefix slice above is the remaining work.

**WC-1 (writer ordering invariant).** The broker commits a receipt to durable
storage before replacing the manifest that covers it. The store is therefore
never behind the manifest, which is what makes manifest-ahead unambiguous
tampering (VC-1) rather than a benign race. Every reader guarantee above depends
on it, and Rev 5.1 assumed it rather than stating it.

Durable means crash-durable, not merely ordered in source. The full commit is:

1. Append the receipt line; flush; `fsync` the receipt file.
2. Write the manifest to a temporary file; flush; `fsync` the temporary file.
3. Atomically replace the manifest with it.
4. `fsync` the containing directory where the platform supports it.

Steps 2 and 4 are what make the ordering survive power loss. Without them the
replace may become visible while the temporary file's contents have not reached
disk, producing a manifest that is present, current-looking, and truncated.

Rev 5.1 claimed this invariant already holds in the code. That was too strong,
and it remains too strong at 56b8bff. `receipts.py:797-805` has the correct
logical order and does `fsync` the receipt, but `_write_manifest`
(`receipts.py:847-849`) writes the temporary file and calls `os.replace` with no
flush of either the file or the directory. **The current implementation has the
ordering but has not established crash durability.**

**WC-1a.** Release 0 includes crash-injection fixtures that interrupt the commit
between each numbered step and assert the result is `live_catching_up`,
`unreadable`, or `incomplete` — never `tampered` and never a silently truncated
manifest that reads as valid.

**VC-4.** The reducer folds verified receipts in sequence order. Counts are
current lane states, not receipt totals. Repeated `g2a` and repair attempts remain
inspectable but contribute one current state per lane.

**VC-5.** Lease expiry annotates a running row as recovery-required but does not
change reduced state. Only a verified interruption receipt changes the state.

**VC-6.** A lane whose latest attempt was created but never terminated reduces to
`abandoned` only when a verified `run_abandoned` receipt (SR-4a) enumerates that
attempt. A lane not enumerated is unaffected. In every other case, including
`live_verified` on a run whose processes are all dead, the lane remains
`running` and the condition is carried as the operational `orphaned` annotation.

No verification state, on its own, converts an unterminated attempt into
`abandoned`. Only evidence does. This keeps VC-5's rule intact: process liveness
never moves reduced state, and `abandoned` is a claim some writer signed rather
than an inference Fleet drew from silence.

**VC-7.** A verified `run_abandoned` puts the run in `workflow_abandoned` and
seals the manifest. Any receipt sequenced after it is `tampered`, and the
reducer stops there rather than folding what follows.

## 9. Release 1 requirements - Fleet Read

Fleet renders four core and two conditional rows in sealed catalog order.

| State | Visual | Evidence |
|---|---|---|
| dormant | faint grey | conditional lane not activated |
| queued | grey | lane without attempt |
| running | gold | open created/dispatched attempt |
| sealed | sage | latest attempt completed |
| needs you | burnt orange | open lane action |
| failed | red plus `x` glyph | latest attempt failed |
| interrupted | violet/red outline plus `!` glyph | verified interruption |
| abandoned | slate plus `?` glyph | verified `run_abandoned` enumerating this lane's attempt |

`abandoned` is visually distinct from both `failed` and `interrupted`. It says
less than either: nobody survived to record what happened, so the outcome is
unknown rather than known-bad. Its detail view says `Attempt outcome unrecorded`
and offers no automatic retry, for the same reason FR-4 refuses retry on unknown
dispatch.

A `running` lane in an `orphaned` run is not shown as abandoned. It keeps its
running treatment and carries the orphan annotation, with the recovery action
offered as an open operator action. Reduced state moves only when the operator
takes it and the recovery writer seals `run_abandoned`.

**FR-1.** The orchestrator card shows run identity, execution/workflow state,
lane catalog, reducer tallies, open actions, and verification coverage.

**FR-2.** A reducer disagreement displays `state reduction error`; it never
falls back to a plausible count.

**FR-3.** Rows show role, state, attempt ordinal, latest verified sequence, and
one-line summary. Expansion stays in place and shows attempt history.

**FR-4.** Preflight refusal says `No provider transport was invoked`. A later
failure says `Provider transport attempted`. Unknown dispatch says
`Dispatch status unknown` and offers no automatic retry.

**FR-5.** Supervisor-derived interruption is labeled as inference and displays
its lease evidence. Writer and evidence basis appear in detail.

**FR-6.** Every TORQ screen shows verification, run state, six pips, elapsed
time, and open-action count. Live elapsed time is operational and labeled;
historical elapsed is approximate and suppressed for non-monotonic timestamps.

**FR-7.** One action generates at most one notification across restart/replay.
Notification text contains no decrypted content, secrets, or capability tokens.

**FR-8.** Artifact viewing is optional and local. Decryption failure is separate
from receipt integrity. Fleet never tails provider stdout.

## 10. Release 2 and 3 requirements

### 10.1 Accounting

Fleet labels figures `Metered equivalent`, `Direct billed this run`, and
`Pricing coverage`; it never labels marginal subscription cost as `Your cost`.

Every entitlement figure is labeled **TORQ-observed usage**. TORQ can account
only for dispatches it performed and receipted. Usage of the same provider
account through TORQ Console, a vendor web console, another machine, or a direct
API call is invisible to it. A meter presented as account consumption would be
wrong whenever the operator uses the account anywhere else, which for a
subscription plan is the normal case, so the qualifier is part of the label and
not a footnote.

**AR-1.** Every pricing attempt seals rate-table version and SHA-256, including
rate misses. Referenced tables are immutable.

**AR-2.** Monetary values are sealed as decimal strings or integer nanos, never
binary JSON floats. Unrounded attempts are summed and rounded once for display.

**AR-3.** Unknown rates yield `metered_usd: null` and `rate_unknown`, displayed
as `unpriced`. Legacy unsplit usage is excluded, never imputed.

**AR-4.** Entitlement meters aggregate only verified eligible runs whose run-key
certificates chain to the selected root trust anchor. They show verified coverage
as numerator and denominator.

**AR-4a (dispatch registry).** Coverage is measured against a root-level,
append-only run registry stored outside any individual run directory and signed
by the broker. A run is enrolled at creation, before its first dispatch. The
registry records: entitlement account, run ID, root key ID, rolling window, and
enrollment time.

Without it, the fail-closed rule below is unenforceable. Rev 5 asserted that
deleting a run's evidence could not read as freed quota, but deleting the run
directory removes the usage *and every trace the run existed* — the denominator
falls with the numerator and coverage stays at 100 percent, so the refusal never
fires. The registry is the only durable record that a run was supposed to be
there, and it is what makes a missing run detectable at all.

Registry entries with no verifiable evidence resolve to `missing`, `deleted`, or
`unverifiable`. Each is counted in the coverage denominator and carries a
conservative reservation for the usage that cannot be read.

`expired` is different and Rev 5.1 had it wrong. An entry whose rolling window
has closed leaves the active-window denominator entirely and releases its
reservation, because AR-5 already says reservations expire with their window.
Keeping expired entries counted contradicted AR-5 and would have ratcheted
coverage permanently downward as runs aged out, eventually making AR-4b refuse
every dispatch.

**AR-4c (anti-rollback).** Signing entries detects modification but not deletion
of the registry or restoration of an older, still-validly-signed copy. The
registry therefore maintains a hash-chained head, and the broker anchors the
current head outside the mutable journal, alongside the trust anchor and under
the same protections. Verification compares the journal's computed head against
the anchored head.

A registry that is missing, whose head does not match, or whose head is an
ancestor of the anchored head fails closed: AR-4b refuses, and the condition is
reported as `registry_rollback_detected` rather than as reduced coverage. A
rolled-back registry is not a run with weaker evidence; it is an attack on the
denominator itself, and treating it as ordinary missing coverage would let it be
reconciled away. Recovery is explicit operator reconciliation under AR-6.

**AR-4b.** Any preflight consuming these meters fails closed with
`entitlement_coverage_incomplete` when coverage is below 100 percent. Excluding
unverifiable runs is correct for display and dangerous for enforcement: an
attacker who corrupts or deletes a run's evidence lowers recorded consumption, so
a preflight trusting the reduced total would treat tampering as freed quota.
Display continues to show the partial figure against its denominator.

**AR-5.** Used, reserved, and limit remain separate with independent provenance.
Reservations begin at transport attempt, expire with their rolling window, and
remain counted after uncertain failures until expiry or reconciliation.

**AR-6.** Operator reconciliation is non-evidentiary configuration but has a
durable local history: account, old/new value, source, actor, and time.

### 10.2 Control

**CR-1.** Every input receives command ID, durable accepted/rejected status,
target, and earliest eligible attempt boundary.

**CR-2.** Input acknowledged during an open attempt cannot affect that attempt.
If no eligible future attempt occurs, it resolves `unapplied`.

**CR-3.** Text passes `PatternRegistry`. Files use an approved extraction and
sanitization contract. Unsupported binary content fails before dispatch.

**CR-4.** Content is an encrypted artifact. `context_injected` records hash,
path, MIME type, size, redactions, command ID, target, and effective attempt;
raw content is not embedded in receipts.

**CR-5.** `run_replanned` records old/new plan hashes, reason, command ID, and
affected future attempts. Prior evidence is immutable.

## 11. Dependencies and release gates

Status is relative to the Release 0 implementation branch based on protected
`main` at **ea70760**. Local quality, adversarial, headless, package, and wheel
checks passed on 2026-07-25; protected-main CI remains the merge gate.

| Dependency | Status |
|---|---|
| Split token counts and preflight refusal | landed |
| Receipt `observed_at` and signed rolling manifest foundation | landed |
| Rate table, in-memory entitlement ledger, settlement receipts | foundation landed; Rev 4 hashing/decimal/cross-run lifecycle outstanding |
| DeepSeek/Qwen routing | implemented; default region pending |
| Verified Fleet projector and loopback server | foundation landed; stable snapshot, directional coverage, and capability checks outstanding |
| Governed textual context injection | foundation landed; command lifecycle, attempt boundary, and replan outstanding |
| Root-certified per-run writer identities binding `(run_id, role, key)` | **landed** — `receipts.py:670-717`, verifier at `:993-994` |
| Two-sided writer-role check (declared vs certified) | **landed** — `receipts.py:1042-1060`; mismatch verifies as tampered |
| Artifact key separated from signing keys (TC-5) | **landed** — five independent secrets in `RunKeys` (`receipts.py:477-483`); artifact `:817`, manifest `:840` |
| Schema v2 receipts with writer signatures | **landed** — `_RECEIPT_SCHEMA_VERSION = "2.0.0"` (`receipts.py:32`), legacy 1.0.0/1.1.0 still accepted |
| Lane catalog, attempt lifecycle, sequence-linkage validation | **landed** — `run_evidence.py:9-24`, `validate_v2_receipt_contract` at `:117-239` |
| Manifest-before-store read order (VC-3) | **landed** — `fleet.py:445-446` |
| Exact Host validation on the HTTP surface | **landed** — `fleet_http.py:133-144` |
| Single canonical JSON encoder, receipt path (TC-2) | **landed** — `_canonical` at `receipts.py:94-100` serves hash `:729` and store `:801` |
| Canonical encoder extended to manifest and certificate (TC-2) | implemented; receipt, certificate, and manifest storage use the pinned canonical encoder and packaged oracle |
| Local evidence broker holding all keys (TC-7) | implemented; production run creation exposes a keyless broker facade |
| Broker capability issuance and binding (TC-7a) | implemented; role-bound, process-bound, expiring, single-use grants fail closed on replay |
| Authenticated artifact encryption (TC-5) | implemented with AES-256-GCM and run ID as associated data |
| Recovery identity, `run_abandoned`, `workflow_abandoned` (TC-1, SR-4a/b, LC-7a) | implemented and certified at run creation |
| Machine-readable transition matrix and generated conformance tests (TC-4a) | implemented in `domain/evidence_transitions.py`; append and verification share it |
| Crash-durable commit (WC-1) | implemented with receipt fsync, canonical temporary manifest fsync, atomic replace, and directory fsync |
| Covered-prefix projection (VC-3) | implemented; uncovered tails report `live_catching_up` and do not enter reduction |
| Registry anti-rollback head anchor (AR-4c) | not landed |
| Orphan annotation (SR-3a) | implemented as non-evidentiary supervisor state |
| Abandoned-attempt reduction (LC-1, VC-6, VC-7) | implemented only through certified `run_abandoned` evidence |
| Append-only dispatch registry (AR-4a) | not landed; no record survives a deleted run |
| Coverage-gated entitlement preflight (AR-4b) | not landed |
| Capability bootstrap and session contract (SR-5a) | implemented; a single-use URL nonce exchanges for an HttpOnly, expiring, rotating session |
| Operator action lifecycle | implemented with linked resolution and operator-gateway closure |
| Local supervisor and interruption evidence | implemented with atomic non-evidentiary state and role-limited interruption/recovery writes |
| Reservation expiry/reconciliation and cross-run aggregation | not landed |
| Binary extraction/sanitization | not landed |

### Release 0 gate

Production Fleet Read work begins only after:

1. The key hierarchy, schema v2, writer authorization, and legacy policy pass
   security review and tests.
2. Certificates bind `(run_id, role, key)`, and a receipt whose declared
   `writer_role` differs from its certified role verifies as `tampered`.
3. The evidence broker holds every signing, manifest, and artifact key; a test
   proves no client writer process can read key material, that a caller cannot
   choose its own `writer_role`, and that a capability issued to one client is
   refused when presented by another or replayed after use.
4. The broker and verifier are both driven from one machine-readable transition
   specification, and generated conformance tests exercise every row positively
   and every precondition negatively (TC-4a).
5. The broker serializes concurrent appends from multiple writers without a
   sequence gap, duplicate, or lost manifest update, and upholds the full WC-1
   commit including manifest and directory fsync.
6. Crash-injection fixtures interrupt the commit between each WC-1 step and
   never produce `tampered` or a truncated manifest that reads as valid (WC-1a).
7. One canonical JSON function produces the stored bytes and the signed bytes for
   **every** signed object — receipt, manifest, and run certificate — pinned by an
   import oracle, with a non-ASCII receipt in the fixtures. `certificate_hash`
   digests canonical bytes.
8. Attempts, actions, lane catalog, terminal transitions, and exception closure
   are versioned and tested, including the authorized post-action closing writer
   and both terminal workflow states (LC-7a).
9. Verification distinguishes catching-up, truncation, tampering, unreadable, and
   incomplete; projection reduces only the manifest-covered prefix.
10. The supervisor owns workers independently and writes only its authorized
    interruption evidence. The recovery writer has a certificate minted at run
    creation, is the only path to `run_abandoned`, and every precondition the
    verifier checks for it is derivable from the chain alone.
11. Exact Host validation and capability authentication protect the HTTP surface,
    reads included, with the SR-5a bootstrap exchange implemented end to end.
12. A reference reducer passes repeat-audit, repair, block, failure,
    interruption, stale-lease, legacy, partial-write, truncation, and
    double-death fixtures. The double-death fixture kills the worker after
    `stage_attempt_created` and the supervisor before it can write
    `stage_interrupted`; the run must remain `live_verified` with the lane still
    `running` and an `orphaned` annotation, and must reduce to `abandoned` only
    after `run_abandoned` is sealed. No reduction error and no tampering claim in
    either phase.
13. A concurrent-append fixture writes a receipt between the manifest and store
    reads and asserts `live_catching_up`, not `tampered`, with the uncovered
    receipt excluded from projection.

Release 2 additionally requires rate hashes, non-float amounts, the append-only
dispatch registry, cross-run certified aggregation, reservation expiry,
reconciliation history, and resolved provider routing. Release 3 requires the
command lifecycle, sanitizer, attempt boundaries, and replan receipts.

## 12. Build order

Rev 5.4 implements and locally verifies steps 0-7. Rev 5.5 implements step 8,
Fleet Read UI. Steps 9-10 remain gated as Release 2 and Release 3 work.

0. One pinned canonical JSON encoder shared by hashing, signing, and storage.
   Everything below signs over its output, so it lands first. Partly done:
   `_canonical` already covers the receipt path, so the remaining work is
   extending it to the manifest and certificate writers, hashing the certificate
   over canonical bytes, and adding the import oracle. If decision 6 selects JCS
   over the landed `json.dumps` form, that substitution happens here, before
   schema v2 freezes.
1. Evidence broker: key custody, capability issuance and binding, serialized
   commit, crash-durable WC-1. Every writer below reaches the chain through it,
   so it precedes them.
2. Machine-readable transition specification, then the key hierarchy, five
   certified identities including recovery, authenticated artifact encryption
   with a key independent of signing, schema v2, legacy compatibility.
3. Attempt, action, lane-catalog, and terminal-decision contracts, including the
   post-action closing writer, the recovery writer, and both terminal workflow
   states.
4. Covered-prefix verifier and directional coverage states.
5. Supervisor lifecycle, interruption evidence, and orphan annotation.
6. HTTP Host validation, capability bootstrap, and session handling.
7. Pure reference reducer and adversarial fixtures.
8. Fleet Read board, detail, monitor, and notifications.
9. Dispatch registry, accounting replay, and cross-run entitlement lifecycle.
10. Control commands, supported artifacts, and replanning.

## 13. Acceptance criteria

### Release 1

- A four-core happy path shows two dormant repairs and one run-level action.
- A repair activates one lane and preserves distinct `g2a` attempts.
- Preflight refusal, transport attempt, failure, and interruption display
  distinct evidence-supported messages.
- Every created attempt has exactly one terminal transition. After a double
  death the lane stays `running` under an `orphaned` annotation, and becomes
  `abandoned` only once `run_abandoned` is sealed. An unterminated attempt in a
  `sealed_verified` run is a reduction error.
- Supervisor signatures verify only on authorized interruption/terminal events.
- A receipt signed by a validly certified key but declaring a `writer_role`
  other than the one bound in its certificate verifies as `tampered`.
- Generated conformance tests exercise every TC-4 row positively and every
  precondition negatively, from the same specification that drives the broker
  and verifier. Among them: a gateway terminal decision with an action still
  open, a supervisor failure decision with no preceding `stage_interrupted`, a
  second `run_planned`, a `run_decision: completed` on a run with a failed lane,
  and a `stage_completed` for an attempt never created — each `tampered`.
- No TC-4 precondition references process liveness, wall-clock time, or an
  operational annotation. An offline verifier given only an exported chain
  reaches the same verdict as the live one, and a fixture asserts it.
- Run A's writer key, used to sign a **newly constructed, syntactically valid
  run B receipt**, fails verification because the certificate binds `run_id`.
  Verbatim replay is not sufficient evidence for this criterion, since it fails
  on the `run_id` field alone and exercises no certificate logic.
- Decrypting a run B artifact with run A's key raises an authentication error.
  Asserting on garbage output is not acceptable; the criterion requires AEAD.
- A test running under a worker's effective identity finds no readable private
  key material anywhere, and a caller presenting a worker capability cannot
  append a receipt claiming `writer_role: supervisor`.
- A capability issued to one client process is refused when presented by
  another, after expiry, and when an append message is replayed.
- Decrypting any artifact does not yield material that can sign a manifest or a
  receipt, closing the current path where both derive from `self.key`.
- Three concurrent writers appending through the broker produce a gap-free,
  duplicate-free sequence and one consistent final manifest.
- A crash injected at each WC-1 step leaves the run readable as
  `live_catching_up`, `unreadable`, or `incomplete` — never `tampered`, and
  never a manifest that is truncated but verifies.
- `run_abandoned` enumerating two of three unterminated attempts abandons
  exactly those two, seals the manifest, and renders any later receipt
  `tampered`. A run with no unterminated attempt cannot be abandoned at all.
- A v1 run and a run predating the recovery identity both refuse
  `run_abandoned` and route to operator reconciliation instead.
- A receipt containing non-ASCII payload text round-trips: the stored line
  rehashes to its sealed `receipt_hash` under the single canonical encoder.
- An action-gated run reaches `workflow_closed` through an operator-gateway
  terminal decision referencing the `action_resolved` sequence.
- Schema-v1 runs render degraded, not falsely malformed.
- Closing Fleet does not stop the worker; reopening reproduces normalized state.
- Active manifests verify; store-ahead is catching-up; manifest-ahead and
  covered truncation are tampering. An append landing between the manifest and
  store reads yields catching-up, never tampering, and the uncovered receipt
  does not appear in any lane state, tally, or count.
- The same prefix reduces identically under live, expired, and absent leases.
- One action notifies once and closes only through verified action resolution.
- Host rebinding, spoofed Origin, missing/expired capability, and non-loopback
  bind tests fail closed. A read of `/api/v1/fleet` without a capability token
  is refused. `/healthz` returns a fixed response carrying no run ID, count,
  lifecycle state, or verification finding.
- The bootstrap nonce is accepted exactly once; a second use of the same launch
  URL is refused. No reusable session credential appears in any URL or log; a
  spent nonce in shell or browser history grants nothing.
- After `workflow_closed` or `workflow_abandoned` the session still serves reads
  and the final state renders; every mutation route refuses.
- Keyboard, reduced-motion, focus, contrast, and screen-reader checks pass.

### Release 2

- Rate-table replay reproduces priced and unpriced outcomes by table hash.
- Sealed monetary values require no binary-float interpretation.
- Run totals sum unrounded values and round once.
- Shared Qwen/DeepSeek entitlement uses one window.
- Possibly consumed reservations expire or reconcile without permanent drift.
- Invalid runs reduce an explicit coverage denominator, not silently the total.
- Deleting one verified run's entire directory still lowers coverage, because
  its registry enrollment survives outside it. The preflight refuses
  `entitlement_coverage_incomplete`, the run resolves `deleted`, and its
  conservative reservation remains counted.
- Deleting the registry, or restoring an older validly signed copy, fails closed
  as `registry_rollback_detected` rather than reading as full coverage.
- Runs whose window has closed leave the active denominator and release their
  reservations, so coverage does not ratchet downward as runs age out.
- Routine root rotation leaves coverage at 100 percent and dispatch unblocked;
  marking a root `distrusted_compromised` is what reduces coverage.
- Every entitlement figure renders under a TORQ-observed label; no view presents
  a figure as total provider-account consumption.

### Release 3

- Accepted input is sanitized, encrypted, receipted, and applied no earlier
  than its acknowledged boundary.
- Open-attempt injection names a future eligible boundary or becomes unapplied.
- Unsupported content fails before dispatch.
- Replans name old/new hashes and never mutate prior evidence.

## 14. Remaining product decisions

1. Supervisor packaging: daemon, broker-managed worker, or OS service.
2. Whether OpenAI `g2a` remains metered or moves to subscription CLI transport.
3. Default Qwen Token Plan region when configuration declares none.
4. Whether operator-declared quota limits are sufficient for initial Release 2.
5. Rolling-manifest cadence and maximum visible verification lag.
6. **Settled in Rev 5.4:** canonical JSON uses the pinned standard-library
   `json.dumps(value, sort_keys=True, separators=(",", ":"),
   ensure_ascii=True)` encoding. A packaged non-ASCII oracle pins the exact
   bytes and SHA-256 without adding a fourth runtime dependency.
7. Broker key custody and identity: OS keychain (reusing the `keyring` backend
   already carrying provider credentials), an encrypted keystore the broker
   unlocks at start, or a separate OS user under which the broker runs. Only the
   last also defends against an operator-privileged process reading the broker's
   memory, and only it imposes account setup on a single-operator local tool.
8. Broker transport: Unix domain socket / named pipe with peer credential
   checks, or loopback TCP with capability tokens. The first is harder to reach
   accidentally; the second reuses the SR-5 machinery.
9. Broker unavailability: whether a run refuses to start, blocks, or degrades
   when the broker is down. The broker is now on the critical path for every
   receipt, so its absence must have a defined, fail-closed behavior.
10. Who may invoke the recovery writer, and whether abandoning a run requires a
    confirmation step. `run_abandoned` is irreversible and seals the manifest,
    so an accidental invocation destroys the run's ability to continue. It is
    the only terminal transition an operator can trigger directly.
11. Whether `trusted_legacy` roots age out of aggregation on a schedule or
    remain trusted indefinitely. Indefinite trust means a root retired years ago
    still contributes coverage; a schedule reintroduces the rotation-availability
    problem more slowly.

These decisions do not reopen the Rev 4, Rev 5, Rev 5.1, or Rev 5.2 trust,
attempt, action, or HTTP security requirements.

## 15. Rev 4 changes

- Replaced self-asserted shared-key `authority` with certified per-run writer
  identities, writer signatures, and explicit evidence basis.
- Separated root trust, manifest signing, writer signing, and artifact
  encryption keys.
- Added schema-v2 and degraded schema-v1 behavior.
- Added preflight-safe `stage_attempt_created` and exact terminality.
- Added action open/resolve lifecycle and separated execution completion from
  workflow closure.
- Corrected transport-attempt wording so it does not claim proven egress.
- Corrected implementation status for unexpected exceptions, rate hashes,
  float amounts, cross-run entitlements, stable snapshots, and control commands.
- Made Host validation and capability authentication mandatory release gates.

## 16. Rev 5 changes

Rev 4 established the trust model. Rev 5 closes the gaps that made parts of it
unenforceable, unclosable, or self-contradicting when checked against the code.

This section is history, not current contract. Rows 1, 2, 5, 6, 7, 8, and 10
were superseded or corrected by Rev 5.1; read Section 17 alongside it. Rev 5.2
(Section 18) then corrected parts of Rev 5.1 in turn. Where revisions disagree,
the latest governs.

| # | Item | Gap in Rev 4 | Change |
|---|---|---|---|
| 1 | TC-1 | Certificates certified a key but bound nothing, so a certified writer could sign into another run or claim another role | Certificate binds `(run_id, role, key)`; contents enumerated; revocation declared out of scope with reasoning |
| 2 | TC-2 | "Canonical" was unnamed, and the code already uses two different encodings for the stored line and the hashed body | Pins one function (JCS or the explicit `json.dumps` triple); requires stored and signed bytes to share it; cites `receipts.py:513/517/541` |
| 3 | TC-4 | `writer_role` was self-asserted, so the permission table constrained nothing | Verifier enforces declared role against certified role; mismatch is `tampered` |
| 4 | TC-4, LC-7 | LC-7 required a sealed terminal decision after action resolution but authorized no writer to produce one, leaving action-gated runs unclosable | Operator gateway authorized for the post-action terminal decision, referencing the `action_resolved` sequence |
| 5 | TC-7 (new) | Key separation was named but not storage-enforced; a worker able to read the supervisor key can forge the interruption evidence FR-5 presents as independent | Per-role storage boundaries; supervisor key created before worker spawn; notes the current one-key-per-evidence-root model as the delta |
| 6 | LC-1, VC-6 (new), section 9 | "Exactly one terminal transition" is violated by a double death with no surviving writer; the reducer would report a crash as an integrity failure | `abandoned` state added, valid only under `incomplete`; still a reduction error under `sealed_verified` |
| 7 | VC-3 | Read order was unpinned; the current store-then-manifest order manufactures a false `tampered` on any concurrent append | Manifest read first, then store, so the race lands in `live_catching_up` |
| 8 | AR-4 | Meters excluded unverifiable runs, so evidence tampering lowered recorded consumption and would have read as freed quota | Preflight fails closed below 100 percent coverage with `entitlement_coverage_incomplete`; display unchanged |
| 9 | SR-5 | Capability tokens were required only on mutation, leaving run identity, bindings, refusal reasons, and settlement readable by any local process | Token required on all routes except `/healthz` |
| 10 | Section 13 | "Compromise of one run key cannot forge another run or decrypt its artifacts" stated a property with no test shape | Replaced with two fixture-asserted criteria: cross-run key replay fails verification, cross-run artifact key fails to decrypt |

Also added: gate items for role binding, key storage, canonical encoding, read
order, double-death and concurrent-append fixtures; a build-order step 0 for the
canonical encoder, since everything else signs over its output; status rows for
each new requirement; and decisions 6 and 7 for the two choices Rev 5 constrains
but does not settle.

## 17. Rev 5.1 changes

Rev 5 added requirements that were internally unsatisfiable. Each item below is
a case where following Rev 5 exactly produced a contradiction, an unreachable
state, or a control that did not control what it claimed.

### Blocking corrections

| # | Item | Why Rev 5 failed | Rev 5.1 |
|---|---|---|---|
| 1 | LC-1, VC-6, SR-3a, SR-4a, section 9 | `abandoned` was gated on `incomplete`, which the writer never produces: the manifest is replaced after every append, so a double death leaves a valid `live_verified` run. The state was unreachable and the gate fixture asserted an impossible transition | Lane stays `running`; the operational `orphaned` annotation marks it; a certified recovery writer seals `run_abandoned`, which is the only route to `abandoned`. Evidence moves reduced state, liveness never does |
| 2 | TC-7 | Owner-only key files do not isolate processes running as the same OS user, which is every writer here. Rev 5 mandated the control in TC-7 and admitted its inadequacy in decision 7. It also left three processes appending to one hash chain with three private views of the chain head | A single local evidence broker owns all keys, exposes capability-scoped appends, stamps `writer_role` from the capability, enforces the TC-4 matrix at write time, and serializes every commit |
| 3 | VC-3, WC-1 | The rule pinned a read order but never said which receipts are projected, leaving the uncovered tail — signed by a writer, covered by no manifest — eligible for reduction | Projection is exactly the manifest-covered prefix; the tail is verification lag. Read order follows from the new WC-1 writer invariant rather than carrying the guarantee alone |
| 4 | AR-4a | Fail-closed coverage cannot detect a deleted run: the directory holds both the usage and the only evidence the run existed, so numerator and denominator fall together and coverage stays at 100 percent | Root-level append-only dispatch registry outside every run directory, enrolled before first dispatch, resolving absent runs to `missing`/`deleted`/`expired`/`unverifiable` with conservative reservations |
| 5 | TC-5, section 13 | The cross-run artifact criterion cannot be asserted against an unauthenticated HMAC-keystream XOR: a wrong key returns bytes, not an error | AEAD required (AES-256-GCM or ChaCha20-Poly1305, run ID as associated data); criterion asserts an authentication failure. The signing fixture now forges a valid run B receipt with run A's key rather than replaying a run A receipt |

### Corrections

| # | Item | Change |
|---|---|---|
| 6 | TC-2 | Withdrew the claim that the stored value cannot be reproduced — a verifier can parse and re-serialize. The defect is dual representation and silent cross-implementation drift. Dropped the incorrect `receipts.py:517` citation, which is the redaction scan and never persisted. Canonical choice must settle before schema freeze |
| 7 | TC-4 | Role table replaced by a transition matrix over role, transition, evidence basis, required prior state, and referenced sequence. A role table alone let the gateway seal a terminal decision on a run with actions still open |
| 8 | SR-5a, SR-6 | Rev 5 required a token on every read without specifying delivery, which a navigating browser cannot satisfy. Added the bootstrap exchange: single-use launch parameter, redirect to a clean URL, `HttpOnly`/`SameSite=Strict` host- and path-scoped session cookie, idle and absolute expiry, rotation, `Referrer-Policy: no-referrer`. `/healthz` reduced to a fixed response with no verification finding |
| 9 | TC-1 | Withdrew the revocation rationale, which answered the wrong actor: the party needing revocation is the operator responding to a leaked key, not an attacker. Infrastructure stays out of scope; retirement, exclusion from aggregation, and forward-only root rotation are now defined |

Also added: TC-4's `recovery` row and the `run_abandoned` transition; broker
gate items for key custody, write-time matrix enforcement, and concurrent-writer
serialization; TORQ-observed labeling on every entitlement figure, since TORQ
cannot see account usage performed outside it; build-order step 1 for the broker
ahead of every writer; and decisions 8 and 9, which the broker raises and does
not answer.

## 18. Rev 5.2 changes

Rev 5.1's architecture stands. Rev 5.2 finishes the recovery writer it
authorized without giving it an identity, makes its preconditions checkable
offline, narrows authority that was still too broad, and supplies the durability
and anti-rollback properties the broker and registry were assumed to have.

### Blocking corrections

| # | Item | Why Rev 5.1 failed | Rev 5.2 |
|---|---|---|---|
| 1 | TC-1, TC-2, SC-1a | TC-4 authorized a `recovery` writer, but TC-1 minted only four identities and TC-2's enum omitted the role. No valid certificate could sign `run_abandoned` | Five certified identities, all minted at run creation because recovery is needed when every run process is dead; `recovery` added to the enum; v1 and pre-contract runs route to operator reconciliation instead |
| 2 | SR-4a, SR-4b, TC-4, VC-6, VC-7, LC-7a | The recovery row required prior state `orphaned`, which SR-3a defines as non-evidentiary. An offline verifier cannot prove no worker was alive, so the row's `tampered` consequence was unenforceable | Preconditions are now chain-derivable only: no terminal decision, at least one unterminated attempt, enumerated set equals exactly those attempts. `evidence_basis: submitted`, since operator liveness observation is an assertion, not an inference. `run_abandoned` terminalizes the enumerated attempts, sets `workflow_abandoned`, seals the manifest, and forbids later receipts |
| 3 | TC-4, TC-4a | One `run_decision` row admitted any decision whenever none was sealed, so `completed` could be sealed on a run with failed lanes; one combined row covered attempts, routing, and stage results | Per-decision and per-transition rows with individual preconditions; a machine-readable specification drives both broker and verifier; conformance tests generated over every row rather than sampled |

### Corrections

| # | Item | Change |
|---|---|---|
| 4 | AR-4a, AR-4c | Signed entries detect modification but not deletion or restoration of an older valid registry. Added a hash-chained registry head anchored outside the mutable journal; missing or rolled-back registries fail closed as `registry_rollback_detected`, not as reduced coverage. Fixed the AR-5 contradiction: `expired` entries leave the active denominator and release their reservations |
| 5 | WC-1, WC-1a | Withdrew the claim that current code upholds WC-1. `_write_manifest` (`receipts.py:582-584`) does not flush the temporary file or the directory, so the ordering is logical but not crash-durable. Specified the four-step commit and added crash-injection fixtures between each step |
| 6 | TC-7, TC-7a, TC-7b | "No writing process holds a private key" contradicted the broker holding them; now "no client writer process". Added the capability contract — per-run per-role issue, process binding, expiry, single-use replay protection — and stated plainly that none of it defends against reading a live client's memory as the same user |
| 7 | SR-5a, SR-5b, SR-6 | Rev 5.1 forbade tokens in persisted URLs while putting one in the launch URL. Split the single-use bootstrap nonce from the session token: the nonce may live in history because a spent nonce grants nothing; no reusable credential ever enters a URL. Closure now downgrades the session to read-only rather than invalidating it, since closure is when the final state most needs to render |
| 8 | TC-1 | Excluding every retired root from aggregation made routine rotation a self-inflicted outage under AR-4b. Split into `trusted_legacy`, which aggregates at full weight, and `distrusted_compromised`, which does not |

Also recorded: the artifact key is currently the Ed25519 signing key
(`receipts.py:557` and `:574` both use `self.key`), so artifact-read access and
manifest-forgery access are the same capability today — a live violation of TC-5
now carried as its own status row. Added decisions 10 and 11, on who may trigger
the irreversible `run_abandoned` and whether `trusted_legacy` roots ever age out.

Line citations in Sections 16-18 describe the tree as it stood when each revision
was written and are preserved as written. Sections 1-14 cite `origin/main` at
56b8bff; see Section 19.

## 19. Rev 5.3 changes

No requirement changed. Rev 5.3 reconciles the document with `origin/main` at
56b8bff, after PRs #15-#19 merged on 2026-07-25. Seven requirements this document
described as outstanding had landed, and a related defect it had not noticed was
surfaced. Every line citation in Sections 1-14 was
re-derived against that commit; `receipts.py` grew from 696 to 1079 lines and
`fleet.py` from 178 to 468, so none of the previous citations pointed at the code
they named.

### Now landed

| Requirement | Evidence |
|---|---|
| TC-2, receipt path | `_canonical` (`receipts.py:94-100`) serves both the hash (`:729`) and the stored line (`:801`). The dual-encoding defect Rev 5 raised is closed for receipts |
| TC-5, key separation | `RunKeys` (`receipts.py:477-483`) carries five independent secrets; artifact keystream `:817`, manifest signature `:840`. The live TC-5 violation recorded in Section 18 is resolved |
| TC-1, certified identity | `_write_run_certificate` (`receipts.py:670-717`) binds run ID and role; verifier enforces at `:993-994` |
| TC-4, role binding | Two-sided check at `receipts.py:1042-1060`: a declared `writer_role` that differs from the certified role verifies as tampered |
| Schema v2 and lifecycle | `_RECEIPT_SCHEMA_VERSION = "2.0.0"` (`:32`); lane catalog and stateful attempt/sequence validation in `run_evidence.py:9-24` and `:117-239` |
| VC-3, read order | `fleet.py:445-446` reads the manifest before the store |
| SR-5, Host validation | `fleet_http.py:133-144` compares the single `Host` header against the bound port |

### Newly surfaced

The certificate and manifest are each signed over `_canonical(...)`
(`receipts.py:706`, `:843`) but persisted as `json.dumps(..., sort_keys=True)`
(`:710`, `:847`), and `certificate_hash` (`:838`) digests the persisted bytes.
The receipt fix did not propagate to these two writers. TC-2 previously described
this defect only for receipts; it now names all three objects, and Release 0 gate
item 7 was widened accordingly.

### Confirmed absent

Each of these was checked by search over the 56b8bff tree, not inferred from the
absence of a citation:

- **Evidence broker (TC-7, TC-7a).** Zero occurrences of `broker` in `src/` or
  `tests/`.
- **AEAD (TC-5).** No `AESGCM`, `ChaCha20`, or `aead` symbol anywhere in `src/`.
- **Capability authentication (SR-5a).** No capability, session-token, or bearer
  handling on any route; the only matches in `src/` are redaction denylists.
- **Crash durability (WC-1).** `_write_manifest` (`receipts.py:845-851`)
  contains no `fsync` of the temporary file or the containing directory.
- **Recovery identity (TC-1).** `_WRITER_ROLES` (`receipts.py:35`) enumerates
  exactly `orchestrator`, `supervisor`, `operator_gateway`.

### Corrections to status

Seven "not landed" rows in Section 11 were wrong and were replaced with the
evidence above. The gate itself did not move: the evidence broker, AEAD, crash
durability, the recovery identity, the machine-readable matrix, covered-prefix
projection, and capability authentication all remain absent, and six of the
thirteen gate items still fail. Build order step 0 is now partly complete — the
receipt encoder exists, so the remaining work is the manifest and certificate
paths plus the import oracle.

### Correction to the reported backend phase list

The implementation phase list circulating alongside this reconciliation omits
TC-4a, the machine-readable transition specification. It is the first item of
build-order step 2 and drives both the broker and the verifier, so it cannot be
folded into the lifecycle-contract phase after those are built.

It also names the canonical JSON encoder as the next item without noting that the
receipt path already landed. The remaining work is the manifest and certificate
writers, `certificate_hash` over canonical bytes, and the import oracle — and
decision 6 is still open while the `json.dumps` form is now shipped code, so
selecting JCS means replacing a landed function before schema v2 freezes. That
decision closes before step 0 proceeds.
