# Production-readiness audit — 2026-07-23

Commit baseline: `f330177` (code baseline).
Hosted CI evidence: run `30053432134`, green on Windows, macOS, Linux, and
headless Linux, including lint, strict typing, full tests, 14 named mutants,
wheel build, and clean-wheel smoke.

This audit distinguishes implemented/local evidence from external release
evidence. A missing live credential or signed tag is a release blocker; mock
evidence is not promoted to live evidence.

## Credential handling — High deferred

Windows Credential Manager, macOS Keychain, Linux Secret Service, and the
attended encrypted-file fallback are selected fail-closed. Direct connectors
only receive opaque vault handles. A legacy Moonshot wrapper exposed a
credential during discovery. Credential rotation is unresolved and all legacy
credential-bearing wrappers remain prohibited. Boundary: no Kimi live smoke or
release until rotation and vault capture are independently confirmed.

Nonsecret `torq auth status` evidence on 2026-07-23 failed closed: Claude and
Codex authentication were detected but resolved model identity was
unattestable; DeepSeek, Kimi, and Z.ai were blocked; Grok was unavailable.

## Sandbox escape — resolved locally

Path traversal, symlink escape, protected `.env` reads, concurrent workspace
access, dirty-primary refusal, command/network allowlists, environment
filtering, and process-tree cancellation have automated coverage.

Sandbox re-test: `tests/test_phase4_safety.py` passed locally and in the hosted
Windows, macOS, Linux, and headless Linux matrix on 2026-07-23.

## Receipt-chain integrity — resolved locally

Sequence continuity, hash chaining, artifact hashes, schema/profile/policy
consistency, Ed25519 manifest seals, encrypted artifacts, and offline
verification are tested, including seeded tampering.

## Dual redaction — resolved locally

One versioned pattern registry enforces both pre-provider egress and
pre-persistence redaction. Blocking patterns fail closed with labels only.

## Approval boundary — resolved locally

Primary files remain untouched before explicit approval. Application is pinned
to the captured primary tree and exact audited content hash. Drift refuses with
a re-run/re-baseline instruction. No push or merge role exists.

## Extraction conformance — resolved locally

MMH normalization fixtures, retry/budget behavior, graph profiles, routing
policy v3.1.3, and redaction match their frozen reference projections. The
suite includes a deliberately divergent normalization mutant that is caught.

## Open findings

- High — credential rotation and new vault capture: deferred behind a hard live/release boundary.
- High — live smoke for all six providers: deferred until approved credentials and exact model grants exist.
- High — signed tag and clean-machine OS-keychain access from installed artifacts: unresolved external release gates. Cross-platform wheel build and isolated wheel smoke are green.
- Medium — remote receipt anchoring: explicitly deferred to a future release; local signing is tamper-resistant, not tamper-proof.

## Repository controls — resolved

The standalone public repository is `pilotwaffle/torq-cli-python`. `main` is
protected with strict required checks for all four CI jobs, admin enforcement,
linear history, conversation resolution, and force-push/deletion disabled.
