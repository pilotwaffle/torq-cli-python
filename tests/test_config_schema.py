import pytest
import yaml

from torq_cli.domain.config_schema import (
    ConfigSyntaxError,
    parse_config_bytes,
    parse_config_text,
    validate_config,
    validate_config_shape,
)
from torq_cli.domain.registry_schema import load_registry
from torq_cli.application.resolve import resolve_text


def valid_config() -> dict:
    return {
        "config_version": 1,
        "profile": {"id": "torq-v5-6-live", "version": "1.0.0"},
        "binding_overrides": {
            "builder": {"connector_id": "builder-main", "enabled": True},
        },
        "connectors": {
            "builder-main": {
                "provider_id": "deepseek",
                "surface": "direct_api",
                "enabled": True,
                "credential_ref": "credref_0123456789abcdef0123456789abcdef",
            },
        },
        "policy": {
            "independence_mode": "profile_minimum",
            "unattestable_action": "deny",
            "loop_budget": 1,
            "resource_limits": {
                "max_runtime_seconds": 60,
                "max_cost_cents": 100,
                "max_file_count": 10,
                "max_changed_lines": 100,
            },
        },
    }


def test_valid_closed_config_has_no_findings() -> None:
    findings = validate_config(valid_config(), load_registry())
    assert findings == ()


def test_optional_external_credential_source_is_closed_and_absolute() -> None:
    config = valid_config()
    config["credential_source"] = {"kind": "external_env", "path": r"C:\secure\.env"}
    assert validate_config(config, load_registry()) == ()

    for source in (
        {"kind": "wrong", "path": r"C:\secure\.env"},
        {"kind": "external_env", "path": "relative.env"},
        {"kind": "external_env", "path": r"C:\secure\.env", "extra": True},
    ):
        changed = valid_config()
        changed["credential_source"] = source
        findings = validate_config(changed, load_registry())
        assert any(finding.path.startswith("/credential_source") for finding in findings)


def test_nfc_equivalent_duplicate_mapping_keys_are_parser_invalid() -> None:
    text = """config_version: 1
profile:
  id: torq-v5-6-live
  version: 1.0.0
binding_overrides: {}
connectors: {}
policy:
  independence_mode: profile_minimum
  unattestable_action: deny
  loop_budget: 1
  resource_limits:
    max_runtime_seconds: 60
    max_cost_cents: 100
    max_file_count: 10
    max_changed_lines: 100
  é: 1
  é: 2
"""

    with pytest.raises(ConfigSyntaxError):
        parse_config_text(text)


def test_plain_and_quoted_duplicate_string_keys_are_parser_invalid() -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_config_text('a: 1\n"a": 2\n')


@pytest.mark.parametrize(
    "payload",
    [
        b"# comments only\n",
        b"---\n",
        b"- sequence\n",
        b"scalar\n---\nsecond\n",
        b"---\nconfig_version: 1\n---\n",
        b"config_version: !!str 1\n",
        b"config_version: &unused 1\n",
        b"config_version: *missing\n",
        b"<<: {}\n",
    ],
)
def test_yaml_policy_failures_are_opaque_parser_errors(payload: bytes) -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_config_bytes(payload)


def test_quoted_merge_key_reaches_closed_schema_after_parser_preflight() -> None:
    parsed = parse_config_text('"<<": {}\n')
    findings = validate_config(parsed, load_registry())
    assert any(finding.id == "config_schema_invalid" and finding.path == "/<<" for finding in findings)


@pytest.mark.parametrize("size", [65_536, 65_537])
def test_config_parser_enforces_inclusive_byte_bound(size: int) -> None:
    payload = b"config_version: 1\n" + b"#" * (size - len(b"config_version: 1\n"))

    if size == 65_536:
        assert parse_config_bytes(payload)["config_version"] == 1
    else:
        with pytest.raises(ConfigSyntaxError):
            parse_config_bytes(payload)


def test_config_parser_rejects_bom_invalid_utf8_and_isolated_surrogate() -> None:
    for payload in (b"\xef\xbb\xbfconfig_version: 1\n", b"\xff"):
        with pytest.raises(ConfigSyntaxError):
            parse_config_bytes(payload)
    with pytest.raises(ConfigSyntaxError):
        parse_config_text("config_version: 1\n\ud800")


def test_empty_stream_rejects_and_explicit_document_end_succeeds() -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_config_bytes(b"")
    assert parse_config_text("config_version: 1\n...\n")["config_version"] == 1


@pytest.mark.parametrize("text", ['"é": 1\n"e\u0301": 2\n', "a: 1\na: 2\n"])
def test_quoted_and_plain_duplicate_string_identity_rejects(text: str) -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_config_text(text)


