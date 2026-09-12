# TORQ CLI 0.2.0

TORQ 0.2.0 is a governed agent runner: every run is attested, dry-run by default,
and backed by a verifiable receipt chain.

## What is new since 0.1.0

- **Governed orchestration boundary.** `torq run` executes the full
  G1D → G1R → Builder → G2A pipeline. HIGH defects route through a bound
  repair lane, and a targeted G2A re-audit runs before a run returns
  `awaiting_approval`. Invalid config, credentials, regional routing, or
  missing transport binaries fail before a run directory is created.
- **Evidence-backed Fleet chat.** `torq fleet --serve` exposes an attended
  chat surface over verified run evidence. Provider processes run under
  OS-enforced containment — production-enabled on Windows; Linux and macOS
  fail closed.
- **Native credential storage.** `torq auth store` keeps provider secrets in
  the Windows Credential Manager, macOS Keychain, or Linux Secret Service as
  opaque `credref_` handles; an attended, passphrase-protected encrypted-file
  vault is available for headless machines. `auth verify-access` confirms
  access without printing or transmitting the value.
- **One-command trial.** `torq demo --run` scaffolds a valid identity and
  attestation, executes a real dry-run, and leaves a run directory that
  `torq evidence verify` confirms — no provider calls, no hand-written JSON.
- **Platform honesty.** Commands report exactly what is production-grade and
  what is not; `torq trust readiness` names the signing and anchoring gaps
  still open.

## Install

    pip install torq-cli        # Python 3.11–3.13

## Try it in one command

    torq demo --goal "Add input validation to the login form" --run
    torq evidence verify --run-root ./torq-demo-runs/<run-id>

## Verification

Release evidence — signed tag, artifact hashes, multi-OS CI, clean-machine
installs, and live provider smokes — is attached to the release. Security
guarantees and limits: SECURITY.md. Internal gate detail:
docs/releases/torq-cli-v0.2.0.md.
