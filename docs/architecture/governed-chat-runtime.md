# Governed chat runtime contract

TORQ chat is an attended control loop layered onto an existing governed run. It
accepts one operator turn at a time, launches exactly one provider child inside
kernel-backed containment, streams provisional output, and writes a signed
terminal transcript only after the operating system reports the owned process
tree's disposition.

## Launch

Chat is opt-in. The existing Fleet command remains read-only unless an explicit
provider and model are supplied:

```powershell
torq fleet --run-root E:\evidence\run-id --serve `
  --chat-provider deepseek --chat-model deepseek-v4-pro `
  --credential-file E:\TORQ-CONSOLE\.env
```

`deepseek` deliberately resolves the Alibaba/Qwen Token Plan credential and
regional Anthropic-compatible endpoint. It never falls back to a direct
DeepSeek metered key. `claude` uses the installed Claude Code subscription and
does not accept file attachments; Anthropic-compatible plan providers accept
bounded text, image, and PDF content through the owned streaming bridge.

## Invariants

- The browser owns neither provider credentials nor process handles.
- One `ChatRuntimeCoordinator` owns at most one active turn.
- Prompt content travels on the child's stdin, never in argv.
- Ambient secrets are removed before the child environment is built.
- `turn_cancelled` is durable only after containment reports `known_empty`.
- An uncertain OS observation becomes `turn_cancellation_uncertain`, never a
  successful cancellation claim.
- Provider deltas are provisional SSE events. The transcript projector renders
  durable user/assistant messages only from verified chat evidence.
- Signed chat rows form a hash chain bound to the run ID and certified
  `operator_gateway` identity. A signed head outside the run directory detects
  journal-only rollback. The head and journal are owner-ACL protected and the
  writer is interprocess serialized.
- Attachment bodies are never persisted in chat receipts; only bounded name,
  MIME type, size, and SHA-256 metadata are signed.
- Restart recovery marks any submitted but nonterminal turn uncertain. It does
  not infer that a pre-crash worker died.

## Routes

- `GET /api/v1/chat` returns the verified transcript projection.
- `GET /api/v1/chat/events` streams provisional output and refreshed verified
  snapshots, with browser polling fallback.
- `POST /api/v1/chat/turns` accepts one bounded turn.
- `POST /api/v1/chat/turns/{turn_id}/cancel` requests owned-tree termination.

All routes require the HttpOnly Fleet session. Mutations additionally require
an exact loopback Origin and rotate the session token after every attempt.

## Platform boundary

Production chat currently enables only on Windows, where the provider is
created suspended, assigned to a kill-on-close Job Object, and then resumed.
Linux and macOS fail closed because POSIX process groups alone cannot prevent a
child from escaping with `setsid()`. They remain unsupported until a tested
kernel-backed owner can provide the same no-pre-assignment execution and
confirmed-empty observations.

## Residual threat

The signed external head prevents an attacker limited to a run directory from
rolling back chat evidence. It does not claim to defeat a same-account attacker
who can snapshot and later restore both the journal and its owner-protected head
as one consistent pair. A monotonic hardware/OS service would be required to
make that stronger claim; no filesystem counter is presented as that primitive.
Transcript bodies are plaintext under owner-only ACLs. This protects against
other local accounts, not a process already executing as the operator.

## Out of scope

This release does not provide multiple simultaneous turns, browser-held API
keys, optimistic cancellation, editing already signed messages, or a claim of
strong containment on POSIX platforms.