@pytest.mark.parametrize(
    "text",
    ["1: value\n", "true: value\n", "null: value\n", "{a: b}: value\n", "[a, b]: value\n"],
)
def test_non_string_and_complex_mapping_keys_reject(text: str) -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_config_text(text)


def test_custom_tag_distinct_from_standard_string_tag_rejects() -> None:
    with pytest.raises(ConfigSyntaxError):
        parse_config_text("value: !custom text\n")


def test_parser_policy_failures_precede_safe_loader_construction(monkeypatch) -> None:
    calls: list[str] = []

    def construction_sentinel(text: str):
        calls.append(text)
        raise AssertionError("safe loader construction must not run")

    monkeypatch.setattr("torq_cli.domain.config_schema._construct_preflighted", construction_sentinel)
    for text in ("a: 1\na: 2\n", "x: !!str 1\n", "x: &unused 1\n", "x: *missing\n"):
        with pytest.raises(ConfigSyntaxError):
            parse_config_text(text)
    assert calls == []


@pytest.mark.parametrize(
    "text",
    [
        "- value\n",
        "1: value\n",
        "true: value\n",
        "null: value\n",
        "{a: b}: value\n",
        "[a, b]: value\n",
    ],
)
def test_node_policy_failures_precede_construction_for_sequence_and_complex_key(monkeypatch, text: str) -> None:
    calls: list[str] = []

    def construction_sentinel(text: str):
        calls.append(text)
        raise AssertionError("safe loader construction must not run")

    monkeypatch.setattr("torq_cli.domain.config_schema._construct_preflighted", construction_sentinel)
    with pytest.raises(ConfigSyntaxError):
        parse_config_text(text)
    assert calls == []


def test_parser_accepts_depth_eight_and_rejects_depth_nine() -> None:
    depth_eight = "{k: " * 7 + "{x: 1}" + "}" * 7
    assert parse_config_text(depth_eight)["k"] is not None

    depth_nine = "{k: " * 8 + "{x: 1}" + "}" * 8
    with pytest.raises(ConfigSyntaxError):
        parse_config_text(depth_nine)


def test_parser_event_budget_is_1024_inclusive() -> None:
    at_limit = "\n".join(f"k{index}: {index}" for index in range(510)) + "\n"
    assert len(parse_config_text(at_limit)) == 510

    over_limit = "\n".join(f"k{index}: {index}" for index in range(511)) + "\n"
    with pytest.raises(ConfigSyntaxError):
        parse_config_text(over_limit)


def test_unknown_key_rejected() -> None:
    config = valid_config()
    config["unrecognized-key"] = "secret-sentinel-value"

    findings = validate_config(config, load_registry())

    assert [finding.id for finding in findings] == ["config_schema_invalid"]
    assert "secret-sentinel-value" not in repr(findings)


def test_malformed_credential_ref_rejected() -> None:
    config = valid_config()
    config["connectors"]["builder-main"]["credential_ref"] = "credref_" + "0" * 32 + "-suffix"

    findings = validate_config(config, load_registry())

    assert [finding.id for finding in findings] == ["credential_ref_invalid"]


def test_provider_override_rejected() -> None:
    config = valid_config()
    config["binding_overrides"]["builder"]["provider_id"] = "openai"

    findings = validate_config(config, load_registry())

    assert [finding.id for finding in findings] == ["binding_override_forbidden"]


def test_nonempty_binding_override_is_required_when_role_is_overridden() -> None:
    config = valid_config()
    config["binding_overrides"]["builder"] = {}

    findings = validate_config(config, load_registry())

    assert [finding.id for finding in findings] == ["config_schema_invalid"]


def test_connector_provider_and_surface_must_match_packaged_binding() -> None:
    config = valid_config()
    config["connectors"]["builder-main"]["provider_id"] = "openai"
    config["connectors"]["builder-main"]["surface"] = "agent_sdk"

    findings = validate_config(config, load_registry())

    assert [finding.id for finding in findings] == [
        "connector_provider_mismatch",
        "connector_surface_mismatch",
    ]


def test_required_role_cannot_be_disabled() -> None:
    config = valid_config()
    config["binding_overrides"]["builder"]["enabled"] = False

    findings = validate_config(config, load_registry())

    assert [finding.id for finding in findings] == ["required_role_disabled"]


def test_future_version_rejected() -> None:
    config = valid_config()
    config["config_version"] = 2

    assert [finding.id for finding in validate_config(config, load_registry())] == [
        "config_version_unsupported"
    ]


