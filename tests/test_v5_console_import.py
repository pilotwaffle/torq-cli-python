import json
from pathlib import Path

import yaml

from torq_cli.application.import_v5_console import import_v5_console_path
from torq_cli.domain.v5_console_import import parse_console_config
from torq_cli.interfaces.cli import main


FIXTURE = Path("tests/fixtures/torq-console-v5-config.yaml")
NORMALIZED_REFERENCE = Path("src/torq_cli/data/oracles/torq-console-3ae19610/v5_config.normalized.json")


def _raw() -> bytes:
    return FIXTURE.read_bytes()


def _document() -> dict:
    return yaml.safe_load(_raw())


def _dump(document: object) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


def test_raw_console_fixture_normalizes_without_manual_translation() -> None:
    finding, normalized = parse_console_config(_raw())
    assert finding is None
    assert normalized == json.loads(NORMALIZED_REFERENCE.read_text(encoding="utf-8"))


def test_agent_order_does_not_change_normalized_mapping() -> None:
    document = _document()
    document["agents"] = dict(reversed(list(document["agents"].items())))
    finding, normalized = parse_console_config(_dump(document))
    assert finding is None
    assert normalized == json.loads(NORMALIZED_REFERENCE.read_text(encoding="utf-8"))


def test_missing_unknown_and_duplicate_roles_are_rejected() -> None:
    missing = _document()
    missing["agents"].pop("g1d")
    assert parse_console_config(_dump(missing))[0] == "legacy_config_role_missing"

    unknown = _document()
    unknown["agents"]["unknown"] = unknown["agents"].pop("g1d")
    assert parse_console_config(_dump(unknown))[0] == "legacy_config_role_missing"

    duplicate = _raw().replace(b"  g1r:\n", b"  g1d:\n", 1)
    assert parse_console_config(duplicate)[0] == "legacy_config_syntax_invalid"


def test_secret_shaped_key_is_rejected_without_returning_source_data() -> None:
    payload = _raw() + b"\napi-key: secret-sentinel\n"
    finding, normalized = parse_console_config(payload)
    assert finding == "legacy_config_secret_field_forbidden"
    assert normalized is None


def test_syntax_encoding_size_and_document_failures_are_closed() -> None:
    assert parse_console_config(b"{")[0] == "legacy_config_syntax_invalid"
    assert parse_console_config(b"\xef\xbb\xbf" + _raw())[0] == "legacy_config_syntax_invalid"
    assert parse_console_config(b"\xff")[0] == "legacy_config_syntax_invalid"
    assert parse_console_config(_raw() + b" " * (65_537 - len(_raw())))[0] == "legacy_config_schema_invalid"
    assert parse_console_config(_raw() + b"\n---\n{}")[0] == "legacy_config_syntax_invalid"


def test_alias_tag_merge_and_excessive_depth_are_rejected() -> None:
    assert parse_console_config(b"version: 5\nagents: &agents {}\ncopy: *agents\n")[0] == "legacy_config_syntax_invalid"
    assert parse_console_config(b"version: 5\nagents: !custom {}\n")[0] == "legacy_config_syntax_invalid"
    assert parse_console_config(b"version: 5\nagents:\n  <<: {}\n")[0] == "legacy_config_syntax_invalid"
    deep = b"root: " + (b"[" * 9) + b"value" + (b"]" * 9)
    assert parse_console_config(deep)[0] == "legacy_config_syntax_invalid"


def test_closed_root_and_agent_shapes_are_required() -> None:
    root = _document()
    root["extra"] = True
    assert parse_console_config(_dump(root))[0] == "legacy_config_schema_invalid"

    agent = _document()
    agent["agents"]["g1d"]["extra"] = True
    assert parse_console_config(_dump(agent))[0] == "legacy_config_schema_invalid"


def test_console_version_must_be_integer_five_not_bool_or_float() -> None:
    for value in (True, 5.0, "5", 4, 6):
        document = _document()
        document["version"] = value
        assert parse_console_config(_dump(document))[0] == "legacy_config_schema_invalid"


def test_governed_lane_values_are_preserved_for_oracle_comparison() -> None:
    document = _document()
    document["agents"]["builder"]["model"] = "different-model"
    finding, normalized = parse_console_config(_dump(document))
    assert finding is None
    assert normalized is not None
    builder = next(agent for agent in normalized["agents"] if agent["role_id"] == "builder")
    assert builder["model"] == "different-model"


def _import(tmp_path: Path, payload: bytes | None = None):
    path = tmp_path / "console-config.yaml"
    path.write_bytes(_raw() if payload is None else payload)
    return import_v5_console_path(str(path))


