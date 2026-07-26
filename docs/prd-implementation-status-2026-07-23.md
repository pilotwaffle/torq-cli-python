# PRD r5 implementation status — 2026-07-24

This is the v0.1.0 historical record. Since that release, the attended headless
encrypted-file backend and installed `torq run --live` dispatcher factory have
been implemented. Governed Fleet chat is production-enabled on Windows; Linux
and macOS fail closed for the platform-specific containment reasons documented
in `architecture/governed-chat-runtime.md`. Local production trust remains
blocked by an exportable signing identity and a same-volume receipt anchor. No
v0.2.0 protected-main CI, tag, signature, or published release is claimed by
this update.

Current protected-main baseline: `8d5f014218f3e7d1ff2f91c1ae3a28abed425fb9`.
Hosted quality run
[`30160472701`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30160472701)
passed Windows, macOS, Linux, and headless Linux. The Release 2 accounting
candidate documented below is based on that commit and adds the immutable rate
identity, durable dispatch registry, rollback anchor, cross-run coverage,
reservation expiry, and signed reconciliation contracts required by Fleet UI
build-order step 9.

The final T-35 native-credential workflow at the assessed baseline completed
successfully:

- native evidence: [`30105467956`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30105467956);
- quality push: [`30105470143`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30105470143).

The quality run passed Windows, macOS, Linux, and headless Linux. The native
evidence run passed wheel build, macOS Keychain, and Linux Secret Service.

T-35 clean-machine native evidence later completed successfully at source commit
`3b3bb957a055d989eeb41b6a1eff88966d9f3390` in hosted run
[`30104750363`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30104750363).

Previous orchestration-phase hosted quality runs:

- pull request: [`30064851658`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30064851658);
- push: [`30064849331`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30064849331).

Both runs completed successfully for Windows, macOS, Linux, and headless Linux.
This report update is a documentation-only descendant of the assessed source
commit.

“Implemented” means the repository boundary and deterministic tests exist and
pass. It does not promote injected, recorded, or mock connector results into
live-provider evidence. “Operator-gated” means closure depends on authority or
systems that are not present in this checkout; it does not imply missing code is
complete.

| Task | Status | Evidence boundary |
| --- | --- | --- |
| T-02 | Complete | Extraction audit and REUSE/WRAP/REBUILD verdicts are recorded. |
| T-03 | Complete at the decision-artifact boundary | The closed provider-surface matrix, dated provenance labels, and downstream decisions exist. Live effectiveness belongs to T-21. |
| T-05 | Complete | Standalone Python repository, wheel/pipx distribution, OS implications, and one version source are documented. |
| T-07 | Complete | Hermetic Windows/macOS/Linux/headless CI is green. `main` currently requires the strict four-job matrix, admin enforcement, linear history, and conversation resolution; force-push and deletion are disabled. |
| T-08–T-12 | Complete | Provider-neutral engine, graph, routing, redaction, retry/budget contracts, and conformance fixtures pass. |
| T-13–T-20 | Complete; v0.2.0 extended | Six connector contracts, auth/health status, explicit credential-source handling, and credential-free conformance pass. The v0.2.0 candidate adds the installed live transport factory. |
| T-21 | Complete | The manual-only runner produced `docs/evidence/live-smoke-2026-07-24.json`: all six provider smokes passed with resolved-model identity and usage metadata. The report is machine-generated, secret-free, and explicitly non-receipt-backed. SHA-256: `9B09AC38AAA4092860EFA4FE8AF9241D7C3F0C876ADE1852FC92F88F5290EE17`. |
| T-22–T-27 | Complete | Isolation, execution policy, governed orchestration, bounded repair/re-audit, approval, usage, encrypted artifacts, and receipt verification pass. The `.pub`-swap exploit is replayed end-to-end and rejected as `trust_anchor_substituted`; unsafe/missing identity variants also fail closed. |
| T-28–T-31 | Complete; v0.2.0 extended | Setup, dry-run, orchestration, cancellation/resume, effective status, and evidence verification exist. The v0.2.0 candidate adds the installed fail-closed `torq run --live` dispatcher factory. |
| T-32 | Complete for merged-main baseline `6d4d564` | The refreshed audit resolves live-provider and three-OS native credential evidence, verifies the receipt bundle and repository controls, and records only the explicitly authorized signed release identity/artifacts as the remaining T-36 gate. |
| T-33 | Complete | The authorized proposal-only runner dispatched G1D/G1R/Builder/G2A across Anthropic, DeepSeek, and OpenAI, verified exact profile-bound model identities, and stopped at `awaiting_approval`. The portable signed receipt bundle verifies against its separately exported public key; no application transition occurred. Report SHA-256: `77CE748C5054DE8B525835287CA32F7DCB17B79101C66E71A99B8E450016B262`. |
| T-34 | Complete | `SECURITY.md` distinguishes the authenticated private identity from the mutable public-key cache and states the same-principal/private-identity limitation. |
| T-35 | Complete for native attended backends; v0.2.0 headless implementation added | Fresh hosted Windows, macOS, and Linux runners verified v0.1.0 native operations. The v0.2.0 candidate adds the explicit attended headless encrypted-file backend; fresh clean-machine headless effectiveness remains a separate release gate. |
| T-36 | Complete | Signed annotated tag `v0.1.0` targets protected-main commit `f6df23e`; GitHub reports its registered Ed25519 signature `verified: true` with reason `valid`. The public release contains the wheel, source archive, `SHA256SUMS`, and the signed checksum manifest. Re-downloaded hashes and signature verified, and the published wheel passed an isolated install smoke test. |