def test_policy_and_resource_limits_are_closed_and_bounded() -> None:
    config = valid_config()
    config["policy"]["unexpected"] = True
    config["policy"]["resource_limits"]["max_file_count"] = 0

    ids = [finding.id for finding in validate_config(config, load_registry())]

    assert ids == ["config_schema_invalid", "config_schema_invalid"]


def test_hyphenated_credential_ref_is_unknown_schema_key() -> None:
    config = valid_config()
    connector = config["connectors"]["builder-main"]
    del connector["credential_ref"]
    connector["credential-ref"] = "credential-secret-sentinel"

    findings = validate_config(config, load_registry())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "config_schema_invalid"
    assert finding.path == "/connectors/builder-main/credential-ref"
    assert finding.severity.value == "high"
    assert finding.bucket == "B"
    assert finding.status_class == "invalid"
    serialized = repr(findings)
    assert "credential-secret-sentinel" not in serialized


def test_canonical_invalid_credential_ref_is_rejected() -> None:
    config = valid_config()
    config["connectors"]["builder-main"]["credential_ref"] = "not-a-valid-credref"

    findings = validate_config(config, load_registry())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "credential_ref_invalid"
    assert finding.path == "/connectors/builder-main/credential_ref"
    assert finding.severity.value == "high"
    assert finding.bucket == "B"
    assert finding.status_class == "invalid"
    assert "not-a-valid-credref" not in repr(findings)


def test_hyphenated_connector_id_is_unknown_schema_key() -> None:
    config = valid_config()
    override = config["binding_overrides"]["builder"]
    del override["connector_id"]
    override["connector-id"] = "connector-secret-sentinel"

    findings = validate_config(config, load_registry())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "config_schema_invalid"
    assert finding.path == "/binding_overrides/builder/connector-id"
    assert finding.severity.value == "high"
    assert finding.bucket == "B"
    assert finding.status_class == "invalid"
    serialized = repr(findings)
    assert "connector-secret-sentinel" not in serialized


def test_normalized_hyphenated_raw_secret_key_is_still_forbidden() -> None:
    config = valid_config()
    config["connectors"]["builder-main"]["api-key"] = "raw-secret-sentinel"

    findings = validate_config(config, load_registry())

    assert len(findings) == 1
    finding = findings[0]
    assert finding.id == "raw_credential_field_forbidden"
    assert finding.path == "/connectors/builder-main"
    assert finding.severity.value == "critical"
    assert finding.bucket == "A"
    assert finding.status_class == "invalid"
    serialized = repr(findings)
    assert "raw-secret-sentinel" not in serialized
    assert "api-key" not in serialized


@pytest.mark.parametrize(
    "member",
    ["provider_id", "model_id", "prompt_id", "prompt_version", "effort_id", "policy_id", "strategy_id", "authority", "independence"],
)
def test_every_protected_override_member_is_eligibility_forbidden(member: str) -> None:
    config = valid_config()
    config["binding_overrides"]["builder"][member] = "sentinel"

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "blocked"
    assert result.findings[0].id == "binding_override_forbidden"
    assert result.findings[0].exit_code == 3


def test_unknown_override_member_is_schema_invalid_not_forbidden() -> None:
    config = valid_config()
    config["binding_overrides"]["builder"]["ordinary_member"] = "value"

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert result.findings[0].id == "config_schema_invalid"
    assert result.findings[0].exit_code == 2


@pytest.mark.parametrize(
    "raw_key",
    [
        "api_key", "apikey", "secret", "secret_key", "access_token", "auth_token", "token",
        "password", "private_key", "credential", "credentials", "client_secret", "bearer_token",
        "openai_api_key", "anthropic_api_key", "deepseek_api_key", "kimi_api_key", "glm_api_key",
        "zai_api_key",
    ],
)
def test_full_raw_credential_denylist_uses_nfc_case_and_hyphen_normalization(raw_key: str) -> None:
    config = valid_config()
    config["connectors"]["builder-main"][raw_key.replace("_", "-").upper()] = "secret-sentinel"

    findings = validate_config(config, load_registry())

    assert any(finding.id == "raw_credential_field_forbidden" for finding in findings)
    assert "secret-sentinel" not in repr(findings)


@pytest.mark.parametrize("provider", ["anthropic", "deepseek", "openai", "moonshot", "zai"])
@pytest.mark.parametrize("surface", ["agent_sdk", "codex_sdk", "acp", "direct_api"])
def test_every_provider_and_surface_enum_member_is_shape_valid(provider: str, surface: str) -> None:
    config = valid_config()
    config["connectors"]["builder-main"]["provider_id"] = provider
    config["connectors"]["builder-main"]["surface"] = surface

    assert not validate_config_shape(config, load_registry())


