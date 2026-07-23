# T-06C Raw Console V5 Import

`torq config import-v5-console --config ABSOLUTE_PATH` accepts the raw TORQ
Console V5 YAML structure and emits the same fixed, registry-authoritative
`torq-v5-repo-compat` v1 projection as T-06A. This supplies the PRD's
without-manual-translation path while keeping normalized JSON and raw YAML as
distinct input contracts.

## Input and parsing

The importer reads only the explicit absolute path through the protected-path,
bounded native reader. It does not discover or probe `.torq/v5/config.yaml`.
Registry and authenticated oracle validation precede input access.

Input is strict UTF-8 without BOM and at most 65,536 bytes. A dedicated parser
permits the sequences required by raw Console fields while enforcing one
mapping document, at most 1,024 meaningful YAML events, maximum collection
depth 8, unique NFC-normalized string keys, standard scalar/container tags,
and no explicit/custom tags, anchors, aliases, or unquoted merge keys. The
canonical TORQ-CLI config parser remains unchanged and continues to reject
sequences.

The closed root contains `version`, `created`, `supersedes`, `agents`,
`state_machine`, `rejection_routing`, `cost_guardrails`, and
`success_criteria`. `version` must be integer 5. The agent map contains exactly
`g1d`, `g1r`, `builder`, `g2a`, `refine_bug`, and `refine_ui`. Governed
`model`, `cli`, `prompt`, and optional `endpoint` values normalize to the
authenticated compatibility-oracle representation and must match it exactly.
Operational fields such as `role`, `reads`, `writes`, and optional `notes` are
shape-checked but are not projected.

Secret-shaped keys are rejected recursively after NFC, case, and hyphen
normalization. Source content is never included in a finding or failure
envelope.

## Output and boundaries

Success emits command `config_import_v5_console`, source schema
`torq-console-v5-config-v5`, target config version 1, profile
`torq-v5-repo-compat` version `1.0.0`, and the canonical target bytes and
SHA-256. The result remains `runtime_effective=false` and
`runtime_state=offline_unattested`.

The command is stdout-only and rejects `--output` before input I/O. It does not
write files, persist state, migrate configuration, resolve or create credential
references, copy CLI wrappers or endpoints, call providers, execute agents,
or claim runtime effectiveness.

## Failure behavior

Parser and shape failures are invalid (exit 2); protected-path failures are
blocked (exit 3); unexpected failures use the fixed non-disclosing internal
error (exit 5). Processing stops at the earliest failed registry, oracle,
input, parse, mapping, or projection stage.
