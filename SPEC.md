# TORQ CLI — Product Specification

**Status:** Draft v2 — rewritten after adversarial self-review found v1's central
trust claim was false against the code (see §9 changelog).
**Lens:** Enterprise AI-ops / governance buyers (confirmed).
**Last updated:** 2026-08-02

> This spec defines *what* TORQ CLI is, *who* it serves, and *how* it gets
> built. Architecture docs in `docs/architecture/` are authoritative for *how
> the code works*; this document is authoritative for product intent. Where
> they disagree, the spec is wrong and gets fixed. Every capability claim below
> is keyed to the source that backs it; claims tagged **[code-verified]** were
> confirmed against the tree during this revision.

---

## 1. What TORQ CLI is

TORQ CLI is a **governed agent runner and evidence-backed control surface**. It
lets an enterprise run AI-agent work (plan → review → build → audit) with three
guarantees a naked provider CLI does not give you:

1. **Process containment.** Provider processes run inside an OS-enforced
   ownership boundary — **Windows Job Objects in production** **[code-verified:
   `adapters/windows_job.py`]**; experimental Linux cgroup-v2; **fail-closed on
   macOS** (`owned_process_strong_containment_unavailable`). The provider tree
   is owned and its termination is confirmed. *Note: this is process
   containment, not a workspace sandbox — see §1.1 for the honest distinction.*
2. **Evidence.** Every stage of a run is recorded as a tamper-resistant,
   hash-chained, **Ed25519-signed receipt** with an **AES-GCM-encrypted** response
   artifact **[code-verified: `safety/receipts.py`]**. The outcome is
   independently verifiable after the fact: what ran, against which profile, with
   which provider/model, and what it produced.
3. **Governance.** Work flows through a fixed stage pipeline
   (`g1d → g1r → builder → g2a`) **[code-verified: `application/orchestrator.py`,
   `docs/architecture/governed-orchestration.md`]** with executable policy.
   Provider/model bindings are immutable per role; HIGH defects route through a
   bounded repair lane; critical defects halt for human escalation. Live
   execution requires **double opt-in** (`--allow-live` *and*
   `--policy-allow-live`) **[code-verified: `interfaces/cli.py`]**; dry-run is
   the default.

The **Fleet** surface turns this into an operable control plane: an attended,
**loopback-bound** UI to drive runs, observe evidence, and approve/reject
proposals with the receipt chain as the audit record **[code-verified:
`interfaces/fleet_http.py`]**.

### 1.1 What "containment" does and does not mean here (honest scope)

This is the section v1 got wrong, so it's stated plainly:

- **What exists:** *process* containment — the provider process tree is owned
  by a Job Object (Windows) and its termination is confirmed. Agent output is
  captured as JSON text and stored as an encrypted artifact. The primary
  worktree is **not** written to during a governed run.
- **What does NOT exist:** an isolated worktree/copy sandbox that the agent
  writes files into. **[code-verified: no `sandbox`/`worktree`/`copy_tree`
  symbols anywhere in `src/`.]** `SECURITY.md` asserts "Builder and refinement
  work occurs in an isolated worktree or copy sandbox" — **this is not
  implemented** and should be corrected in `SECURITY.md`.
- **What does exist but is not wired into the governed flow:** a
  **tree-pinned `ApprovalBoundary`** (`safety/approval.py`) that, given an
  explicit approver and a `ChangeProposal` bound to a SHA-256 of the starting
  tree, writes files to the primary tree — refusing if the tree moved or the
  diff hash changed. **[code-verified: `safety/approval.py:33-52`.]** Today this
  is only exercised by `application/e2e.py`, not by the `g1d→g2a` run.

**Implication for the spec:** a real "agent writes code into a sandbox, then a
tree-pinned proposal is applied on approval" flow is **a build target, not a
current capability.** It is Step 1 of the build plan.

### 1.2 What it is not
- Not an agent. It *runs* agents under governance.
- Not an LLM provider. It binds to operators' existing credentials.
- Not a CI/CD system. It produces evidence *for* a human release gate.
- Not tamper-proof against a privileged insider. Production trust requires a
  **non-exportable platform signer**, an **independently operated remote
  transparency anchor**, **and** an **independent trust verifier** — three
  adapters, none of which are implemented today (see §6, §7-Step 2).

---

## 2. Who it's for