@pytest.mark.parametrize("field, value", [("provider_id", "unsupported"), ("surface", "unsupported")])
def test_unsupported_provider_or_surface_is_shape_invalid(field: str, value: str) -> None:
    config = valid_config()
    config["connectors"]["builder-main"][field] = value

    findings = validate_config_shape(config, load_registry())

    assert any(finding.id == "config_schema_invalid" for finding in findings)


@pytest.mark.parametrize(
    "field, minimum, maximum",
    [
        ("max_runtime_seconds", 1, 86400),
        ("max_cost_cents", 0, 100000000),
        ("max_file_count", 1, 100000),
        ("max_changed_lines", 1, 1000000),
    ],
)
def test_resource_limits_reject_lower_upper_and_bool_and_accept_endpoints(field: str, minimum: int, maximum: int) -> None:
    for value in (minimum - 1, maximum + 1, True):
        config = valid_config()
        config["policy"]["resource_limits"][field] = value
        findings = validate_config_shape(config, load_registry())
        assert any(finding.path == f"/policy/resource_limits/{field}" for finding in findings)

    for value in (minimum, maximum):
        config = valid_config()
        config["policy"]["resource_limits"][field] = value
        findings = validate_config_shape(config, load_registry())
        assert not any(finding.path == f"/policy/resource_limits/{field}" for finding in findings)


@pytest.mark.parametrize("field", ["config_version", "profile", "binding_overrides", "connectors", "policy"])
def test_required_config_root_mapping_omission_is_invalid(field: str) -> None:
    config = valid_config()
    del config[field]

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "config_validate"
    assert any(finding.id == "config_schema_invalid" for finding in result.findings)
    assert all(finding.stage == "config_validate" for finding in result.findings)


def test_required_config_root_binding_and_connector_omission_is_invalid() -> None:
    config = valid_config()
    del config["binding_overrides"]
    del config["connectors"]

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert {finding.path for finding in result.findings} >= {
        "/binding_overrides", "/connectors",
    }
    assert all(finding.id == "config_schema_invalid" for finding in result.findings)


@pytest.mark.parametrize("field", ["config_version", "profile", "binding_overrides", "connectors", "policy"])
@pytest.mark.parametrize("invalid_value", [[], {}, True, 1, "wrong"])
def test_config_root_wrong_types_are_invalid(field: str, invalid_value) -> None:
    config = valid_config()
    if field in {"binding_overrides", "connectors"} and invalid_value == {}:
        pytest.skip("empty mappings are valid for the two mapping roots")
    config[field] = invalid_value
    if field == "config_version" and invalid_value == 1:
        pytest.skip("1 is the valid config version")

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    if isinstance(invalid_value, list):
        assert result.snapshot.resolution_stage == "config_parse"
        assert result.snapshot.config_path is None
        assert result.data == {}
    else:
        assert result.snapshot.resolution_stage == "config_validate"
    if isinstance(invalid_value, list):
        assert result.findings[0].id == "config_syntax_invalid"
    else:
        assert any(finding.id == "config_schema_invalid" for finding in result.findings)
    assert not any(finding.id == "internal_error" for finding in result.findings)


@pytest.mark.parametrize("field", ["binding_overrides", "connectors"])
def test_empty_required_mapping_roots_remain_valid(field: str) -> None:
    config = valid_config()
    config[field] = {}
    if field == "connectors":
        config["binding_overrides"] = {}

    assert validate_config(config, load_registry()) == ()


@pytest.mark.parametrize("field", ["id", "version"])
@pytest.mark.parametrize("value", [[], {}, True, 1, None])
def test_profile_identity_fields_must_be_strings_through_public_resolution(field: str, value) -> None:
    config = valid_config()
    config["profile"][field] = value

    result = resolve_text("profile_validate", yaml.safe_dump(config), "explicit.yaml")

    assert result.status == "invalid"
    assert result.snapshot is not None
    if isinstance(value, list):
        assert result.snapshot.resolution_stage == "config_parse"
        assert result.snapshot.config_path is None
        assert result.data == {}
        assert result.findings[0].id == "config_syntax_invalid"
    else:
        assert result.snapshot.resolution_stage == "config_validate"
        assert any(finding.id == "config_schema_invalid" for finding in result.findings)
    assert not any(finding.id == "internal_error" for finding in result.findings)
    if isinstance(value, list):
        assert result.snapshot.profile_id is None
        assert result.snapshot.profile_version is None
        return
    if field == "id":
        assert result.snapshot.profile_id is None
        assert result.snapshot.profile_version == "1.0.0"
    else:
        assert result.snapshot.profile_id == "torq-v5-6-live"
        assert result.snapshot.profile_version is None
