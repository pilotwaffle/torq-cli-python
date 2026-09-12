# Roadmap

## Now — v0.2.0 hardening
- Fresh protected-`main` CI and clean-machine install evidence on Windows, Linux, and macOS.
- Publish to PyPI under the settled package name.
- User-facing release notes and a recorded demo of `torq demo --run` + `torq evidence verify`.

## Next
- Production trust hardening: non-exportable platform signing identity and an
  independently operated remote transparency anchor for receipts
  (`torq trust readiness` reports both gaps today).
- Linux governed containment: mature the experimental systemd/cgroup-v2 adapter
  from evidence-grade to production-grade.
- macOS governed containment: separately signed and notarized containment product.
- Hosted docs site generated from `docs/`.

## Later
- Formal V6 contract publication, MMH consensus, and remote receipt anchoring.
- Provider pricing tables and richer Fleet analytics.

Non-goals are stable: TORQ agents never commit, push, or merge; live execution
always requires operator and policy double opt-in; evidence stays verifiable offline.