## Verification

- Test collection: 551 tests across 34 test files; the refreshed local suite
  passes with four intentional live/environment skips (547 executed tests).
- Strict mypy: pass across 58 Python source files.
- Ruff: pass.
- Named security/governance mutants: 23/23 killed on Windows (22/22 applicable
  on POSIX). M01-M14 cover config, registry, and hermeticity; M15-M23 cover the evidence layer (signing encoder,
  receipt sanitizer, transition authority, recovery-abandonment guard, lane
  state projection, monetary accounting, and binary evidence writes).
- Source distribution and wheel builds: pass; hosted jobs perform isolated wheel
  smoke tests.
- Hosted PR run `30064851658` and push run `30064849331`: all four jobs green at
  assessed commit `5138c3542ab3b3065960fa65c0c4b59c03d7cc9b`.
- Branch protection rechecked on 2026-07-24: strict required four-job matrix,
  admin enforcement, linear history, conversation resolution, force-push
  disabled, and deletion disabled.
- Automated security regression: a substituted owner-only `.pub` cache plus a
  forged `mode: live` receipt chain and matching manifest is rejected as
  `trust_anchor_substituted` while the private identity remains byte-for-byte
  unchanged.
- Operator replay reported on 2026-07-24: key wipe, anchor deletion, and full
  consistent reforge variants were rejected, including `trust_identity_unsafe`.
- T-35 Windows evidence on 2026-07-24: both the editable checkout and an isolated
  installation of the built wheel selected `windows_credential_manager`, stored
  and resolved a generated test-only value, revoked it, and verified absence.
  No credential value was printed or retained; artifacts and the isolated
  environment live only under `E:\tmp`. The final tested wheel SHA-256 is
  `BB47AC3136C2AEC9E9DE8F87C91E0CEDB7A7EBA63077D2E2203902346E6288F3`.
- T-33 live evidence on 2026-07-24: the machine-generated report at
  `docs/evidence/governed-live-2026-07-24.json` records four governed stages
  across three real providers, a verified receipt chain, bounded configured
  cost, and `application_performed: false`. The committed chain can be checked
  with `torq evidence verify --run-root docs/evidence/t33-governed-live-2026-07-24/run --trusted-public-key docs/evidence/t33-governed-live-2026-07-24/.torq-receipt-signing-key.pub`.
- T-35 clean-machine evidence on 2026-07-24: hosted run
  [`30104750363`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30104750363)
  passed build-wheel, native-macos, and native-linux. Both native jobs verified
  and installed the same wheel hash, then recorded successful store, resolve,
  revoke, and absence checks with `secret_persisted: false`. Machine reports:
  `docs/evidence/native-credential-macos-2026-07-24.json` (SHA-256
  `F93E15395D0CF0168D80894EA65BEF90004CC1E45208353622E5B4E75B52BC5A`)
  and `docs/evidence/native-credential-linux-2026-07-24.json` (SHA-256
  `36A586506437C6A667A2E7D3F2986F1D4941BD9F3CFD862BD644C6FD565F34BA`).

## T-36 closure evidence

Fresh Windows clean-machine evidence completed in hosted run
[`30123795749`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30123795749).
The exact-wheel Windows Credential Manager round trip passed store, resolve,
revoke, and absence checks with `secret_persisted: false`. Machine report:
`docs/evidence/native-credential-windows-2026-07-24.json` (SHA-256
`54F026CC0CDB0D2D8957519B648072B62121F07892224C1159697BE464BAF8FE`).

| Task | Release evidence | Authorization | Result |
| --- | --- | --- | --- |
| T-36 | Signed `v0.1.0` tag, immutable artifact hashes, checksum signature, public GitHub release, and clean re-download verification | King Flowers explicitly authorized signing/publication on 2026-07-24 | Complete: `https://github.com/pilotwaffle/torq-cli-python/releases/tag/v0.1.0`. |

All PRD r5 tasks through T-36 remain complete at their documented v0.1.0
boundaries. Headless encrypted-file credentials and the turnkey installed live
dispatcher are now implemented. Remote receipt anchoring and non-exportable
signing identity remain external production-trust prerequisites.
