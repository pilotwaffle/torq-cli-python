# T-06C Raw Console V5 Import Design

## Outcome

Add a bounded, read-only command that imports the raw TORQ Console V5 YAML
configuration into TORQ CLI's canonical `torq-v5-repo-compat` v1 configuration
without manual translation.

The command is:

```text
torq config import-v5-console --config ABSOLUTE_PATH
```

It emits a result envelope containing the canonical target configuration on
stdout. It does not write files, discover a default path, resolve credentials,
call providers, claim runtime effectiveness, or mutate Console state.

## Input Contract

The input is the raw V5 YAML shape represented by
`E:\TORQ-CONSOLE\.torq\v5\config.yaml`: V5 metadata, six agent records, state
machine data, rejection routing, cost guardrails, and success criteria.

Input processing uses the existing protected-path and bounded native read
boundary. YAML parsing uses a dedicated sequence-aware variant of the T-06B
preflight policy: strict UTF-8, no BOM, 65,536-byte maximum, one mapping
document, bounded events and depth, no anchors, aliases, merge keys, custom
tags, duplicate keys, or secret-shaped keys. Sequences are permitted because
they are required by the raw Console shape; the canonical TORQ-CLI config
parser remains unchanged and continues to reject sequences.

The importer recognizes exactly the six governed roles: `g1d`, `g1r`,
`builder`, `g2a`, `refine_bug`, and `refine_ui`. Source lane values are checked
against the authenticated normalized compatibility oracle. Operational-only
fields such as reads, writes, notes, state-machine presentation, endpoints,
and CLI wrapper names are never copied into the target configuration.

## Projection

Successful import produces the same registry-authoritative canonical v1 bytes
as T-06A. This preserves one target contract and avoids two subtly different
repo-compat configurations.

The result records:

- source schema `torq-console-v5-config-v5`;
- target config version 1;
- target profile `torq-v5-repo-compat` version `1.0.0`;
- registry-authoritative lossy canonicalization;
- `runtime_effective=false` and `runtime_state=offline_unattested`;
- the canonical target UTF-8 bytes and SHA-256.

## Components

- A domain parser validates and normalizes raw Console YAML into the existing
  six-lane normalized representation.
- The application boundary authenticates the registry and oracle before
  reading input, then validates the normalized mapping and fixed projection.
- The CLI adds a distinct `import-v5-console` subcommand. The existing strict
  `import-v5-normalized` JSON contract remains unchanged.
- Documentation records T-06C completion without advancing provider,
  credential, persistence, migration, deployment, release, or broader Phase 1
  gates.

## Error Handling

Expected input failures return stable, non-disclosing findings and stop at the
earliest failed stage. Syntax and structural defects are invalid; protected
paths are blocked; unexpected failures return the fixed internal-error
envelope. No failure output includes source values, paths embedded in the YAML,
CLI wrapper names, endpoints, notes, or possible secrets.

## Testing

Development follows red-green-refactor. Tests cover:

- the captured raw Console fixture shape importing without manual translation;
- exact deterministic target bytes and digest;
- role order independence;
- malformed YAML, duplicate keys, size, depth, document, tag, alias, and merge
  failures;
- secret-shaped key rejection without value disclosure;
- missing, duplicate, unknown, or mismatched agent lanes;
- protected and unreadable input paths;
- registry/oracle precedence and projection validation;
- unchanged behavior of `import-v5-normalized` and all Foundation commands.

Completion requires focused tests, the full pytest suite, Ruff, mypy, named
mutants, package build, wheel smoke, diff review, and documentation review.
