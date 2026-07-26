# ADR 2026-07-26: Linux governed chat requires a distinct-identity system broker

## Decision

Linux production governed chat remains unavailable. `OwnedProcess` and the
Fleet chat CLI fail closed before provider execution with
`distinct_identity_system_broker_required`. The per-user systemd adapter is an
experimental host-kernel harness only; no environment variable or config flag
can promote it to production.

The controlling reason is identity, not cgroup mechanics. A coordinator,
provider, and user manager running as one UID share enough authority to copy a
lease descriptor, reconnect to known manager sockets, or delegate work to
another same-user process. Hiding individual paths improves the experiment but
does not create a security principal.

## Required broker boundary

A future Linux implementation must provide all of these properties:

1. A narrowly scoped system service runs as an identity distinct from both the
   desktop operator and provider. The provider cannot signal, inspect `/proc`
   state for, duplicate descriptors from, or authenticate as the broker.
2. The operator-to-broker IPC authenticates peer credentials, binds each request
   to a unique run and turn, enforces replay protection, bounds every field, and
   never accepts a caller-supplied cgroup or executable control path.
3. The broker creates the cgroup and all sandbox restrictions before the first
   provider instruction executes. There is no post-fork migration window.
4. Provider credentials and prompt bytes cross a protected one-shot channel;
   secrets do not appear in argv, unit properties, journals, or inherited
   unrelated environment.
5. Broker death, coordinator death, IPC loss, and forced cancellation all
   independently trigger whole-cgroup termination. `cancelled` is durable only
   after the broker reports a kernel-observed empty cgroup.
6. Workspace access is an explicit broker policy: canonical roots, symlink and
   traversal refusal, read/write separation, and no implicit access to the
   operator home, runtime directory, keyrings, or control sockets.
7. Direct HTTPS remains available under an explicit network policy. Local Unix
   sockets and proxies are denied unless individually mediated and tested.

## Acceptance evidence

Release qualification requires clean Linux hosts and zero skips. Tests must
prove different UIDs for broker and provider; authenticated and replay-resistant
IPC; no pre-containment execution; `setsid`, double-fork, cgroup migration,
direct user-bus, `.host` manager delegation, and lease-descriptor theft refusal;
100 sequential and 20 concurrent stop cycles; coordinator and broker crash
cleanup; kernel-observed empty cgroups; workspace policy enforcement; direct
HTTPS under the declared network policy; and zero surviving processes.

Until those tests pass against the production broker, Linux remains fail closed
regardless of how the experimental user-systemd job behaves.
