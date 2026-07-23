# PRD r5 implementation status — 2026-07-23

Code baseline: `f330177`. Hosted quality run: `30053432134`.

“Implemented” means the production boundary and deterministic tests exist and
pass. It does not silently promote recorded/mock connector evidence into live
provider evidence.

| Task | Status | Evidence boundary |
| --- | --- | --- |
| T-02 | Complete | Extraction audit and verdicts recorded. |
| T-03 | Implemented; live verification pending | Matrix and decisions exist; current auth/model attestation fails closed. |
| T-05 | Complete | Python standalone repository, wheel/pipx distribution, one version source. |
| T-07 | Complete | Hermetic Windows/macOS/Linux/headless CI and protected `main`. |
| T-08–T-12 | Complete | Extracted core, graph, routing, redaction, and conformance tests. |
| T-13–T-20 | Complete | Six connector boundaries, auth status, and credential-free conformance. |
| T-21 | Runner complete; live evidence pending | Manual live smoke is blocked by credentials/model grants. |
| T-22–T-27 | Complete | Workspace isolation, policy, governed run, receipts, approval, and usage. |
| T-28–T-31 | Complete | Setup, run/cancel/resume, effective status, and offline evidence verification. |
| T-32 | Complete | Production-readiness audit records resolved and open findings. |
| T-33 | Recorded composition complete; live execution pending | Fault-injected heterogeneous governed E2E and receipt verification pass; providers were recorded/mock. |
| T-34 | Complete | `SECURITY.md` matches implemented controls and audit findings. |
| T-35 | Implementation and CI packaging complete; keychain install evidence pending | Wheels build and smoke on all three OSes; clean-machine OS-keychain access still needs operator environments. |
| T-36 | Correctly withheld | A signed release tag would violate the PRD while T-21, live T-33, and the remaining T-35 evidence are open. |

## Verification

- Local full suite: pass with four intentional live/environment skips.
- Strict mypy: pass across 42 source files.
- Ruff: pass.
- Named security/governance mutants: 14/14 killed.
- Hosted run `30053432134`: all four jobs green, including wheel build and
  isolated wheel smoke.
- Branch protection: strict four-job checks, admin enforcement, linear history,
  conversation resolution, force-push disabled, deletion disabled.

## Required operator evidence before T-36

1. Supply approved credentials and exact model grants for all six providers;
   run T-21 live smoke and retain redacted evidence.
2. Run T-33 against at least two live heterogeneous providers.
3. Verify installed-artifact access to Credential Manager, Keychain, and Secret
   Service/encrypted fallback on clean Windows, macOS, and Linux machines.
4. Re-run T-32, then create the signed standalone tag `v0.1.0` and matching
   release artifacts only if no High finding remains.
