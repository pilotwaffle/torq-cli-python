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

Production chat uses a kernel-backed owner on Windows. The provider is created
suspended, assigned to a kill-on-close Job Object, and then resumed. Linux
production chat fails closed with `distinct_identity_system_broker_required`.
macOS remains unsupported. Process groups are never advertised as ownership.

The Linux per-user systemd adapter is an experimental behavior harness, not a
strong boundary: its manager, coordinator, and provider all execute as the same
OS identity. It places a trusted supervisor in a cgroup-v2 unit before provider
exec, uses `KillMode=control-group`, and observes `cgroup.events: populated`.
Provider secrets are framed over stdin and the stdin connection acts as a
coordinator lifetime lease.

The experimental service makes `/proc`, the operator's user-bus and private
user-manager sockets, and the system manager sockets inaccessible in its mount
namespace. It permits only `AF_INET` and `AF_INET6` socket creation, preserving
direct HTTPS while intentionally disabling local AF_UNIX integrations such as
provider-side keyring access and local proxies. TORQ must resolve credentials
before launch. These restrictions reduce known same-identity escape paths but
do not upgrade the adapter to production containment.

The `linux-systemd-experimental-evidence` CI job starts the runner account's
real user manager through PID 1, then requires a protected runtime directory,
user bus, unified cgroup-v2 hierarchy, and successful sandbox preflight. Missing
prerequisites, skips, and test-inventory changes refuse the job. It uses neither
a container nor a process-group substitute.

The job uploads `evidence.json`, `junit.xml`, and an evidence SHA-256 sidecar as
the `linux-systemd-experimental-evidence` artifact. It records checked-out event
and PR-head SHAs, workflow identity, hosted image, kernel, manager cgroup, test
summary, and JUnit SHA-256. Checksums are integrity aids, not signatures. The
GitHub run and artifact provide provenance; the files alone do not.

On a clean cgroup-v2 login host with an already-running user manager, run:

```bash
TORQ_TEST_LINUX_SYSTEMD_CGROUP=1 python -m pytest -q \
  tests/test_owned_process_linux_kernel.py tests/test_chat_end_to_end.py
```

That experimental gate exercises `setsid()` and double-fork adversaries, 100 sequential
stops, 20 concurrent stops, coordinator-crash lease cleanup, durable chat
completion, user-manager delegation refusal, and cancellation only after a
known-empty cgroup observation. A green result proves only those observed
user-systemd behaviors on that host. It does not enable Linux production chat.

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
keys, optimistic cancellation, editing already signed messages, or production
strong-containment claims on Linux or macOS. The Linux systemd/cgroup-v2 path
described above is experimental evidence only.