**Primary:** Engineering, platform, and security teams in enterprises (and
regulated industries — finance, healthcare, public sector) adopting AI-coding
agents who cannot accept ungoverned writes to production codebases.

### Glossary (terms an enterprise buyer needs)

| Term | Meaning |
|---|---|
| **governed** | Work runs through the fixed stage pipeline under executable policy, with every stage receipted |
| **profile** | An immutable role→provider/model/prompt binding, validated against a closed schema (`domain/registry_schema.py`) |
| **stage** | One step of the pipeline: `g1d` (design draft), `g1r` (design review), `builder`, `g2a` (second audit) |
| **repair lane** | The conditional `refine_bug`/`refine_ui` stages, selected by executable g2a policy when a HIGH defect is found |
| **double opt-in** | Live runs need both `--allow-live` (operator) and `--policy-allow-live` (policy) — neither alone is sufficient |
| **tree-pinned proposal** | A change set bound to a SHA-256 of the starting tree; `ApprovalBoundary` rejects it at apply time if the tree moved |
| **redaction registry** | A pattern registry applied to provider egress before send (`core/redaction.py`) |
| **receipt** | A signed, hash-chained record of one stage's dispatch + provenance + encrypted artifact pointer |

### Personas

| Persona | Their problem | What TORQ gives them |
|---|---|---|
| **Platform eng lead** | "Let devs use agents on the monorepo without a prod incident" | Process containment + (post-Step 1) tree-pinned proposals + approval gate |
| **Security/compliance** | "A defensible audit trail for every agent action" | Signed receipt chain, encrypted artifacts, tamper-resistant evidence |
| **Regulated-industry eng** | "Provider/model provenance and blast-radius for SOX/HIPAA" | Immutable role bindings, deny-by-default network/commands |
| **Release manager** | "Evidence the right checks ran before merge" | The Fleet approval surface backed by the receipt chain |

**Buying posture:** Bottom-up adoption by a platform team, then enterprise
contract sponsorship once the governance story is proven. The product must
survive a security-architect review on first contact.

---

## 3. Who it is NOT for

- **Solo/hobby developers** wanting a faster local coding agent — governance
  overhead is the wrong trade-off without compliance pressure.
- **Teams that want auto-merge** — the model is human-gated proposals.
- **Users needing macOS/Linux production chat today** — Windows is the only
  production containment path in v0.2.0; others fail closed.
- **Teams wanting a hosted/SaaS control plane** — self-hosted, attended,
  local-first by design.
- **Anyone who needs evidence to withstand a privileged-insider compromise
  *today*** — until §7-Steps 1–2 ship, the ledger is tamper-resistant, not
  insider-tamper-evident. Do not position it otherwise.

---

## 4. What success looks like

### Product success signals (12-month) — each falsifiable
1. **One reference deployment** running ≥**50 receipt-verified runs/week for one
   quarter** with zero `tampered` / `trust_anchor_substituted` verdicts from
   `verify_receipt_store`. *(Unit = runs, not "agents"; window = one quarter.)*
2. **External security audit** produces **no critical finding outside the
   documented Wave-2 set**: non-exportable signer, remote transparency anchor,
   independent verifier, structural injection isolation, configurable protected
   paths, cross-platform containment.
3. **Cross-platform production containment** — Windows ✅, Linux gated in CI,
   macOS via signed native helper (separate release track).
4. **Operator identity is real** — every Fleet mutation attributable to a named
   human via an external IdP (OIDC/SAML), not `"operator:local-session"`.
5. **`torq trust readiness` reports `ready`** — achievable only after Steps 1–2
   deliver **all three** of: non-exportable signer + independent transparency
   anchor + independent verifier **[code-verified: `production_trust.py:259-320`
   requires all three]**.

### Anti-success (things we will NOT optimize for)
- Feature breadth traded against fail-closed posture. TORQ ships *fewer*
  conveniences than competitor runners; the discipline *is* the product.
- Raw agent speed. Governance adds latency; the pipeline is strictly sequential
  (g1d→g1r→builder→g2a + bounded repair loops), so a run is N provider
  round-trips with no concurrency. That is expected.
- Vendor lock-in. The receipt schema and evidence format stay open and
  third-party-verifiable with no TORQ install.

### Measurable gates (binary, this year) — tempered by §6 reality
- [ ] Windows + Linux have production containment paths (macOS moved to its own
      release track — see §7 Step 5).
