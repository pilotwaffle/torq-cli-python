# Governed context injection contract

Status: **Release 3 backend implemented and merged before this v0.2.0 candidate.**

`GovernedContextInjector` binds one active `GovernedOrchestrator` to its
authenticated `ReceiptChain`. It is the only supported Fleet write boundary.
The Fleet projector remains read-only.

## Invariants

1. Context is size-bounded before any artifact or receipt is written.
2. The shared `PatternRegistry` blocks known high-risk secrets and redacts
   permitted patterns before persistence or provider dispatch.
3. Sanitized content is encrypted as a run artifact. The receipt records its
   hash and path, never the content.
4. `command_accepted` durably fixes the earliest eligible boundary. The command
   is reconstructed from covered receipts, not process memory.
5. A context applies only to an attempt created after acknowledgement and before
   dispatch. Otherwise terminal closure records `command_unapplied`.
6. Lead context creates a reproducible `run_replanned` hash revision naming the
   exact effective attempt. Confirmed direct-lane context never replans.
7. `context_injected` repeats the accepted extraction provenance and effective
   attempt; `stage_dispatch_started` names the applied command/context IDs.
8. Receipt append, rolling-manifest replacement, artifact writing, and terminal
   sealing are serialized through the chain lock.

## HTTP boundary

An active in-process runtime may pass a `GovernedContextInjector` to
`create_fleet_server`. This enables `POST /api/v1/fleet/context`; the legacy
`POST /api/v1/context` spelling remains a compatibility alias. Without that explicit
injector, every POST remains `405 read_only`, including the standalone
`torq fleet --serve` command.

The endpoint:

- accepts a closed `inline_text`/`file` union; files use strict standard Base64;
- requires exact single Host and Origin headers, JSON content type, one bounded
  Content-Length, and no Transfer-Encoding;
- atomically consumes and rotates the write session so concurrent replay cannot
  fork it, while preserving the original absolute expiry;
- independently rejects mutation when verified run state is terminal;
- returns only receipt metadata, never submitted content;
- reuses the governed application service rather than writing receipts itself.

## Attachment boundary

The approved v1 file contract accepts only strict UTF-8 `.txt`, `.md`/
`.markdown`, and strict `.json` with exact MIME/extension pairing. JSON rejects
duplicate keys, non-finite numbers, and excessive depth/nodes, then canonicalizes
before scanning. BOM, NUL, known binary signatures, PDF, Office/ZIP, images,
PNG, JPEG, and PDF inputs are bounded and signature checked, then preserved as
canonical base64 envelopes so the encrypted artifact retains the submitted
bytes without invoking an unsafe parser. HTML, YAML, XML, archives, and other
opaque binary content fail before artifact write
or provider dispatch.
