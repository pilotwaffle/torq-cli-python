# Production-readiness audit — 2026-07-24

Commit baseline: `767b1db5d70a83aee1e98b1ebc93360bb471ecae`
on the stacked release-candidate branch `feat/t35-clean-machine-credentials`.
This baseline is **not merged to `main`**: it is composed of draft PRs #7, #8,
and #9 above `main` at `148175cfb6fc677b5a8f97da38af115aee912ab3`.
This documentation-only T-32 refresh is a descendant of that assessed source.

Verdict: the assessed candidate passes the implemented security and quality
gates, and the previously open live-provider and macOS/Linux native-credential
findings are resolved by actual evidence. It is **not ready to publish as
v0.1.0** until the High release blockers below are closed. Evidence produced by
mock or injected transports is not promoted to live evidence.

## Evidence and repository state

- Six-provider live smoke: `docs/evidence/live-smoke-2026-07-24.json`, SHA-256
  `9B09AC38AAA4092860EFA4FE8AF9241D7C3F0C876ADE1852FC92F88F5290EE17`.
- Governed heterogeneous live run: `docs/evidence/governed-live-2026-07-24.json`,
  SHA-256
  `77CE748C5054DE8B525835287CA32F7DCB17B79101C66E71A99B8E450016B262`.
  Four stages ran across Anthropic, DeepSeek, and OpenAI, stopped at
  `awaiting_approval`, and recorded `application_performed: false`.
- The committed T-33 receipt bundle verifies offline against its separately
  exported public key. Verification was replayed during this audit and returned
  `verified`.
- One exact T-35 wheel, SHA-256
  `24C0286B6B1E8D981AF577FB4DEBB8A2D60B312412F0D6E36A3FFCF53E96AD32`,
  passed installed-wheel native round trips on fresh hosted macOS and Linux
  runners. The committed reports have SHA-256 values
  `F93E15395D0CF0168D80894EA65BEF90004CC1E45208353622E5B4E75B52BC5A`
  and `36A586506437C6A667A2E7D3F2986F1D4941BD9F3CFD862BD644C6FD565F34BA`.
- Native-credential workflow run
  [30105467956](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30105467956)
  passed `build-wheel`, `native-macos`, and `native-linux` at the assessed HEAD.
  The committed native reports came from run
  [30104750363](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30104750363)
  at `3b3bb95`; the only later source change was bounded workflow retry handling.
- Draft PRs #7, #8, and #9 are cleanly mergeable and their Windows, macOS,
  Linux, and headless Linux quality matrices are green. Their draft/unmerged
  status is nevertheless a release-state blocker, not merged-main evidence.

## Credential handling — implemented; attended native evidence is OS-specific

Windows Credential Manager, macOS Keychain, and Linux Secret Service expose
read/write/revoke adapters through verified platform-specific `keyring` 25.7
backends. Direct connectors receive only the selected resolved value; config
contains opaque references; attended input is no-echo; redirected input is
rejected; and errors collapse to secret-free findings. The headless encrypted-
file contract remains unimplemented and fails closed. It is not a claimed
v0.1.0 capability.

Fresh hosted installed-wheel evidence now proves macOS Keychain and Linux
Secret Service store/resolve/revoke/absence behavior without persisted test
secrets. Windows Credential Manager passed locally from both the editable
checkout and an isolated wheel installation, using an ephemeral value that was
revoked immediately. That is valid Windows implementation evidence, but it is
not the fresh-machine Windows evidence required by the current release notes.

## Sandbox escape — resolved at the assessed baseline

Path traversal, symlink escape, protected `.env` reads, concurrent workspace
access, dirty-primary refusal, command/network allowlists, environment
filtering, resource limits, and process-tree cancellation have automated
coverage. The four hosted quality jobs exercise the full suite on Windows,
macOS, Linux, and headless Linux. No sandbox finding remains open at High.
Sandbox re-test: `tests/test_phase4_safety.py` passed locally during this audit;
the same suite also passed in the current hosted quality matrices.

