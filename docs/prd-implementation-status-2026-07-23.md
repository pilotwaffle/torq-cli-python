# PRD r5 implementation status — 2026-07-24

Current assessed implementation baseline:
`b34ff31ceca8975784aca7c8159506103a46bb12` on PR #12. The T-21/T-33/T-35/T-32
consolidation is merged to protected `main` at
`bdf329480a56f91fbe801c85ed8df663a03f5490` with a green four-platform quality
run. PR #12 adds the remaining fresh hosted Windows credential evidence.

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
| T-13–T-20 | Complete at the injected-transport boundary | Six connector contracts, auth/health status, explicit credential-source handling, and credential-free conformance pass. There is no standalone production transport factory. |
| T-21 | Complete | The manual-only runner produced `docs/evidence/live-smoke-2026-07-24.json`: all six provider smokes passed with resolved-model identity and usage metadata. The report is machine-generated, secret-free, and explicitly non-receipt-backed. SHA-256: `9B09AC38AAA4092860EFA4FE8AF9241D7C3F0C876ADE1852FC92F88F5290EE17`. |
| T-22–T-27 | Complete | Isolation, execution policy, governed orchestration, bounded repair/re-audit, approval, usage, encrypted artifacts, and receipt verification pass. The `.pub`-swap exploit is replayed end-to-end and rejected as `trust_anchor_substituted`; unsafe/missing identity variants also fail closed. |
| T-28–T-31 | Complete at the implemented boundary | Setup, dry-run, injected live orchestration, cancellation/resume, effective status, and evidence verification exist. Standalone `torq run --live` fails before creating a run with `live_dispatcher_required`. |
| T-32 | Complete for PR #12 baseline `b34ff31` | The refreshed audit resolves live-provider and three-OS native credential evidence, verifies the receipt bundle and repository controls, and records the remaining T-36 blockers: merge PR #12 through protected `main`, confirm its mainline checks, then create the explicitly authorized signed release identity/artifacts. |
| T-33 | Complete | The authorized proposal-only runner dispatched G1D/G1R/Builder/G2A across Anthropic, DeepSeek, and OpenAI, verified exact profile-bound model identities, and stopped at `awaiting_approval`. The portable signed receipt bundle verifies against its separately exported public key; no application transition occurred. Report SHA-256: `77CE748C5054DE8B525835287CA32F7DCB17B79101C66E71A99B8E450016B262`. |
| T-34 | Complete | `SECURITY.md` distinguishes the authenticated private identity from the mutable public-key cache and states the same-principal/private-identity limitation. |
| T-35 | Complete for native attended backends | Fresh hosted Windows, macOS, and Linux runners verified and installed the same exact wheel, then passed native store/resolve/revoke/absence operations with generated ephemeral values and `secret_persisted: false`. The Windows evidence report is `docs/evidence/native-credential-windows-2026-07-24.json`, SHA-256 `54F026CC0CDB0D2D8957519B648072B62121F07892224C1159697BE464BAF8FE`. The separately gated headless encrypted-file contract remains unimplemented and fails closed. |
| T-36 | Correctly withheld | Publication remains gated by merging PR #12 through protected `main`, passing the resulting mainline matrix, and receiving explicit operator authorization for signing/publication. |

## Verification

- Test collection: 476 tests across 25 test files; the refreshed local suite
  passes with four intentional live/environment skips (472
  executed tests).
- Strict mypy: pass across 48 Python source files.
- Ruff: pass.
- Named security/governance mutants: 14/14 killed.
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

## Remaining closure work and ownership

Fresh Windows clean-machine evidence completed in hosted run
[`30123795749`](https://github.com/pilotwaffle/torq-cli-python/actions/runs/30123795749).
The exact-wheel Windows Credential Manager round trip passed store, resolve,
revoke, and absence checks with `secret_persisted: false`. Machine report:
`docs/evidence/native-credential-windows-2026-07-24.json` (SHA-256
`54F026CC0CDB0D2D8957519B648072B62121F07892224C1159697BE464BAF8FE`).

| Task | Required closure evidence | Operator-owned prerequisite | Codex scope after authorization |
| --- | --- | --- | --- |
| T-36 | Merge PR #12 through protected `main`, pass the resulting mainline checks, then create a signed `v0.1.0` tag and immutable artifact hashes | Review/merge authority and explicit signing/publication authorization | Prepare and verify remaining evidence; tag and publish only within explicit authorization. |

External evidence production is therefore in scope for Codex once King Flowers
provides the required authority and systems. Credential ownership, model grants,
spend approval, clean-machine access, signing identity, and the final decision to
publish remain King Flowers’ handoff responsibilities.
