# TORQ CLI Foundation Slice — Approved Design

Date: 2026-07-17  
Status: Gate 1 approved for bounded implementation  
Terra G1D thread: `019f730e-3f6f-75f1-a5d7-b4615c466930`  
Sol G1R thread: `019f7345-ded0-7971-af5d-1b733b862af6` (`APPROVE`)

## Outcome

Build a standalone Python 3.11+ CLI that validates immutable TORQ governance profiles and non-secret configuration offline. The Foundation Slice exposes:

```text
torq profile validate --config PATH
torq status --offline --config PATH [--require-effective]
```

## Controlling invariant

Offline reads, parsing, and validation may establish evidence but never authorize protected capabilities. Every protected capability remains denied until its capability-specific prerequisites pass against one immutable invocation snapshot, and every failed prerequisite emits a stable finding before execution.

## Boundaries

Foundation permits only explicit config reads and packaged-resource reads. It contains no provider or agent invocation, credential resolution, environment discovery, network, subprocess, Git or `.git` access, upstream worktree access, persistence, telemetry, sandbox execution, receipt handling, apply path, or repository mutation.

The packaged registry is authoritative for CLI profiles. The pinned TORQ Console baseline is represented only by exact sanitized package fixtures for commit `3ae196102a84aed24f7daa9dc3fed037522e1f20`.

## Contract summary

- Closed registry: one policy, twelve prompt identities, three efforts, six roles, thirteen states, twenty-three transitions, four severity buckets, two strategies, two profiles, one promotion procedure, and one drift oracle.
- Each profile has six bindings, four independence rules, and three escalation declarations.
- Closed configuration rejects unknown keys, raw-secret fields, malformed opaque credential references, protected routing overrides, connector provider/surface mismatch, and disabled required roles.
- Resolution is pure and ordered: registry read/parse/validate; config read/parse/validate; profile resolution; eligibility; oracle validation; offline status.
- Results use stable findings, immutable partial snapshots, deterministic ordering, status precedence, and exit codes 0/2/3/4/5.
- Oracle integrity uses exact package bytes: three canonical JSON fixtures plus an exact 423-byte LF-only YAML manifest.

The complete normative schemas, byte preimages, hashes, findings catalog, transitions, bindings, prompt identities, mutation definitions, and test obligations are the latest `task_complete` packet in the Terra thread named above. If this summary and that packet differ, implementation stops and returns to Gate 1.

## Honest PRD status

- T-01: partial until implementation and evidence complete.
- T-02: audit draft/inventory only.
- T-04: may complete only if its three-OS requirements document meets the PRD.
- T-05: provisional pending completed T-02.
- T-06: implemented and locally verified; normalized JSON import, raw Console
  YAML import, and the closed v1 config contract remain read-only and offline.
  No new independent T-06C gate approval is claimed.
- T-07: scaffold and CI definition only.
- T-08 through T-36: excluded and not started.

No Phase 1 gate, runtime effectiveness, provider capability, branch protection, signing, tagging, release, or deployment is claimed.