## Receipt-chain integrity — resolved within the documented local trust model

Sequence continuity, hash chaining, artifact hashes, schema/profile/policy
consistency, Ed25519 manifest seals, encrypted artifacts, and offline
verification are tested with seeded tampering. The `.pub`-substitution attack
is rejected because verifier trust is derived from the protected persistent
private identity rather than the mutable public cache. Owner-only POSIX modes
and Windows DACLs are enforced on both private identity and public cache.

The guarantee is tamper-resistant, not tamper-proof: compromise of the
operator's own OS identity and persistent private signing identity can permit a
consistent re-sign. Non-exportable platform signing keys and remote anchoring
remain future hardening.

## Dual redaction — resolved

One versioned pattern registry enforces both pre-provider egress and
pre-persistence redaction. Blocking patterns fail closed with labels only. The
T-21, T-33, and T-35 committed reports are secret-free; no private signing key
is committed.

## Approval boundary — resolved for the demonstrated proposal-only path

Primary files remain untouched before explicit approval. Application is pinned
to the captured primary tree and exact audited content hash; drift refuses with
a re-run/re-baseline instruction. No push or merge role exists. The T-33 target
was immutable and proposal-only, no `approval_apply` transition exists in the
receipt chain, and the final verdict is `awaiting_approval`.

The installed `torq run --live` command still fails closed with
`live_dispatcher_required`; the production adapter was exercised by the
explicit T-33 runner and is not wired into the default CLI transport factory.
This is an accepted v0.1.0 scope boundary only while README and release material
continue to say so. It must not be advertised as turnkey standalone live
dispatch.

## Extraction conformance — resolved

MMH normalization fixtures, retry/budget behavior, graph profiles, routing
policy v3.1.3, and redaction match their frozen reference projections. The
named mutation suite includes a deliberately divergent normalization mutant
and 13 other security/governance mutants.

## Open findings and release disposition

- **High — unmerged release candidate.** T-21, T-33, and T-35 are in draft PRs
  #7, #8, and #9 rather than protected `main`. Resolve by reviewing and merging
  the stack in dependency order, then requiring the protected-branch quality
  matrix on the resulting mainline commit.
- **High — missing fresh-machine Windows credential attestation.** The release
  notes require clean-machine credential-backend access on all three desktop
  OSes. Current Windows evidence is local isolated-wheel evidence. Resolve with
  a fresh Windows host installing the exact candidate artifact and recording
  secret-free store/resolve/revoke/absence evidence, or explicitly narrow and
  approve the v0.1.0 release criterion before publication.
- **High — release identity and immutable artifacts absent.** No signed
  `v0.1.0` tag or published artifact hashes exist. Resolve only after the source
  blockers close and King Flowers explicitly authorizes signing/publication.
- **Medium — local signing identity is exportable.** Same-principal compromise
  can replace and reuse the private identity. Accepted for v0.1.0 with the
  documented limitation; non-exportable key storage and remote receipt
  anchoring are future hardening.
- **Medium — headless encrypted-file credentials are unavailable.** Selection
  fails closed. Accepted only because v0.1.0 does not claim this backend.
- **Medium — default CLI has no production live transport factory.** The
  authorized T-33 script proves the adapter and governed path, while installed
  `torq run --live` refuses. Accepted only as a documented v0.1.0 boundary.

## Repository controls — active on `main`

The standalone public repository is `pilotwaffle/torq-cli-python`. GitHub branch
protection was rechecked on 2026-07-24: strict required checks are
`quality-windows-py311`, `quality-macos-py311`, `quality-linux-py311`, and
`headless-linux-py311`; admin enforcement, linear history, and conversation
resolution are enabled; force-push and deletion are disabled. Required commit
signatures are not enabled, so the release process must independently verify
the signed tag required by T-36.
