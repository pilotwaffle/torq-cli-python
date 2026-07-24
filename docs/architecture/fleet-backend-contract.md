# Fleet backend contract

Status: **implemented on `feat/fleet-backend`; merge pending.**

The Fleet backend is a local, read-only projection of authenticated run
evidence. It does not tail provider output, import CLI command handlers, expose
the receipt signing key, or mutate the chain.

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

The HTTP contract is `GET /api/v1/fleet`. `GET /healthz` reports the current
verification result. Mutation methods return `405 read_only`. The server rejects
non-loopback bind addresses.

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

`torq-fleet-snapshot-v1` contains:

- verification status and finding;
- run identity, mode, decision, seal state, and waiting-on list;
- reconciled sealed/running/needs-you/queued counts;
- six lane rows ordered by first receipt sequence;
- raw blocked reason, plain-language gloss, and `provider_dispatch` assertion;
- split token usage, settlement, quota provenance, and priced/unpriced values;
- settlement totals reconstructed from completed-stage receipts.

The projector never reads encrypted artifact bodies. Lane attachment and
artifact decryption require a separate attended application boundary.
