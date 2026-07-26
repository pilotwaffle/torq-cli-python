# Production trust hardening gate

Status: accepted design boundary, 2026-07-26. The repository implementation is
the fail-closed capability contract and readiness probe. Platform signers and a
remote transparency service are not bundled in the `torq-cli` wheel.

## Decision

The local `FileRunKeyStore` and same-volume manifest anchor remain supported as
tamper-resistant local evidence, but they are never described as production
hardened. `torq trust readiness` exits 3 and names both residuals:

- `production_signing_identity_exportable`; and
- `production_receipt_anchor_not_independent`.

TORQ will not add a same-filesystem generation counter, encrypt an exportable
private key and call it non-exportable, or treat ACLs as a hardware boundary.
Those measures do not stop an attacker with the operator's filesystem identity
from restoring or consistently re-signing state.

The adapter contract is `safety.production_trust`. A ready result requires all
of the following in the same invocation:

1. a key generated inside an OS-isolated or hardware signing backend;
2. a backend declaration that the private key is non-exportable;
3. an active random signing challenge verified with that identity;
4. integration of that signer into the receipt path, not a sidecar demo;
5. an independently operated append-only remote transparency service;
6. a submitted random-digest probe with a verified inclusion proof and signed
   checkpoint; and
7. integration of remote anchoring into receipt terminalization and
   verification.

Metadata without successful active probes cannot produce `ready`. Adapter and
service errors reduce to stable blocked findings; vendor details do not escape
the CLI boundary.

## Why a cross-platform wheel cannot complete this alone

### Windows

Microsoft documents the Platform Crypto Provider as a CNG key-storage provider
that uses the TPM and prevents extraction of private keys. A future Windows
adapter must create an ECDSA P-256 persisted key with the Platform Crypto
Provider, leave export policy disabled, sign through `NCryptSignHash`, and
verify provider/name/export-policy properties at every readiness probe. This is
native CNG/TPM integration, not the current Ed25519 file-key implementation.

Primary references:

- <https://learn.microsoft.com/windows/win32/seccertenroll/cng-key-storage-providers>
- <https://learn.microsoft.com/windows/win32/api/ncrypt/nf-ncrypt-ncryptcreatepersistedkey>
- <https://learn.microsoft.com/windows/security/operating-system-security/system-security/cryptography-certificate-mgmt>

Acceptance evidence: a clean TPM 2.0 Windows runner creates the key in the
Platform Crypto Provider; private-key export fails; 100 sign/verify cycles
pass; key substitution and provider downgrade fail closed; the receipt
certificate identifies the P-256 algorithm and key ID; uninstall/reboot does
not silently generate a replacement identity.

### macOS

Apple documents Secure Enclave signing as NIST P-256 only, with private key
material unavailable to the application. Data-protection Keychain access is
determined by the host executable's code-signing entitlements and is available
only in a user context. A future adapter therefore requires a signed/notarized
native helper or application bundle and an evidence-schema algorithm migration;
an ordinary Python wheel cannot provide the distribution identity or turn the
current Ed25519 key into a Secure Enclave key.

Primary references:

- <https://developer.apple.com/documentation/security/protecting-keys-with-the-secure-enclave>
- <https://developer.apple.com/documentation/cryptokit/secureenclave/p256>
- <https://developer.apple.com/documentation/Technotes/tn3137-on-mac-keychains>
- <https://developer.apple.com/documentation/xcode/creating-distribution-signed-code-for-the-mac>

Acceptance evidence: a clean Apple-silicon macOS runner installs a notarized
helper, creates a Secure Enclave P-256 key, demonstrates that external
representation is unavailable, signs after reboot, refuses an unsigned helper
or unexpected Team ID, and verifies receipts across the schema migration.

### Linux

The kernel documents Trusted Keys rooted in a TPM Storage Root Key and notes
that trust-source suitability depends on the deployment. A future Linux
adapter must use TPM 2.0 signing operations (not merely store an exportable
Ed25519 seed in Secret Service), bind persistent key identity to an explicit
TPM policy, and define recovery for TPM replacement. Generic keyring storage is
not proof of non-exportability.

Primary references:

- <https://docs.kernel.org/security/keys/trusted-encrypted.html>
- <https://docs.kernel.org/security/keys/core.html>

Acceptance evidence: clean TPM-backed Linux runners show that the private key
never enters userspace, sign through the TPM, reject a software-provider
downgrade and PCR-policy mismatch, survive reboot, and produce portable public
verification material.

### Remote anchoring

An independent service is required because state on the same operator-owned
volume can be rolled back together. Sigstore documents Rekor as a REST-backed,
append-only, cryptographically verifiable transparency log with signed tree
heads. Rekor is a reference architecture, not an implicit TORQ production
dependency or a claim that public submission is suitable for private run
metadata.

Primary references:

- <https://docs.sigstore.dev/logging/overview/>
- <https://docs.sigstore.dev/about/security/>

The future service contract accepts only a domain-separated digest, run key ID,
manifest generation, and schema version; it returns a record ID, inclusion
proof, and signed checkpoint. It must not receive prompts, artifacts,
credentials, provider output, or receipt prose. Verification pins the service
trust root independently of the run volume and rejects missing, stale,
inconsistent, or unmonitored checkpoints.

Acceptance evidence: staging submits and verifies an inclusion proof; removing
or rolling back the local manifest is detected against the remote record;
split-view/invalid checkpoint fixtures fail closed; retry is idempotent; a
service outage blocks hardened terminalization without losing the local chain;
and a privacy review confirms digest-only egress.

## Delivery sequence

1. Approve the receipt schema migration from Ed25519 to an algorithm-agile
   certificate before platform adapters are written.
2. Select and threat-model the remote log operator, data retention, monitoring,
   authentication, and outage policy.
3. Implement one platform adapter at a time behind `ProductionSigner` and run
   its clean-machine acceptance suite.
4. Implement `ReceiptAnchor`, pin its trust root outside the run volume, and
   integrate submit/verify into terminalization and fleet projection.
5. Only after the active readiness probe returns `ready` may release evidence
   use the phrase **production trust hardened**.
