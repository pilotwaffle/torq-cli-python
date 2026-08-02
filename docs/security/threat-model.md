# TORQ CLI Threat Model

**Status:** Living document. Captures the threat surface, controls, and known
residual risks for TORQ CLI v0.2.0. This document consolidates reasoning that
also appears in `SECURITY.md`, `docs/architecture/production-trust-hardening-decision.md`,
`docs/architecture/receipt-key-hierarchy.md`, and the machine-readable findings
emitted by `src/torq_cli/safety/production_trust.py`. Where those sources
disagree, the code is authoritative and this document should be updated.

## Scope

TORQ CLI is a governed agent runner: it validates role profiles, resolves
provider credentials through a fail-closed backend, runs provider processes
under OS-enforced process containment (Windows Job Objects in production),
records cryptographically signed evidence, and exposes a Fleet control surface
for a local attended operator.

**In scope of this model:** credential handling, process containment, evidence
integrity, the Fleet HTTP surface, and the indirect-prompt-injection surface
introduced by injecting model output and operator context into prompts.

**Out of scope:** the security of upstream provider transports once a
credential leaves TORQ's process boundary, and the host OS itself.

## Assets

1. **Provider credentials** — Claude/Codex subscription surfaces, Grok ACP,
   Kimi/Z.ai/DeepSeek direct-adapter tokens, external env files, headless
   vault contents. Compromise = unauthorized model use and billing.
2. **The evidence ledger** — the hash-chained, writer-signed receipt stream and
   the sealed manifest. Compromise = unattributable or forged governance
   record.
3. **The per-run signing keys** — Ed25519 seeds for manifest, orchestrator,
   supervisor, operator-gateway, recovery, and artifact encryption
   (`docs/evidence/**/.torq-run-identities/<run-id-hash>/`). Compromise =
   ability to forge that run's evidence.
4. **The evidence root identity** — the single long-lived Ed25519 anchor.
   Compromise = ability to forge the root-signed run certificate and,
   transitively, every run.
5. **The host filesystem and process tree** — sandbox escape lets model output
   reach operator data or persistence.

## STRIDE analysis

### Spoofing

