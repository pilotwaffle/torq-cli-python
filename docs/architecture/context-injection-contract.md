# Governed context injection contract

Status: **implemented on `feat/context-injection`; merge pending.**

`GovernedContextInjector` binds one active `GovernedOrchestrator` to its
authenticated `ReceiptChain`. It is the only supported Fleet write boundary.
The Fleet projector remains read-only.

## Invariants

1. Context is size-bounded before any artifact or receipt is written.
2. The shared `PatternRegistry` blocks known high-risk secrets and redacts
   permitted patterns before persistence or provider dispatch.
3. Sanitized content is encrypted as a run artifact. The receipt records its
   hash and path, never the content.
4. `context_injected` records the target, route, media type, source label,
   redaction findings, and `provider_dispatch: false`.
5. Lead-routed context is consumed by the next governed stage. Explicit
   lane-routed context waits for that lane and is consumed once.
6. `stage_started.context_ids` proves which injected inputs reached a dispatch.
7. Receipt append, rolling-manifest replacement, artifact writing, and terminal
   sealing are serialized through the chain lock.

## HTTP boundary

An active in-process runtime may pass a `GovernedContextInjector` to
`create_fleet_server`. This enables `POST /api/v1/context`. Without that explicit
injector, every POST remains `405 read_only`, including the standalone
`torq fleet --serve` command.

The endpoint:

- accepts a closed JSON shape (`content`, optional `target_role`, `media_type`,
  and `source_name`);
- requires an exact loopback same-origin `Origin` header;
- rejects payloads over 1 MiB;
- returns only receipt metadata, never submitted content;
- reuses the governed application service rather than writing receipts itself.

## Attachment boundary

This version accepts UTF-8 text and textual JSON/XML/YAML documents. Opaque
images and binary documents fail closed with `context_media_type_invalid`.
They require a separate extraction/OCR contract so TORQ can truthfully apply
the redaction registry to what will be dispatched. The UI must keep binary
attachment controls disabled until that contract lands.
