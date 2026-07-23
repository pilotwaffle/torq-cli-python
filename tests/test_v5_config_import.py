import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from torq_cli.interfaces.cli import main


REFERENCE = Path("src/torq_cli/data/oracles/torq-console-3ae19610/v5_config.normalized.json")
TARGET_SHA256 = "63ffadbe88e6b04ac732d5a282e27e0af1a2bbd80f89412ad1a4364e01a3650e"


def _source() -> bytes:
    return REFERENCE.read_bytes()


def _run(tmp_path: Path, payload: bytes, *extra: str) -> dict:
    path = tmp_path / "normalized.json"
    path.write_bytes(payload)
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(["config", "import-v5-normalized", "--config", str(path), *extra])
    result = json.loads(output.getvalue())
    result["_exit_code"] = code
    return result


def _success(tmp_path: Path) -> dict:
    return _run(tmp_path, _source())


def test_import_emits_exact_fixed_projection(tmp_path: Path) -> None:
    result = _success(tmp_path)
    value = result["data"]["canonical_target_config_utf8"].encode("utf-8")
    assert len(value) == 1029
    assert hashlib.sha256(value).hexdigest() == TARGET_SHA256


def test_projection_has_one_final_lf_and_no_bom(tmp_path: Path) -> None:
    value = _success(tmp_path)["data"]["canonical_target_config_utf8"].encode("utf-8")
    assert value.endswith(b"\n")
    assert not value[:-1].endswith(b"\n")
    assert not value.startswith(b"\xef\xbb\xbf")


def test_six_roles_are_reconciled_to_registry_authority(tmp_path: Path) -> None:
    result = _success(tmp_path)
    target = json.loads(result["data"]["canonical_target_config_utf8"])
    assert set(target["binding_overrides"]) == {"g1d", "g1r", "builder", "g2a", "refine_bug", "refine_ui"}
    assert target["binding_overrides"]["builder"]["connector_id"] == "deepseek-direct-api"
    assert target["connectors"]["anthropic-agent-sdk"]["provider_id"] == "anthropic"


def test_agent_order_does_not_change_projection(tmp_path: Path) -> None:
    document = json.loads(_source())
    document["agents"] = list(reversed(document["agents"]))
    result = _run(tmp_path, json.dumps(document, separators=(",", ":")).encode())
    assert result["data"]["canonical_target_config_utf8"].encode() == _success(tmp_path)["data"]["canonical_target_config_utf8"].encode()


def test_closed_root_schema_is_required(tmp_path: Path) -> None:
    document = json.loads(_source())
    document["extra"] = True
    result = _run(tmp_path, json.dumps(document).encode())
    assert result["findings"][0]["id"] == "legacy_config_schema_invalid"


def test_unknown_role_is_rejected(tmp_path: Path) -> None:
    document = json.loads(_source())
    document["agents"][0]["role_id"] = "unknown"
    result = _run(tmp_path, json.dumps(document).encode())
    assert result["findings"][0]["id"] == "legacy_config_role_missing"


def test_missing_role_is_rejected(tmp_path: Path) -> None:
    document = json.loads(_source())
    document["agents"] = document["agents"][1:]
    result = _run(tmp_path, json.dumps(document).encode())
    assert result["findings"][0]["id"] == "legacy_config_role_missing"


def test_duplicate_role_is_rejected(tmp_path: Path) -> None:
    document = json.loads(_source())
    document["agents"][1]["role_id"] = document["agents"][0]["role_id"]
    result = _run(tmp_path, json.dumps(document).encode())
    assert result["findings"][0]["id"] == "legacy_config_role_duplicate"


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    payload = b'{"schema":"torq-oracle-v5-config-v1","schema":"duplicate","agents":[]}'
    result = _run(tmp_path, payload)
    assert result["findings"][0]["id"] == "legacy_config_syntax_invalid"


def test_input_size_bound_is_closed(tmp_path: Path) -> None:
    result = _run(tmp_path, _source() + b" " * (65536 - len(_source()) + 1))
    assert result["findings"][0]["id"] == "legacy_config_schema_invalid"


def test_bom_is_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path, b"\xef\xbb\xbf" + _source())
    assert result["findings"][0]["id"] == "legacy_config_syntax_invalid"


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path, b"\xff")
    assert result["findings"][0]["id"] == "legacy_config_syntax_invalid"


def test_invalid_json_is_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path, b"{")
    assert result["findings"][0]["id"] == "legacy_config_syntax_invalid"


def test_secret_key_normalization_is_rejected_without_echo(tmp_path: Path) -> None:
    result = _run(tmp_path, b'{"schema":"torq-oracle-v5-config-v1","agents":[],"OpenAI_API_Key":"secret-sentinel"}')
    assert result["findings"][0]["id"] == "legacy_config_secret_field_forbidden"
    assert "secret-sentinel" not in json.dumps(result)


def test_projection_is_source_independent_and_nonsecret(tmp_path: Path) -> None:
    result = _success(tmp_path)
    rendered = json.dumps(result, sort_keys=True)
    assert "claude-sub" not in rendered
    assert "deepseek-v4-pro" not in rendered
    assert "https://" not in rendered


def test_reference_integrity_failure_precedes_input_read(monkeypatch, tmp_path: Path) -> None:
    from torq_cli.application import import_v5_config

    monkeypatch.setattr(import_v5_config, "load_v5_config_reference", lambda: ("oracle_fixture_hash_mismatch", None))
    result = _run(tmp_path, _source())
    assert result["findings"][0]["id"] == "oracle_fixture_hash_mismatch"
    assert result["data"] == {}


def test_projection_shape_is_validated_before_success(monkeypatch, tmp_path: Path) -> None:
    from torq_cli.application import import_v5_config

    monkeypatch.setattr(import_v5_config, "validate_projection", lambda *args: False)
    result = _success(tmp_path)
    assert result["findings"][0]["id"] == "legacy_config_projection_invalid"


def test_protected_input_path_is_rejected_before_read(capsys) -> None:
    code = main(["config", "import-v5-normalized", "--config", r"E:\TORQ-CONSOLE\config.json"])
    output = json.loads(capsys.readouterr().out)
    assert code == 3
    assert output["findings"][0]["id"] == "protected_path_denied"
