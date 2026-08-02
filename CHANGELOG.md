# Changelog

All notable changes to TORQ CLI are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Per-version release detail and release-gate evidence live in `docs/releases/`;
this file is the canonical cumulative record.

## [Unreleased]

### Added
- Apache-2.0 license (`LICENSE`) and corresponding project metadata.
- `CHANGELOG.md`, `CONTRIBUTING.md`, and `docs/security/threat-model.md`.
- Dependency upper bounds for `cryptography` (`<48`) and `PyYAML` (`<7`) and a
  Python upper bound (`<3.14`) to prevent silent breakage on future majors.
- Project URLs (homepage, repository, documentation, issues, changelog) and
  Trove classifiers in packaging metadata.

### Changed
- `requires-python` is now bounded: `>=3.11,<3.14`.

### Security
- `.torq-run-identities/`, `*.key`, `*.pem`, and `.tmp-tests/` are now
  gitignored, preventing run-local Ed25519 signing seeds and scratch test
  output from being committed.
- `MEMORY.md` (operator-local notes containing filesystem paths) is no longer
  tracked in the repository.

### Changed
- Import blocks sorted tree-wide (117 files) via `ruff --select I001 --fix`;
  safe pyupgrade fixes (UP012/UP017/UP035/UP041) and unused-import removal
  (F401) applied. No source logic changed; the CI ruff gate (`E4,E7,E9,F`)
  remains green.

### Known Limitations (tracked Wave 2 debt)
- A broader ruff pass (S/B/UP) surfaced ~62 real findings kept visible rather
  than suppressed. Highest-signal: **`assert` in `src/` (S101)** in
  `application/chat_runtime.py`, `application/fleet.py`, and
  `application/import_v5_config.py` — these invariants vanish under
  `python -O`, a control-bypass risk in a governed/evidence tool. Plus
  `raise ... from` (B904), loop-variable binding (B023 in
  `tests/test_hermetic.py`), and `xml.etree` on untrusted input (S314 in
  `scripts/run_linux_ownership_evidence.py`). See `docs/security/threat-model.md`
  for the full residual-risk list.

## [0.2.0] — 2026-07-26

### Added
- Installed, fail-closed `torq run --live` dispatcher factory with exact
  provider/model binding and persistent entitlement accounting.
- Attended XChaCha20-Poly1305 / Argon2id headless credential vault with
  explicit backend/root selection, bounded locking, rotation, revocation, and
  restrictive filesystem protections. No unattended or plaintext fallback.
- Evidence-backed Fleet control and governed interactive chat: streaming,
  attachments, Stop/cancellation evidence, and usage accounting.
- Windows production process-tree ownership via Job Objects; process-backed
  Claude-compatible live stages route through the same owned-process boundary.
- Experimental Linux user-systemd / cgroup-v2 evidence harness.
- Machine-readable production-trust readiness contract (`torq trust readiness`).
- Receipt schema 2.0.0: root-certified per-run key hierarchy with per-writer
  Ed25519 signatures, manifest sealing, and a root-signed external head for
  rollback detection.

### Security
- Explicit external env files must already be owner-only (POSIX `0600` or a
  Windows owner-only DACL); permissive files are refused, not mutated.
- Token Plan base URLs restricted to canonical
  `https://token-plan.<region>.maas.aliyuncs.com/apps/anthropic` endpoints with
  no userinfo, query, or fragment.

### Known Limitations
- No claim of a non-exportable platform signing identity or an independently
  operated remote transparency anchor. `torq trust readiness` reports the exact
  blocking findings; the bundled local signer and same-volume anchor
  intentionally report `not ready`.
- Production governed chat is unavailable on Linux and macOS in this release.
- macOS native containment requires a signed/notarized native product; the
  ordinary Python wheel fails closed.

## [0.1.0] — 2026-07-23

### Added
- Initial standalone release: governed multi-provider agent runner with role
  profile validation, fail-closed provider adapters, isolated execution
  sandboxes, evidence recording, and the Fleet control surface.
- Security model and credential backend boundaries documented in `SECURITY.md`.
- Dated production-readiness audit: `docs/security/production-readiness-audit-2026-07-23.md`.

### Non-Goals (carried from the release)
- The tool never commits, pushes, or merges.
- Formal V6 contract publication, MMH consensus, remote receipt anchoring, and
  provider pricing tables deferred beyond 0.1.0.
- Recorded/mock provider conformance is not proof of live provider access.

[Unreleased]: https://github.com/pilotwaffle/torq-cli-python/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/pilotwaffle/torq-cli-python/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/pilotwaffle/torq-cli-python/releases/tag/v0.1.0