| Threat | Control | Residual risk |
|---|---|---|
| Attacker impersonates an evidence writer | Per-run independent Ed25519 keys; every receipt carries `writer_role`, `writer_key_id`, `writer_signature`; offline verifier authenticates writer permission against the root-certified run certificate | None known for in-run writer spoofing |
| Attacker impersonates the Fleet operator | Fleet binds loopback-only, Host-header allowlist, single-use mutation sessions (token popped on use), Origin check, CSRF via same-site cookie + post-mutation session rotation | **Operator identity is a constant string `operator:local-session`, not a named human.** Every mutation is attributable only to "the local session." See [Identity gap](#identity-gap). |
| Attacker presents a forged external credential file | External env files must already be owner-only (POSIX `0600` / Windows owner-only DACL); TOCTOU re-stat; TORQ refuses permissive files rather than mutating them | Low |

### Tampering

| Threat | Control | Residual risk |
|---|---|---|
| Receipt chain tampering | Hash-chained receipts; per-writer signatures; manifest-sealed; root-signed external head (`manifest-head.v1.json`) enables rollback detection; canonical-JSON signing bytes | **The receipt anchor is a same-volume signed head.** `production_trust.py` deliberately reports `production_receipt_anchor_not_independent`. A privileged insider who controls the host can rewrite the tail and re-sign the head. This makes the ledger *tamper-resistant*, not *insider-tamper-evident*. See [Remote anchor gap](#remote-anchor-gap). |
| Artifact tampering | AES-GCM with run-id as AAD; uncovered-tail recovery re-verifies writer signatures before advancing | Low |
| Config tampering | Closed, fail-fast config schema; custom YAML event walker (not blind `safe_load`); forbidden-key blocklist; `_MAX_CONFIG_EVENTS`, `_MAX_CONFIG_DEPTH`, BOM handling | Low |
| Protected-path bypass | Guardian denies reads/writes to protected roots before content enters a prompt | **The protected-root set is hardcoded to one developer path** (`domain/hermetic.py`, `_PROTECTED_ROOTS = ("e:/torq-console",)`). On any other host the check silently no-ops for that root. This must become a governed config field. |

### Repudiation

| Threat | Control | Residual risk |
|---|---|---|
| Operator denies an approval/cancellation | Mutations are recorded in the receipt chain with writer provenance | **See [Identity gap](#identity-gap): provenance is "local session," not a named actor.** Auditors cannot bind an action to a person. |

### Information disclosure

| Threat | Control | Residual risk |
|---|---|---|
| Credential disclosure | Config stores opaque `credref_*` handles only; values enter via attended no-echo terminal (redirected input rejected); headless vault uses Argon2id + XChaCha20-Poly1305 with canonical-JSON AAD; child environments filtered; browser receives no credentials | Low |
| Signing-key disclosure | Per-run keys are owner-only and live outside the exportable run directory | **The `.torq-run-identities/` directory was, until Wave 1, not gitignored.** It is now ignored (`*.key`, `*.pem`). Verify with `git log --all -- '*.key'` before any release. |
| Path traversal in injected context | `tests/test_hermetic.py` parametrizes traversal vectors; hardlink-identity rejection; `FILE_FLAG_OPEN_REPARSE_POINT` handling | Low |

### Denial of service

| Threat | Control | Residual risk |
|---|---|---|
| Run runaway | Resource ceilings halt fail-closed; cancellation terminates with evidence | Low |
| Fleet session forking | Single-use mutation sessions (token consumed on use) prevent concurrent-POST forking | Low |
| Cost runaway | Persistent entitlement accounting; `--live` binding | **No hard spend cap on a run by default.** Operators must set entitlements explicitly. |

### Elevation of privilege

| Threat | Control | Residual risk |
|---|---|---|
| Model output escapes containment | Windows: suspended-launch → assign-to-Job → resume; `active_processes()` raises on closed job so kill-on-close isn't misread as confirmed death. Linux: user-systemd cgroup-v2 (experimental, `KillMode=control-group`, `ProtectControlGroups=yes`), fails closed via `linux_containment_capability()`. macOS: fails closed. | **macOS has no real containment implementation** (46 LOC, fail-closed only). **Linux kernel-containment is non-gating in CI** (`continue-on-error: true`). Windows is the only production-hardened path. |
| Indirect prompt injection | Injected content is JSON-escaped; prose instruction to treat transcripts as data | **This is the weakest control.** There is no structural isolation contract (delimiters + "never interpret content inside as instructions"). See [Injection surface](#injection-surface). |

## Known residual risks (canonical list)

These are the limits an enterprise review must understand. None are hidden —
several are deliberately surfaced by `torq trust readiness` — but they are
gathered here as the single source of truth.

### Identity gap

The Fleet surface has no operator identity layer. `resolver_identity` is the
constant string `"operator:local-session"`, and there is no RBAC. Every
approval, rejection, cancellation, and recovery is attributable only to "the
local session," not to a named human. The receipt architecture is ready to
record an actor identity; the identity *source* is not built.

**Impact:** a governance record that proves *what* happened but not *who*
authorized it. Auditors and compliance frameworks require the latter.

### Remote anchor gap

`production_trust.py` defines a `ReceiptAnchor` protocol and an
`evaluate_production_trust` path that *would* accept a remote transparency log
(a signed append-only log with Merkle inclusion proofs and signed checkpoints —
a Rekor-style transparency log). A trusted timestamping service (RFC 3161 TSA)
is **not** sufficient: the anchor contract (`production_trust.py:311-318`)
requires `scope == "remote_transparency"`, `append_only`,
`independently_operated`, `inclusion_proof_supported`, **and**
`checkpoint_supported`; a TSA token satisfies none of these. No implementation
of any compliant anchor exists. The anchor is a signed head on the same volume
as the receipts. This is the single largest architecture-vs-positioning gap:
the product markets "evidence-backed," but the evidence is not tamper-evident
against a privileged insider.

Note: `torq trust readiness` reports `ready` only when **three** independent
adapters are integrated — a non-exportable signer, this remote-transparency
anchor, **and** an independent verifier (`production_trust.py:259-353`). No
subset suffices; the anchor alone does not flip readiness.

**Impact:** a privileged operator (or host compromise) can rewrite the
uncovered tail of a run's ledger and re-sign the head undetectably.

### Injection surface

Operator-supplied and model-derived content is decrypted and placed into the
prompt as `injected_context[].content`, then JSON-escaped
(`application/orchestrator.py`, `application/chat_runtime.py`). JSON escaping
prevents structural prompt breakage but does not prevent the model from acting
on embedded instructions. For a product whose value prop is *governed*
execution, indirect prompt injection via injected context is the primary abuse
vector.

**Impact:** a malicious artifact could instruct the model to emit work that
reads as legitimate but violates the operator's intent.

### Protected-path configuration

`_PROTECTED_ROOTS` is hardcoded. On any host other than the original
development machine, the "fail-closed" protected-path guardian silently
no-ops its primary check.

**Impact:** the headline "Protected paths are denied for reads and writes
before content can enter a prompt" guarantee does not hold outside the original
machine until this is configurable.

### Platform containment maturity

Windows Job-Object ownership is production-hardened. Linux cgroup-v2 ownership
is real but experimental and non-gating in CI. macOS fails closed with no
implementation. "Production governed chat" is therefore Windows-only in v0.2.0.

## Mitigation roadmap (Wave 2+)

Ordered by leverage on the enterprise positioning:

1. **Operator identity + RBAC** — bind a named actor to every Fleet mutation
   and record it in the receipt chain. Closes the identity and repudiation
   gaps simultaneously.
2. **Production trust — all THREE adapters** — implement the remote
   `ReceiptAnchor` against a signed append-only transparency log (Rekor-style),
   **not** a trusted timestamping service (a TSA token satisfies none of the
   anchor-contract fields), AND integrate a non-exportable platform signer AND
   an independent verifier. All three are required to flip
   `torq trust readiness` from `blocked` to `ready` (`production_trust.py:259-353`);
   no subset suffices. This is what converts the ledger from tamper-resistant
   to insider-tamper-evident.
3. **Structural prompt-injection isolation** — adopt a documented delimiter
   contract and an explicit "untrusted data" marker the G2A stage must honor;
   track residual risk in this document.
4. **Configurable protected paths** — make `_PROTECTED_ROOTS` a governed
   validated config field defaulting to cover credential and source roots.
5. **Cross-platform containment parity** — gate Linux kernel-containment in CI;
   ship a signed/notarized macOS helper.
6. **Observability** — structured, SIEM-exportable event stream (run started,
   stage dispatched, provider called, stage failed, termination forced).

## Verification

Controls in this model are backed by `tests/test_hermetic.py` (import/path
isolation), `tests/test_windows_job.py` and `tests/test_owned_process_spike.py`
(process ownership), `tests/test_receipt_authority.py` and
`tests/test_receipt_prose_bounds.py` (receipt integrity), and
`tests/test_hermetic.py` + `tests/conftest.py` (hermeticity). Mutation testing
(`scripts/run_named_mutants.py`, 30 named mutants, M01–M30) defeats "tests pass but the
security check is bypassed." The residual risks above are the ones these tests
do *not* yet cover.
