# Release trust model — four distinct trust levels

Status: design record for the v0.2.0 release trust pack (R0). This document
defines what each trust mechanism does and does NOT prove. The four levels are
**distinct and non-equivalent**; none of them may be presented as evidence for
another.

## 1. CI provenance (recorded in R0)

What it is: a machine-readable record of where, when, and from what source the
release artifacts were built — workflow run identifiers, candidate source SHA,
builder script blob SHA, contract digest, artifact SHA-256 hashes, tool
versions (`release-evidence.json`, `provenance.json`, `SHA256SUMS`,
GitHub Actions artifact attestations where configured).

What it proves: the artifacts attached to the build run were produced by the
identified workflow from the identified source commit.

What it does NOT prove: that the bytes distributed later are these bytes
(publication-time integrity), that anyone vouches for the content (signing),
or anything about TORQ governed-run receipts.

## 2. Package signing (NOT performed in R0)

What it is: a cryptographic signature (e.g. sigstore keyless or an offline
signing key) over the exact artifact bytes that will be attached to the release.

Hard requirement: the signature MUST cover the identical byte sequence that is
attached to the eventual release. Signing one build and publishing another is
a release-blocking integrity violation. During R0 no package signature exists
and none may be claimed.

## 3. Git tag signing (NOT performed in R0)

What it is: a signed tag object (GPG/SSH/sigstore gitsign) anchoring the
release commit in git history.

Distinct from package signing: a signed tag vouches for the source commit, not
for the built artifact bytes. Both may exist and still not imply each other.
No tag (signed or unsigned) is created during R0.

## 4. TORQ production receipt signing (product trust domain)

What it is: TORQ's own governed receipt chain — the product's evidence model
(SPEC §12). In v0.2.0 the local signer is tamper-resistant but not
production-hardened: `production_signing_identity_exportable` and
`production_receipt_anchor_not_independent` remain unresolved (see
`torq trust readiness`).

Hard rule: release signatures (levels 2–3) and CI provenance (level 1) are
NEVER presented as production receipt trust, and production receipt trust is
never presented as release attestation. They are different trust domains with
different roots of trust.

## R0 boundary

During R0 only level 1 artifacts are produced, and only as workflow artifacts.
No tag, no GitHub release, no PyPI publication, no signature, no attestation
of publication bytes. Release Execution is a separate authorization against
one exact `origin/main` SHA.
