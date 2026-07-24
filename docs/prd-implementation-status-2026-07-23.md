# PRD r5 implementation status — 2026-07-24

Merged implementation baseline: `9b5e77b174626c794bfe76fe3940b09b3c5efb9e`
(`main`, merged PR #5). Receipt/security base: `9bac9246faf93c00c7b69b6828d397347e48a536`
(merged PR #4). The T-35 native-credential phase is being verified on
`feature/native-credential-backends`; hosted run identifiers will be added after
the branch is pushed.

Previous matching hosted quality runs:

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
| T-21 | Operator/integration gated | `LiveSmokeRunner` and its report schema exist, but a real run needs approved credentials, exact model grants, and concrete live provider callables supplied by an authorized integration. |
| T-22–T-27 | Complete | Isolation, execution policy, governed orchestration, bounded repair/re-audit, approval, usage, encrypted artifacts, and receipt verification pass. The `.pub`-swap exploit is replayed end-to-end and rejected as `trust_anchor_substituted`; unsafe/missing identity variants also fail closed. |
| T-28–T-31 | Complete at the implemented boundary | Setup, dry-run, injected live orchestration, cancellation/resume, effective status, and evidence verification exist. Standalone `torq run --live` fails before creating a run with `live_dispatcher_required`. |
| T-32 | Complete for this source baseline | The production-readiness audit records resolved and open findings. It must be rerun after external evidence or implementation changes. |
| T-33 | Operator/integration gated | Recorded/mock heterogeneous composition and receipt verification pass. Closing the live criterion needs at least two authorized live providers plus a safe application target. |
| T-34 | Complete | `SECURITY.md` distinguishes the authenticated private identity from the mutable public-key cache and states the same-principal/private-identity limitation. |
| T-35 | Native implementation complete; cross-platform clean-machine evidence pending | Wheel/sdist construction and hosted wheel smoke pass on all three OS families. Native Windows Credential Manager, macOS Keychain, and Linux Secret Service read/write/revoke operations now use verified platform-specific `keyring` backends and opaque references. An ephemeral Windows round trip was verified and revoked on 2026-07-24. macOS/Linux installed-artifact evidence remains operator-gated. The headless encrypted-file contract remains unimplemented and fails closed. |
| T-36 | Correctly withheld | Signed tag, release publication, and final branch/release evidence remain gated by T-21, live T-33, applicable T-35 evidence, a refreshed T-32 audit, and explicit operator authorization. |

## Verification

- Test collection: 460 tests across 23 test files; the refreshed local suite
  passes with four intentional live/environment skips (456
  executed tests).
- Strict mypy: pass across 45 Python source files.
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

## Remaining closure work and ownership

| Task | Required closure evidence | Operator-owned prerequisite | Codex scope after authorization |
| --- | --- | --- | --- |
| T-21 | Redacted independent live-smoke results for the required providers/models | Provide or authorize access to approved credential sources, exact grants, endpoints, and any billable calls | Run the probes through an approved live integration, preserve secret-free receipts, and update the audit. |
| T-33 | A governed heterogeneous live run using at least two real providers against a safe target | Approve providers, cost/risk limits, and the application target; provide the concrete production transport integration if it remains out of repo | Execute and verify the run, fault routing, receipts, proposal boundary, and negative cases. |
| T-35 | Clean-machine install, artifact verification, and native credential round trips on Windows, macOS, and Linux | Supply clean macOS/Linux machines or VMs and attended keychain prompts; decide whether the separately gated headless encrypted-file contract is required for v0.1.0 | Drive installs and collect secret-free evidence. Windows native access is locally verified; macOS/Linux effectiveness is not yet claimed. |
| T-36 | Refreshed T-32 audit, signed `v0.1.0` tag, immutable artifacts/hashes, and release/branch evidence | Explicitly authorize signing and publication after all prerequisite gates pass | Prepare, verify, tag, and publish only within that authorization. |

External evidence production is therefore in scope for Codex once King Flowers
provides the required authority and systems. Credential ownership, model grants,
spend approval, clean-machine access, signing identity, and the final decision to
publish remain King Flowers’ handoff responsibilities.