- [ ] `torq trust readiness` → `ready` (requires Steps 1–2 fully landed).
- [ ] Named-operator RBAC via external IdP on every Fleet mutation.
- [ ] External security audit passed (excluding the documented Wave-2 set).
- [ ] SBOM + SLSA L3 signed, attested releases.

---

## 5. Out of scope (explicit non-goals)

| Non-goal | Why |
|---|---|
| TORQ as an LLM provider | Credentials are the operator's; TORQ binds, never brokers |
| Auto-commit / auto-merge / auto-push | Contradicts the human-gated proposal model |
| Browser-held credentials or process handles | Breaks the credential boundary; browser is untrusted |
| Unattended headless-vault unlock | Defeats the attended-no-echo credential model |
| Multi-tenant SaaS hosting | Self-hosted/local-first is a design choice; SaaS is a separate product |
| Rewriting provider transports | TORQ binds to installed transports; it does not ship them |

### Operating model (how TORQ is actually invoked) — *was missing in v1*
TORQ is operator-invoked, not a background service. Expected invocation points:
(a) a developer or release manager runs `torq run --live` against a goal on a
checked-out worktree; (b) a CI job could invoke dry-run `torq run` to produce
planning evidence pre-merge; (c) an attended operator drives the Fleet UI for
approval. TORQ is **not** a long-running daemon and does not auto-trigger. This
needs documenting in an ops runbook (a Wave-2 doc task).

---

## 6. The honest gap map

| Capability | State (v0.2.0) | Gap to enterprise-ready |
|---|---|---|
| Windows process containment | ✅ Production (Job Objects) | None |
| Linux containment | ⚠️ Experimental (cgroup-v2, non-gating CI) | Gate real kernel-containment in CI |
| macOS containment | ❌ Fail-closed only | Signed+notarized native **Endpoint Security** product (separate release, Apple-entitlement-dependent — see Step 5) |
| Workspace sandbox (agent writes files) | ❌ **Not implemented** | Build it — Step 1 |
| Tree-pinned approval | ✅ Primitive exists | Wire it into the governed flow + sandbox |
| Receipt integrity (tamper-resistant) | ✅ Implemented | — |
| Receipt integrity (insider-tamper-evident) | ❌ Three adapters missing | Non-exportable signer + remote transparency anchor + independent verifier — Step 2 |
| Operator identity / RBAC | ❌ Constant `"local-session"` | External IdP (OIDC/SAML) + roles + actor attribution — Step 3 |
| Credential backend | ✅ Keyring + headless vault | Automated rotation/expiry (manual today) |
| **Cost containment** | ⚠️ Per-stage ceilings only | **No run-wide hard spend cap** — a rogue run can burn unbounded provider budget. Step 4. |
| Observability / SIEM export | ❌ None | Structured event stream — Step 6 |
| Prompt-injection isolation | ⚠️ JSON-escape only | Structural delimiter contract — Step 7 |
| Configurable protected paths | ❌ Hardcoded `e:/torq-console` | Governed config field — Step 8 |
| Release supply chain | ⚠️ CI strong, no SBOM/attestation | SLSA L3, signed releases — Step 9 |

**Release-evidence caveat:** v0.2.0 is a *candidate*. Protected-main CI and
clean-machine install/release evidence are **not yet recorded**
**[code-verified: `docs/releases/torq-cli-v0.2.0.md`, production-readiness
audit]**. Claims about "v0.2.0" states refer to source-tree state, not shipped
attestations.

---

## 7. Build plan, step by step

Each step lists **key decisions** and the **default I'd choose** (override
before I build). Steps ordered by leverage on closing the §6 gaps. **This plan
is corrected from v1 — see §9.**

### Step 1 — Workspace sandbox + wire tree-pinned approval into the governed flow
*v1 claimed this existed. It does not.* Build the thing the security model
already advertises: a sandbox the builder writes into, and a `ChangeProposal`
emitted at `awaiting_approval` that `ApprovalBoundary` applies on approval.