def test_application_emits_the_existing_canonical_projection(tmp_path: Path) -> None:
    from torq_cli.application.import_v5_config import TARGET_SHA256

    result = _import(tmp_path)
    assert result.status == "ok"
    assert result.command == "config_import_v5_console"
    assert result.snapshot is not None
    assert result.snapshot.resolution_stage == "complete"
    assert result.data["source_schema"] == "torq-console-v5-config-v5"
    assert result.data["canonical_target_config_sha256"] == TARGET_SHA256
    assert result.data["runtime_effective"] is False
    assert result.data["runtime_state"] == "offline_unattested"


def test_application_rejects_lane_mismatch_against_oracle(tmp_path: Path) -> None:
    document = _document()
    document["agents"]["builder"]["model"] = "different-model"
    result = _import(tmp_path, _dump(document))
    assert result.status == "invalid"
    assert result.findings[0].id == "legacy_config_mapping_unsupported"
    assert result.data == {}


def test_application_authenticates_oracle_before_input_read(monkeypatch, tmp_path: Path) -> None:
    from torq_cli.application import import_v5_console

    monkeypatch.setattr(import_v5_console, "load_v5_config_reference", lambda: ("oracle_fixture_hash_mismatch", None))
    monkeypatch.setattr(
        import_v5_console,
        "read_bounded_legacy_config",
        lambda path: (_ for _ in ()).throw(AssertionError("input read occurred")),
    )
    result = import_v5_console_path(str(tmp_path / "missing.yaml"))
    assert result.findings[0].id == "oracle_fixture_hash_mismatch"


def test_application_projection_validation_precedes_success(monkeypatch, tmp_path: Path) -> None:
    from torq_cli.application import import_v5_console

    monkeypatch.setattr(import_v5_console, "validate_projection", lambda *args: False)
    result = _import(tmp_path)
    assert result.findings[0].id == "legacy_config_projection_invalid"


def test_application_unreadable_and_protected_inputs_are_closed(tmp_path: Path) -> None:
    unreadable = import_v5_console_path(str(tmp_path / "missing.yaml"))
    assert unreadable.findings[0].id == "legacy_config_unreadable"

    protected = import_v5_console_path(r"E:\TORQ-CONSOLE\.torq\v5\config.yaml")
    assert protected.status == "blocked"
    assert protected.findings[0].id == "protected_path_denied"


def test_application_unexpected_failure_is_non_disclosing(monkeypatch, tmp_path: Path) -> None:
    from torq_cli.application import import_v5_console

    monkeypatch.setattr(
        import_v5_console,
        "parse_console_config",
        lambda payload: (_ for _ in ()).throw(RuntimeError("secret-sentinel")),
    )
    result = _import(tmp_path)
    assert result.status == "internal_error"
    assert result.snapshot is None
    assert result.data == {}
    assert "secret-sentinel" not in repr(result)


def test_cli_imports_raw_console_yaml(tmp_path: Path, capsys) -> None:
    path = tmp_path / "console.yaml"
    path.write_bytes(_raw())
    code = main(["config", "import-v5-console", "--config", str(path)])
    rendered = json.loads(capsys.readouterr().out)
    assert code == 0
    assert rendered["command"] == "config_import_v5_console"
    assert rendered["status"] == "ok"


def test_cli_returns_invalid_and_blocked_exit_codes(tmp_path: Path, capsys) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_bytes(b"version: 4\n")
    assert main(["config", "import-v5-console", "--config", str(invalid)]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "invalid"

    assert main([
        "config", "import-v5-console", "--config", r"E:\TORQ-CONSOLE\.torq\v5\config.yaml",
    ]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"


def test_cli_console_import_internal_error_exits_five(monkeypatch, tmp_path: Path, capsys) -> None:
    from torq_cli.interfaces import cli

    monkeypatch.setattr(
        cli.import_v5_console,
        "import_v5_console_path",
        lambda path: (_ for _ in ()).throw(RuntimeError("secret-sentinel")),
    )
    code = main(["config", "import-v5-console", "--config", str(tmp_path / "input.yaml")])
    rendered = json.loads(capsys.readouterr().out)
    assert code == 5
    assert rendered["command"] == "config_import_v5_console"
    assert rendered["status"] == "internal_error"
    assert "secret-sentinel" not in json.dumps(rendered)


def test_cli_console_import_rejects_output_before_input_io(monkeypatch, capsys) -> None:
    from torq_cli.interfaces import cli

    monkeypatch.setattr(
        cli.import_v5_console,
        "import_v5_console_path",
        lambda path: (_ for _ in ()).throw(AssertionError("input read occurred")),
    )
    code = main([
        "config", "import-v5-console", "--config", r"C:\missing.yaml", "--output=target.yaml",
    ])
    rendered = json.loads(capsys.readouterr().out)
    assert code == 2
    assert rendered["command"] == "config_import_v5_console"
    assert rendered["findings"][0]["id"] == "legacy_config_projection_invalid"
