# TORQ Fleet UI

TORQ Fleet is a wheel-bundled browser application served by the loopback-only
Python Fleet server. It renders the `torq-fleet-snapshot-v3` envelope and never
reads provider output or evidence files directly.

Launch it with the CLI's Fleet serve command. The single-use `/bootstrap`
exchange establishes an HttpOnly, SameSite session and redirects to `/` without
leaving the nonce in browser history.

## Route contract

- `GET /` and `GET /assets/{fleet,chat}.{css,js}` serve static assets containing no run data.
- `GET /api/v1/fleet` returns one complete verified v3 envelope.
- `GET /api/v1/fleet/events` streams complete envelopes with polling fallback.
- `POST /api/v1/fleet/context` records attended text or a supported attachment.
- `POST /api/v1/fleet/actions/{id}/resolve` resolves an eligible action.
- `POST /api/v1/fleet/recover/confirm` and `/recover` implement two-step recovery.
- `GET /api/v1/chat` and `/api/v1/chat/events` expose verified transcript state
  plus explicitly provisional output deltas.
- `POST /api/v1/chat/turns` and `/api/v1/chat/turns/{id}/cancel` control the one
  OS-owned provider process when chat was explicitly enabled at launch.

All evidence reads require the Fleet session. Mutations additionally require a
write-capable session and exact same origin. The legacy `/api/v1/context` alias
is retained for compatible clients; new UI code uses `/api/v1/fleet/context`.

## Governed command rail

The fixed bottom rail combines the six-lane monitor with attended input. Inline
text and up to four bounded `.txt`, `.md`, `.json`, `.png`, `.jpg`/`.jpeg`, or
`.pdf` attachments are submitted through the governed context route. Binary
types are signature checked, encrypted as canonical artifact envelopes, and
receipt-bound. The legacy context form remains hidden for compatible clients;
the visible bottom composer dispatches through the governed chat runtime only
when the operator explicitly enables a provider and model.

The authoritative backend contract is
`docs/architecture/fleet-backend-contract.md`; artifact extraction boundaries
are in `docs/architecture/context-injection-contract.md`.
The process and chat-evidence boundary is in
`docs/architecture/governed-chat-runtime.md`.
