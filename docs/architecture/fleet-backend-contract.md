# Fleet backend contract

Status: **Rev 5.5A backend and Fleet control surface implemented; see
[`../prd-fleet-ui-rev-5-5a.md`](../prd-fleet-ui-rev-5-5a.md).**

The Fleet backend is a local projection of authenticated run evidence. Its
server-side reducer is the only evidence reducer. It does not tail provider
output, expose receipt keys, or let the browser reconstruct lifecycle state.
The standalone service is read-only unless run-bound context, action, and/or
recovery controllers are explicitly supplied. Eligibility reports unavailable
services rather than presenting a nonfunctional control.

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

The HTTP read contracts are `GET /api/v1/fleet` and bounded SSE
`GET /api/v1/fleet/events`. `GET /healthz` returns only the fixed liveness shape
`{status: ok}`. Mutation methods return `405 read_only` unless an active
in-process runtime supplies the corresponding controller. Controller opt-in
enables same-origin context injection, action resolution, and two-step recovery.
The standalone CLI server remains read-only. The server rejects non-loopback
bind addresses.
Only the run owner's `ActiveRunRuntime` may provide that opt-in. It owns one
broker and separate fixed-role orchestrator/operator-gateway facades; the
gateway cannot decrypt artifacts, inspect covered receipts, or seal the run.
Every data request requires the HttpOnly Fleet session established by the
single-use `/bootstrap` nonce exchange. Every request must also present exactly
one `Host` header matching
`127.0.0.1:<bound-port>`, `localhost:<bound-port>`, or the IPv6 loopback
equivalent; other host forms fail with `421 fleet_host_denied` to block
DNS-rebinding access.

Mutation accepts either inline UTF-8 text or strict Base64 bytes for the pinned
file extraction contract. The write session is consumed atomically and rotated
after both accepted and governed-rejected commands. Each mutation re-verifies
the run; terminal, tampered, incomplete, or cross-run state is non-mutable.

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

## Snapshot and control envelope

`torq-fleet-envelope-v3` carries `snapshot`, `annotations`, `session`,
`eligibility`, and `pending`. Its `torq-fleet-snapshot-v3` contains:

- verification status and finding;
- run identity, mode, decision, seal state, and waiting-on list;
- one count per ordered lane state, open-action count, and reduction errors;
- catalog-ordered lane rows with distinct `blocked` and `needs_you` states;
- raw blocked reason, plain-language gloss, and `provider_dispatch` assertion;
- split token usage, settlement, quota provenance, and priced/unpriced values;
- settlement totals reconstructed from completed-stage receipts.

The projector never reads encrypted artifact bodies. Operational orphan and
recovery annotations remain explicitly non-evidentiary. The browser consumes
only server-reduced state and uses complete-envelope SSE updates with polling
fallback; it never merges partial events.
