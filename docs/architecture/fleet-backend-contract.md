# Fleet backend contract

Status: **Release 1 implemented on protected main; contract reconciled 2026-07-25.**

The Fleet backend is a local projection of authenticated run evidence. Its
server-side reducer is the only evidence reducer. It does not tail provider
output, expose receipt keys, or let the browser reconstruct lifecycle state.
The standalone service is read-only except when an in-process governed context
injector is explicitly supplied.

## Commands

```powershell
# One verified JSON snapshot
torq fleet --run-root E:\path\to\evidence\run-id

# Loopback-only service for the bundled UI
torq fleet --run-root E:\path\to\evidence\run-id --serve --port 8765
```

Exported bundles use the existing out-of-band trust-anchor argument:

```powershell
torq fleet --run-root .\export\run `
  --trusted-public-key .\export\.torq-receipt-signing-key.pub
```

The HTTP read contract is `GET /api/v1/fleet`. `GET /healthz` returns only the
fixed liveness shape `{status: ok}`. Mutation methods return `405 read_only`
unless an active
in-process orchestrator explicitly supplies a `GovernedContextInjector`; that
opt-in enables the same-origin `POST /api/v1/context` contract. The standalone
CLI server remains read-only. The server rejects non-loopback bind addresses.
Every data request requires the HttpOnly Fleet session established by the
single-use `/bootstrap` nonce exchange. Every request must also present exactly
one `Host` header matching
`127.0.0.1:<bound-port>`, `localhost:<bound-port>`, or the IPv6 loopback
equivalent; other host forms fail with `421 fleet_host_denied` to block
DNS-rebinding access.

## Evidence behavior

Every appended receipt updates an atomically replaced, signed rolling manifest.
Its `sealed` field is `false` while the run is active and becomes `true` when
`ReceiptChain.seal()` completes. This closes the former gap where a live run
could not verify until its terminal decision.

The projector verifies the store on every snapshot. If verification is
`tampered` or `incomplete`, it returns `data_status: unavailable` and does not
project lanes, settlement totals, or a plausible run object.

New receipts include `observed_at`, allowing the UI to display receipt-backed
start/update time. Historical receipts without timestamps explicitly return
`receipt_timestamps_unavailable`; the projector does not invent elapsed time.

## Snapshot schema

`torq-fleet-snapshot-v2` contains:

- verification status and finding;
- run identity, mode, decision, seal state, and waiting-on list;
- one count per ordered lane state, open-action count, and reduction errors;
- catalog-ordered lane rows with distinct `blocked` and `needs_you` states;
- raw blocked reason, plain-language gloss, and `provider_dispatch` assertion;
- split token usage, settlement, quota provenance, and priced/unpriced values;
- settlement totals reconstructed from completed-stage receipts.

The projector never reads encrypted artifact bodies. Operational orphan and
recovery annotations remain explicitly non-evidentiary. Artifact decryption
requires a separate attended application boundary.
