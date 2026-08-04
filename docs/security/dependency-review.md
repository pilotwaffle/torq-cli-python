# Dependency review policy and Dependabot outcomes (release trust pack R0E)

Status: report-only governance record. Dependabot remains enabled; this
document records recent outcomes and defines the future blocking policy for
the dependency vulnerability scan.

## Current state

- Dependabot is configured for pip and GitHub Actions (`.github/dependabot.yml`),
  with commit messages pinned to full action SHAs per the repo's SHA-pinning
  discipline.
- CI gains a **non-blocking** dependency vulnerability scan (`pip-audit`) in
  the `report-only-metrics-linux` job during R0. It reports; it does not gate.

## Recent Dependabot PR outcomes (as of 2026-08-03)

| PR | Title | Outcome |
|---|---|---|
| #49 | bump ruff 0.15.12 → 0.16.1 | **merged** (reviewed) |
| #51 | bump pytest 9.0.2 → 9.1.1 | closed unmerged — applied through the reviewed manual bump #53 instead |
| #52 | bump mypy 2.0.0 → 2.3.0 | closed unmerged — held for review |
| #50 | update cryptography requirement | closed unmerged — held for review |
| #48 | bump actions/setup-python 6 → 7 | closed unmerged — major action bumps are re-pinned manually to reviewed full SHAs |
| #47 | bump actions/download-artifact 6 → 8 | closed unmerged — same SHA-pinning policy |
| #46 | bump actions/checkout 6 → 7 | closed unmerged — same SHA-pinning policy |
| #45 | bump actions/upload-artifact 6 → 7 | closed unmerged — same SHA-pinning policy |

Pattern: dev-dependency bumps are merged after review; floating-tag action
bumps are closed because this repository pins reviewed immutable SHAs and
re-pins manually after reviewing the upstream change.

## Later blocking policy (post-R0, not active yet)

When the dependency scan graduates from report-only to blocking, gates MUST be
defined by all four axes, in writing, before enforcement:

1. **Severity** — block on which severities (proposal: critical always;
   high unless exempted).
2. **Exploitability** — reachable in the shipped dependency graph vs
   transitive/unreachable findings.
3. **Fix availability** — a reviewed upgrade path exists; if none, the finding
   is tracked as an accepted risk with an owner and an expiry, not silently
   waived.
4. **Approved exceptions** — recorded in a reviewable exceptions file with
   justification, expiry date, and the reviewer.

No axis may be waived informally. Exceptions expire and must be re-approved.