**Key decisions & defaults:**
- **Sandbox type.** *Default: a copy or `git worktree` of the primary tree under
  the run root, cleaned up after sealing. Worktree preferred (free filesystem
  isolation + git's own tree-hash).*
- **What the agent can touch.** *Default: the sandbox only; protected-path
  guardian already enforces denies. Network remains deny-by-default.*
- **Proposal emission.** *Default: `awaiting_approval` returns a real
  `ChangeProposal` with `pinned_tree_hash` = sandbox base SHA-256; apply writes
  to the primary tree via the existing `ApprovalBoundary`, re-verified at apply
  time.*
- **SECURITY.md correction.** *Default: rewrite the "isolated worktree" sentence
  to match reality (it becomes true once this step ships).*

### Step 2 — Production trust: all THREE adapters (signer + anchor + verifier)
*v1 said "Step 2 = remote anchor → readiness ready." **False.** `production_trust.py`
requires three independent adapters; none exist. This is the highest-leverage
and most under-scoped step in the roadmap.*

**Key decisions & defaults:**
- **Non-exportable signer.** *Default: platform TPM/Secure Enclave — Windows
  CNG/TPM, macOS Secure Enclave, Linux TPM trusted-keys — per
  `docs/architecture/production-trust-hardening-decision.md`. A software key
  (even keyring-backed) will NOT clear `production_signing_identity_exportable`
  **[code-verified: `production_trust.py:259-265` requires
  `private_key_exportable=False` AND `isolation in {os_isolated, hardware}`].***
- **Anchor type — TSA is NOT sufficient.** *Default: a **transparency log**
  (Rekor-style), not RFC 3161. The contract requires `append_only=True`,
  `independently_operated=True`, `inclusion_proof_supported=True`,
  `checkpoint_supported=True`, `scope=="remote_transparency"`, plus a live probe
  returning an `AnchorEvidence` with a Merkle inclusion proof and a signed
  checkpoint **[code-verified: `production_trust.py:311-353`].** A TSA token
  satisfies none of these; a TSA path would require weakening the contract, which
  we will not do. Self-hosted Rekor as default; allow enterprise-operated logs.*
- **Independent verifier.** *Default: a separately configured verifier,
  independent of both probed adapters **[code-verified: `production_trust.py:76-83`]**.
  Without it, `production_trust_verifier_unavailable` fires.*
- **Failure mode.** *Default: fail-closed. Anchor unreachable → run completes
  but readiness reports `anchor_unavailable`. No silent local-only downgrade.*
- **Claim honesty.** *Default: rewrite the readiness claim as "Steps 1–2
  together flip readiness to `ready`," not "Step 2 alone."*

### Step 3 — Operator identity + RBAC via external IdP
*v1 defaulted to local passphrase keys and deferred IdP. For the named buyer
(finance/healthcare/public sector), local keys are a stopgap, not the answer —
the audit requirement is centralized identity. IdP moves to the critical path.*

**Key decisions & defaults:**
- **Identity model.** *Default: external IdP (OIDC first, SAML second) as the
  primary path; local passphrase key as a documented **stopgap** for air-gapped
  deployments only.*
- **Roles.** *Default: `approve`, `reject`, `cancel`, `inject`, `recover`,
  `admin`, enforced at the Fleet mutation boundary.*
- **Actor recording.** *Default: a new signed `actor` field on every receipt,
  verified against the identity source. Offline verifier path intact.*
- **Fleet authn.** *Default: loopback stays, but operator identity is asserted
  via a short-lived IdP-issued token, not a static session string.*

### Step 4 — Run-wide cost containment
*v1 missed this entirely.* Metered runs have per-stage ceilings but **no
run-wide hard cap** — an unbounded-cost rogue run is a real enterprise risk.

**Key decisions & defaults:**
- **Cap scope.** *Default: a hard, run-wide USD budget configured in the profile,
  enforced by the entitlement ledger before each dispatch; breach halts
  fail-closed with a `budget_exceeded` receipt.*
- **Defaults.** *Default: no run proceeds without an explicit cap (fail-closed),
  unless the profile declares an `unbounded` opt-in for trusted internal use.*

### Step 5 — Cross-platform containment parity
- **Linux.** *Default: promote the cgroup-v2 evidence CI job from
  `continue-on-error: true` to a hard gate on a real systemd runner; flip
  `linux_containment_capability()` to production-ready once green.*
- **macOS — reframed from v1.** *This is not "a helper." Per
  `adr-2026-07-26-macos-chat-containment.md`, it requires a signed/notarized
  **app** with an authenticated native **XPC helper or Endpoint Security system
  extension**, an Apple-granted **Endpoint Security entitlement** (external
  approval, uncertain timeline), TCC/Full Disk Access handling, peer
  code-signing on the Python↔helper channel, and adversarial clean-machine
  evidence (setsid/double-fork, coordinator crash, PID reuse). It is a
  **separate native product on its own release track**, moved out of the
  "this year" gate.*

### Step 6 — Observability / SIEM export
**Defaults:** structured JSON events (`run_started`, `stage_dispatched`,
`provider_called`, `stage_failed`, `termination_forced`, `proposal_approved`) to
a configurable sink (file default; syslog/OTLP adapter documented). Operational
events only — never credential values or artifact contents (those stay in
encrypted receipts). Receipts = post-hoc signed *evidence*; events = real-time
unsigned *operability*. Two distinct channels.

### Step 7 — Structural prompt-injection isolation
**Defaults:** randomized per-run delimiters around untrusted content + explicit
system instruction to never interpret content inside as instructions and to emit
an `injection_attempt` receipt and halt if asked to; the g2a stage checks for
marker leakage and fails closed. **Kept as a defense, not a guarantee** —
residual risk stays in `threat-model.md`.

### Step 8 — Configurable protected paths
**Defaults:** governed `protected_roots` field in the config profile (validated
by the existing closed schema), always including the credential source root and
run root even if not listed (fail-safe). Delete the hardcoded `e:/torq-console`.

### Step 9 — Release supply chain (SLSA L3) + Wave-1 debt
**Defaults:** CycloneDX SBOM + SLSA build provenance (target L3) + signed wheel/
sdist/checksums/SBOM/provenance to PyPI + GitHub Releases. Then resolve the 62
S/B/UP ruff findings **(including 31 `assert` in `src/` across 14 files, which
vanish under `python -O`)** by replacing asserts with typed `raise`, and
re-enable the broadened ruff gate as required.

---

## 8. Ordering rationale

Steps 1–2 first because they **make the existing security claims true** (sandbox
exists; trust readiness is reachable). Step 3 closes the identity/repudiation
gap. Step 4 closes an unbounded-cost risk v1 ignored. Steps 5–9 raise assurance,
operability, and shippability. The single highest-leverage step is **Step 2** —
but only because v1's under-scoping is corrected: it is three adapters, not one,
and it is what converts "tamper-resistant" into "insider-tamper-evident" *and*
makes `torq trust readiness` honestly report `ready`.

---

## 9. What changed from v1 and why

| # | v1 said | Reality | Fix |
|---|---|---|---|
| C1 | "Step 2 (remote anchor) flips readiness to ready" | `production_trust.py` requires non-exportable signer **+** transparency anchor **+** independent verifier; a TSA is insufficient | Step 2 rewritten as three adapters; TSA rejected; readiness gate attributed to Steps 1–2 together |
| C2 | "Default anchor = RFC 3161 TSA" | The anchor contract demands append-only + inclusion-proof + checkpoint (a transparency log) | Transparency log (Rekor-style) is the default |
| C3 | "Agent work runs in an isolated worktree sandbox" | No sandbox exists in code; `ApprovalBoundary` exists but is unwired | New Step 1 builds the sandbox + wires approval; §1.1 states the gap |
| H1 | Used "tree-pinned proposal" undefined | Real primitive, unwired in governed flow | Glossary added; Step 1 wires it |
| H2/H3 | Success signals unfalsifiable | "≥50 agents/week, no incident" undefined | Rewritten: runs/week, one quarter, specific verdicts |
| H4 | Local-key RBAC as the default | Insufficient for regulated buyers | External IdP is the primary path; local key is a documented stopgap |
| H5 | "macOS = a helper, may warrant its own release" | It's a native Endpoint Security product needing Apple entitlement | Reframed; pulled out of "this year" gate |
| M3 | §6 named rotation as the credential gap | Bigger risk: no run-wide cost cap | New §6 row + Step 4 |
| M4 | Jargon undefined | — | Glossary in §2 |
| M5 | Missing operating model | — | §5 operating-model subsection |
| L3 | Missing install/perf/data-residency/provider-matrix/V6 | — | Folded into §1, §4 (perf), §5, §6 |

**Provider support matrix (was missing):** claude/anthropic, codex/openai,
qwen, kimi/moonshot, zai, deepseek — per-provider model IDs in
`data/registry/v1/registry.yaml`. **Data residency:** all receipts/artifacts are
local; the only egress is the provider call and (future) the anchor digest.
**V6 contract / MMH consensus:** deferred non-goals per v0.1.0 release notes; a
future transparency-log design may subsume them.
