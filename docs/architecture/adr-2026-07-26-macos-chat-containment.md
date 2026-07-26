# ADR 2026-07-26: macOS governed-chat containment remains fail closed

## Decision

An ordinary pip- or pipx-installed TORQ Python wheel **must not launch governed
chat providers on macOS**. The shipped capability is explicitly unavailable and
returns `owned_process_strong_containment_unavailable` before `Popen`.

TORQ requires all four properties below before macOS chat can be enabled:

1. the provider executes no instruction before assignment to its owner;
2. `setsid()` and double-fork descendants cannot escape the owner's kill domain;
3. coordinator crash triggers termination of every owned descendant; and
4. `turn_cancelled` is written only after the OS reports the domain empty.

POSIX process groups satisfy none of the escape/crash guarantees. No public
Apple API documented for an ordinary Python CLI supplies a Job Object- or
cgroup-equivalent ownership domain with a confirmed-empty query. This is a
scope/assurance decision, not a claim that native macOS containment can never be
built.

## Primary-source basis

- Apple's [Endpoint Security overview](https://developer.apple.com/documentation/EndpointSecurity)
  documents monitoring/authorization of exec, fork, and signal events through
  a system extension. It does not document a descendant containment domain.
- The [Endpoint Security client entitlement](https://developer.apple.com/documentation/BundleResources/Entitlements/com.apple.developer.endpoint-security.client)
  must be requested from Apple; client creation fails when it is absent.
- Apple's [Endpoint Security client contract](https://developer.apple.com/documentation/endpointsecurity/client)
  also requires privilege and user TCC approval. These requirements cannot be
  carried by an unsigned ordinary Python wheel.
- Apple's [XPC overview](https://developer.apple.com/documentation/xpc) says
  `launchd` manages XPC-service lifetime and can tie a bundled service to its
  client. It does not state that arbitrary descendants spawned by that service
  are non-escapable or that their aggregate is queryable as empty.
- Apple's [Endpoint Security sample deployment](https://developer.apple.com/documentation/endpointsecurity/monitoring-system-events-with-endpoint-security)
  requires an Apple Developer ID, the requested entitlement, signed app and
  extension targets, installation from `/Applications`, user approval, and Full
  Disk Access.

The conclusion above is an inference from those published contracts: Endpoint
Security can observe or gate selected events, and XPC can own a service, but
neither documented contract alone proves TORQ's four ownership invariants.

## Future enablement boundary

A future implementation is a separately reviewed native product, not a Python
adapter hidden behind platform detection. At minimum it requires:

- a signed and notarized app containing an authenticated native XPC helper or
  Endpoint Security system extension;
- an Apple-granted Endpoint Security entitlement if that API is part of the
  enforcement design, plus explicit installation, TCC, and privilege handling;
- peer code-signing authentication on the Python-to-helper control channel;
- a no-pre-execution launch protocol and a durable owner lease so coordinator
  loss cannot orphan live provider work;
- an authoritative terminate-and-observe-empty operation, with timeout or
  helper loss recorded as `turn_cancellation_uncertain`; and
- versioned capability attestation bound into the signed turn evidence.

## Evidence gate for changing this decision

Do not flip `available` until a clean macOS runner tests the packaged, signed,
notarized artifact (not a mocked Python adapter) and records all of the
following:

- 100 sequential and 20 concurrent start/stream/stop cycles with zero survivors;
- adversarial `setsid()` and double-fork descendants cannot outlive Stop;
- coordinator crash before launch acknowledgement, during graceful stop, and
  during force-stop leaves zero descendants;
- helper crash, permission revocation, entitlement absence, protocol mismatch,
  and observation timeout fail closed or terminalize as cancellation uncertain;
- process enumeration is independent of parent PID/PGID reuse; and
- the evidence names the macOS build, hardware architecture, helper version,
  signing identity, entitlement state, and notarization result.

Until that machine evidence exists, macOS credentials may still be stored in
Keychain, but they cannot be used by governed interactive chat.
