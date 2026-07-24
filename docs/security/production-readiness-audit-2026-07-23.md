# Production-readiness audit — 2026-07-24

Commit baseline: `6d4d5647001b35dfbea592e32a0c36370bfcb93c` on protected
`main`, including merged PR #12 and the committed fresh-Windows evidence.
Hosted quality run
[30124572287](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30124572287)
passed Windows, macOS, Linux, and headless Linux. This documentation-only audit
refresh is a descendant of the assessed source.

Verdict: the assessed candidate passes the implemented security and quality
gates, and the previously open live-provider and three-OS native-credential
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
- Fresh three-OS native-credential workflow run
  [30123795749](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30123795749)
  passed `build-wheel`, `native-windows`, `native-macos`, and `native-linux` at
  `b34ff31`. The Windows report SHA-256 is
  `54F026CC0CDB0D2D8957519B648072B62121F07892224C1159697BE464BAF8FE`.

## Credential handling — implemented; attended native evidence is OS-specific

Windows Credential Manager, macOS Keychain, and Linux Secret Service expose
read/write/revoke adapters through verified platform-specific `keyring` 25.7
backends. Direct connectors receive only the selected resolved value; config
contains opaque references; attended input is no-echo; redirected input is
rejected; and errors collapse to secret-free findings. The headless encrypted-
file contract remains unimplemented and fails closed. It is not a claimed
v0.1.0 capability.

Fresh hosted installed-wheel evidence now proves Windows Credential Manager,
macOS Keychain, and Linux Secret Service store/resolve/revoke/absence behavior
without persisted test secrets. All three jobs consumed the same build artifact;
the Windows job verified wheel SHA-256
`0B9B527DC764B35795585C35F0EAD099474DB1E9990F27F9CC213BC0C016B7B2`,
used a generated ephemeral value, revoked it, and verified absence.

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

- **High — release identity and immutable artifacts absent.** No signed
  `v0.1.0` tag or published artifact hashes exist. Resolve only after the source
  and evidence gates now recorded on protected `main` and King Flowers
  explicitly authorizes signing/publication.
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
