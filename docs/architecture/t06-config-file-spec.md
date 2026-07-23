# T06B Config File Specification

This specification defines the bounded v1 YAML configuration accepted by TORQ CLI. It is an explicit `--config` contract; the symbolic locations below are documentation-only future defaults and do not authorize runtime discovery.

## Parser contract

File input is read through the existing protected-path checks and bounded native final-handle reader. `resolve_text` is an internal adapter, not a parser bypass. Both paths operate on strict UTF-8 bytes, reject a leading U+FEFF/BOM, enforce a maximum of 65,536 bytes inclusive, and reject invalid UTF-8 or an isolated surrogate. Parser rejection is opaque: one `config_syntax_invalid` finding at `/`, stage `config_parse`, empty context/data, and a snapshot retaining only registry identity/version/resource hash; config path, version, profile identity, and later stages are absent/null.

Before construction, the parser must:

1. Count all YAML parse events other than stream start/end; 1,024 is accepted and 1,025 is rejected.
2. Track mapping/sequence collection depth from zero; depth 8 is accepted and depth 9 is rejected.
3. Require exactly one document with a mapping root. Empty/comment-only streams, explicit empty documents, scalar/sequence roots, and any second document (including trailing empty `---`) are rejected. Trailing comments and one terminating `...` are allowed.
4. Reject sequences everywhere, non-map tags, explicit/custom tags, anchors, aliases, and unquoted merge keys.
5. Require scalar mapping keys with the resolved YAML string tag. Duplicate identity is NFC-normalized, case-sensitive equality within each mapping; this applies to nested mappings and quoted/plain spellings.
6. Permit scalar values only with implicit string, integer, boolean, or null tags.

Only after event and node preflight may the dedicated `SafeLoader` construction helper run. Parser policy failures never expose YAML details, paths, keys, values, tags, anchors, or secrets and never mutate input or invoke schema/profile/eligibility/oracle processing.

## Closed v1 schema

The root is closed and requires exactly these fields: `config_version`, `profile`, `binding_overrides`, `connectors`, and `policy`.

- `config_version` is an integer, never boolean. Missing emits `config_version_missing` plus `config_schema_invalid`; values below 1 require migration, values above 1 are unsupported, and only version 1 is supported.
- `profile` is closed and requires string `id` and string `version`.
- `binding_overrides` is a required mapping. An empty mapping is valid. Each role ID must exist in the packaged registry, and each override is a non-empty closed mapping containing optional `enabled` and/or `connector_id`.
- `connector_id` matches `^[a-z][a-z0-9_-]{0,31}$`; `enabled` is boolean. Protected override members (`provider_id`, `model_id`, `prompt_id`, `prompt_version`, `effort_id`, `policy_id`, `strategy_id`, `authority`, `independence`) produce `binding_override_forbidden` at eligibility; other unknown members are schema-invalid.
- `connectors` is a required mapping and may be empty. Connector IDs use the same regex. Each connector is closed and requires string `provider_id`, string `surface`, and boolean `enabled`; `credential_ref` is optional and must match `credref_[0-9a-f]{32}`. Providers are `anthropic`, `deepseek`, `openai`, `moonshot`, and `zai`. Surfaces are `agent_sdk`, `codex_sdk`, `acp`, and `direct_api`.
- Raw credential keys are rejected after NFC normalization, lowercase, and hyphen-to-underscore normalization. The denylist includes `api_key`, `apikey`, `secret`, `secret_key`, `access_token`, `auth_token`, `token`, `password`, `private_key`, `credential`, `credentials`, `client_secret`, `bearer_token`, and provider-specific API-key names.
- `policy` is closed and requires `independence_mode` (`profile_minimum` or `vendor_strict`), `unattestable_action` (`deny`), integer-not-bool `loop_budget` in 1–10, and closed `resource_limits`.
- `resource_limits` requires integer-not-bool `max_runtime_seconds` 1–86,400, `max_cost_cents` 0–100,000,000, `max_file_count` 1–100,000, and `max_changed_lines` 1–1,000,000. These values are declarative metadata only.

Validation retains immutable result mappings and does not mutate caller input. Profile identity and version are type-checked before registry membership/indexing.

## Resolution taxonomy and ordering

The resolver executes one stage at a time: `registry_read`, `registry_parse`, `registry_validate`, `config_read`, `config_parse`, `config_validate`, `profile_resolve`, `eligibility`, `oracle_validate`, then `complete`. A stage finding returns immediately; same-stage findings remain deterministic. Parser failures stop before construction and all later stages.

- Ordinary absent/unreadable config: exit 2, `config_unreadable`, `config_read`.
- Too-large, BOM, encoding, YAML syntax, event, depth, node, tag, anchor, alias, merge, or duplicate failure: exit 2, `config_syntax_invalid`, `config_parse`.
- Protected/reparse/nonregular/hardlink/identity mutation or missing safety primitive: exit 3, protected-path finding, `config_read`.
- Schema defects: exit 2. Protected overrides and declarative eligibility defects: exit 3. Unexpected defects: exit 5, `internal_error`.

## Documentation-only symbolic locations

No runtime resolver is added. If a future attended feature needs a default, Linux uses absolute non-empty `XDG_CONFIG_HOME/torq/config.yaml`; if XDG is absent/empty/non-absolute, it may use absolute non-empty `HOME/.config/torq/config.yaml` only when `HOME` is absolute. macOS uses absolute `HOME/Library/Application Support/TORQ/config.yaml` only when `HOME` is absolute. Windows uses `APPDATA\TORQ\config.yaml` only when `APPDATA` is an absolute non-empty current-user roaming application-data path. Invalid variables cause no probing, creation, expansion, or fallback beyond the Linux XDG-to-HOME rule.

T06B does not add discovery, provider access, credential lookup, persistence, migration, scheduling, billing, deployment, or operator actions. T06A remains limited to the authenticated normalized JSON projection. T06C adds a separate bounded raw Console YAML importer and does not weaken this canonical config parser or add runtime capability.
