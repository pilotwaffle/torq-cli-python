# TORQ CLI 0.1.0 release notes

Release tag: `v0.1.0` in the standalone repository, per the repository-home
decision. Do not create the tag until every release gate is green.

Security guarantees and limits are in `SECURITY.md`. The dated
production-readiness audit is
`docs/security/production-readiness-audit-2026-07-23.md`; installation is in
`docs/install.md`.

## Non-goals

- The tool never commits, pushes, or merges.
- The v0.2 UI is not included.
- Formal V6 contract publication, MMH consensus, remote receipt anchoring, and
  provider pricing tables are deferred.
- Recorded/mock provider conformance is not proof of live provider access.

## Required release evidence

- Signed tag and artifact hashes match the tagged build.
- Protected `main` requires the Windows, macOS, Linux, and headless CI checks.
- Clean-machine installation and credential-backend access pass on all three OSes.
- All six manual live provider smokes pass with resolved-model attestation.

