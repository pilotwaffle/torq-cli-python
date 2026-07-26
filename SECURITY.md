# Security model

## Credentials and providers

`Claude` and `Codex` use first-party authenticated subscription surfaces and
never extract their underlying tokens. `Grok` uses an authenticated ACP surface
when policy permits. `Kimi`, `Z.ai`, and `DeepSeek` use direct adapters whose
tokens are retrieved only through the credential backend; plaintext config
fields are rejected. An explicitly supplied external env file may be used as a
local compatibility credential source. TORQ never copies that file, serializes
its values, or exposes more than the selected provider credential to a child
process.

An explicit external env file is accepted only when it is already owner-only:
POSIX mode `0600` or a Windows DACL containing only the owner. TORQ refuses a
permissive file rather than mutating operator-owned permissions. Alibaba Token
Plan overrides are restricted to canonical
`https://token-plan.<region>.maas.aliyuncs.com/apps/anthropic` URLs, without
userinfo, query, or fragment.

The implemented native backends are Windows Credential Manager, macOS Keychain,
and Linux Secret Service through a verified `keyring` backend. Credential values
enter through an attended no-echo terminal only; redirected input is rejected.
Configuration stores opaque credential references, never values. The attended
headless encrypted-file backend is implemented behind explicit backend and
absolute store-root selection. Its passphrase is accepted only by a local
no-echo terminal; there is no unattended, environment, argument, pipe,
plaintext, or automatic fallback channel.

Fleet chat has a narrower credential boundary than governed `torq run`: direct
chat providers currently require an explicit absolute external env file. Fleet
chat does not resolve platform-keychain or headless-vault references. The
browser receives neither credentials nor process handles.

## Sandbox and approval

Builder and refinement work occurs in an isolated worktree or copy sandbox.
Protected paths are denied for both reads and writes before content can enter a
prompt. Network and commands are deny-by-default, child environments are
filtered, resource ceilings halt fail-closed, and cancellation terminates the
process tree. The primary worktree changes only after explicit approval of the
audited diff against its pinned starting tree hash.

## Redaction and evidence

The shared pattern registry runs before provider egress and again before
persistence. Receipts are sequence-numbered and hash-chained; artifacts carry
content hashes and are encrypted at rest; an Ed25519 terminal manifest seals
the chain. The CLI persists one signing identity in the run root, outside each
per-run receipt directory, and caches its public key beside the private key. The
private key and public-key cache are owner-only through mode `0600` on POSIX
and protected DACLs granting full control only to the current user SID on
Windows. Failure to apply or verify either protection blocks signing, and
verification rejects permissive permissions as `trust_anchor_unsafe`.
`torq evidence verify` derives the trusted public key from the independently
protected long-lived private identity and requires both the public-key cache
and manifest signer to match it. Replacing the cache together with a newly
generated and self-signed chain therefore fails as `trust_anchor_substituted`.

TORQ receipts are tamper-resistant, not tamper-proof. The receipt hash chain,
artifact hashes, restrictive file permissions, encryption at rest, and signed
terminal manifest make casual or receipt-directory-only replacement detectable.
An attacker with the operator's own OS privileges who can also read or replace
the long-lived private identity can still rewrite and re-sign a complete local
chain. Moving that identity into a non-exportable platform-keychain key and
remote anchoring are future hardening work and are not implied.

`torq trust readiness` makes this boundary machine-readable. The current local
implementation exits 3 with `production_signing_identity_exportable` and
`production_receipt_anchor_not_independent`; owner-only permissions do not
upgrade either result. A future ready result requires active signer and remote
inclusion/checkpoint probes through the contract documented in
`docs/architecture/production-trust-hardening-decision.md`. Adapter-declared
labels and adapter-self-verified probes are insufficient: the configured trust
verifier must independently authenticate the platform identity and the pinned,
fresh remote checkpoint.

Provider credentials must live behind the platform keychain or the documented
attended encrypted-file fallback. Agent subprocesses receive a filtered
environment and protected paths are denied before content enters a prompt.

Governed chat is production-enabled only on Windows, where the provider is
created suspended and assigned to a kill-on-close Job Object before execution.
Linux production chat fails closed with
`distinct_identity_system_broker_required`; the same-UID user-systemd adapter
is experimental evidence only. macOS fails closed with
`owned_process_strong_containment_unavailable` until a separately distributed,
signed, notarized native owner can prove the containment contract.

The same platform gate applies to process-backed Claude-compatible live stages:
they run through `OwnedProcess` and are production-enabled only on Windows.
OpenAI direct HTTPS remains a non-process adapter and does not inherit ambient
proxy configuration.

## Telemetry

TORQ CLI sends no product telemetry; vendor SDKs/CLIs may contact their own
auth/update/diagnostic endpoints outside TORQ's control.
