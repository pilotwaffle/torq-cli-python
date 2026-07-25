# TORQ UI boundary

This directory reserves the future desktop/web UI boundary for v0.2.

The UI is not part of the v0.1 build. Version 0.1 remains a headless-first
Python CLI. UI code consumes `torq-fleet-snapshot-v1` through
`GET /api/v1/fleet` or the public `FleetProjector` application service; it must
not import CLI command handlers or read provider stdout.

See `docs/architecture/fleet-backend-contract.md`.

The optional input dock uses the attended `GovernedContextInjector` contract in
`docs/architecture/context-injection-contract.md`. The standalone Fleet server
does not enable mutation endpoints.
