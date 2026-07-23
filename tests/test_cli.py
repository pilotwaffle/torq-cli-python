import json
from importlib import resources

import yaml

from torq_cli.application import resolve as resolve_module
from torq_cli.application import import_v5_config
from torq_cli.domain import hermetic as hermetic_module
from torq_cli.interfaces import cli as cli_module
from torq_cli.interfaces.cli import main


def valid_config() -> dict:
    return {
        "config_version": 1,
        "profile": {"id": "torq-v5-6-live", "version": "1.0.0"},
        "binding_overrides": {},
        "connectors": {},
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


def test_require_effective_exits_four(tmp_path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(valid_config()), encoding="utf-8")

    code = main(["status", "--offline", "--config", str(config_path), "--require-effective"])

    assert code == 4
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "unattested"
    assert output["data"]["runtime_effective"] is False


def test_protected_config_path_is_denied_before_read(capsys) -> None:
    code = main(["profile", "validate", "--config", r"E:\TORQ-CONSOLE\config.yaml"])

    assert code == 3
    output = json.loads(capsys.readouterr().out)
    assert output["findings"][0]["id"] == "protected_path_denied"


def test_protected_cli_path_retains_config_read_snapshot(monkeypatch, capsys) -> None:
    def deny(*args, **kwargs):
        from torq_cli.domain.hermetic import ProtectedPathError

        raise ProtectedPathError("protected path access denied")

    monkeypatch.setattr(resolve_module.ReadOnlyConfigReader, "read_utf8", deny)
    config_path = r"E:\TORQ-CONSOLE\config.yaml"

    code = main(["profile", "validate", "--config", config_path])

    assert code == 3
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "blocked"
    assert output["data"] == {}
    assert len(output["findings"]) == 1
    finding = output["findings"][0]
    assert finding["id"] == "protected_path_denied"
    assert finding["severity"] == "critical"
    assert finding["bucket"] == "A"
    assert finding["status_class"] == "blocked"
    assert finding["stage"] == "config_read"
    snapshot = output["snapshot"]
    assert snapshot is not None
    assert snapshot["registry_id"] == "torq-cli-role-registry"
    assert snapshot["registry_version"] == "1.0.0"
    assert snapshot["registry_resource_sha256"] is not None
    assert snapshot["config_path"] == config_path
    assert snapshot["config_version"] is None
    assert snapshot["profile_id"] is None
    assert snapshot["profile_version"] is None
    assert snapshot["resolution_stage"] == "config_read"


def test_unexpected_cli_error_is_internal_error(monkeypatch, tmp_path, capsys) -> None:
    reference = resources.files("torq_cli").joinpath(
        "data/oracles/torq-console-3ae19610/v5_config.normalized.json"
    ).read_bytes()
    config_path = tmp_path / "normalized.json"
    config_path.write_bytes(reference)

    def explode(*args, **kwargs):
        raise AttributeError("envelope-secret-sentinel")

    monkeypatch.setattr(cli_module, "envelope_to_dict", explode)

    code = main(["config", "import-v5-normalized", "--config", str(config_path)])

    captured = capsys.readouterr()
    assert code == 5
    output = json.loads(captured.out)
    assert output["status"] == "internal_error"
    assert output["snapshot"] is None
    assert output["data"] == {}
    assert output["findings"] == [{
        "id": "internal_error",
        "message": "Internal failure occurred without exposing details.",
        "severity": "critical",
        "bucket": "A",
        "status_class": "internal_error",
        "stage": "complete",
        "path": "/",
        "context": {},
    }]
    assert "envelope-secret-sentinel" not in captured.out
    assert "envelope-secret-sentinel" not in captured.err


def test_cli_config_read_failure_retains_registry_partial_snapshot(tmp_path, capsys) -> None:
    missing_path = tmp_path / "missing-config.yaml"

    code = main(["profile", "validate", "--config", str(missing_path)])

    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["findings"][0]["id"] == "config_unreadable"
    snapshot = output["snapshot"]
    assert snapshot is not None
    assert snapshot["registry_id"] == "torq-cli-role-registry"
    assert snapshot["registry_version"] == "1.0.0"
    assert snapshot["registry_resource_sha256"] is not None
    assert snapshot["config_path"] == str(missing_path)
    assert snapshot["config_version"] is None
    assert snapshot["profile_id"] is None
    assert snapshot["profile_version"] is None
    assert snapshot["resolution_stage"] == "config_read"


def test_cli_registry_validation_precedes_single_config_read(monkeypatch, tmp_path, capsys) -> None:
    events: list[str] = []
    original_load = resolve_module.load_registry
    original_validate = resolve_module.validate_registry

    def load_once():
        events.append("registry_read")
        return original_load()

    def validate_once(registry):
        events.append("registry_validate")
        return original_validate(registry)

    def read_once(self, path):
        events.append("config_read")
        return yaml.safe_dump(valid_config())

    monkeypatch.setattr(resolve_module, "load_registry", load_once)
    monkeypatch.setattr(resolve_module, "validate_registry", validate_once)
    monkeypatch.setattr(resolve_module.ReadOnlyConfigReader, "read_utf8", read_once)

    code = main(["profile", "validate", "--config", str(tmp_path / "config.yaml")])
    capsys.readouterr()

    assert code == 0
    assert events == ["registry_read", "registry_validate", "config_read"]


def test_missing_registry_prevents_config_access_and_returns_registry_envelope(monkeypatch, capsys) -> None:
    from torq_cli.domain.registry_schema import RegistryResourceMissing

    def missing_registry():
        raise RegistryResourceMissing()

    def forbidden_read(self, path):
        raise AssertionError("config must not be accessed after registry failure")

    monkeypatch.setattr(resolve_module, "load_registry", missing_registry)
    monkeypatch.setattr(resolve_module.ReadOnlyConfigReader, "read_utf8", forbidden_read)

    config_path = r"C:\missing\bad-config.yaml"
    code = main(["profile", "validate", "--config", config_path])
    output = json.loads(capsys.readouterr().out)

    assert code == 2
    assert output["status"] == "invalid"
    assert output["data"] == {}
    assert len(output["findings"]) == 1
    finding = output["findings"][0]
    assert finding["id"] == "registry_resource_missing"
    assert finding["severity"] == "high"
    assert finding["bucket"] == "B"
    assert finding["status_class"] == "invalid"
    assert finding["stage"] == "registry_read"
    snapshot = output["snapshot"]
    assert snapshot is not None
    assert snapshot["registry_id"] is None
    assert snapshot["registry_version"] is None
    assert snapshot["registry_resource_sha256"] is None
    assert snapshot["config_path"] == config_path
    assert snapshot["config_version"] is None
    assert snapshot["profile_id"] is None
    assert snapshot["profile_version"] is None
    assert snapshot["resolution_stage"] == "registry_read"


def test_cli_import_v5_success_envelope(tmp_path, capsys) -> None:
    source = resources.files("torq_cli").joinpath(
        "data", "oracles", "torq-console-3ae19610", "v5_config.normalized.json"
    ).read_bytes()
    config_path = tmp_path / "normalized.json"
    config_path.write_bytes(source)

    code = main(["config", "import-v5-normalized", "--config", str(config_path)])
    output = json.loads(capsys.readouterr().out)

    assert code == 0
    assert output["command"] == "config_import_v5_normalized"
    assert output["status"] == "ok"


def test_cli_import_v5_invalid_is_closed(tmp_path, capsys) -> None:
    config_path = tmp_path / "normalized.json"
    config_path.write_bytes(b"{")

    code = main(["config", "import-v5-normalized", "--config", str(config_path)])
    output = json.loads(capsys.readouterr().out)

    assert code == 2
    assert output["data"] == {}


def test_cli_import_v5_protected_path_exits_three(tmp_path, monkeypatch, capsys) -> None:
    code = main(["config", "import-v5-normalized", "--config", r"E:\TORQ-CONSOLE\config.json"])
    output = json.loads(capsys.readouterr().out)

    assert code == 3
    assert output["findings"][0]["id"] == "protected_path_denied"
    assert output["data"] == {}

    unreadable_path = tmp_path / "ordinary-missing.json"
    code = main(["config", "import-v5-normalized", "--config", str(unreadable_path)])
    unreadable = json.loads(capsys.readouterr().out)

    assert code == 2
    assert unreadable["status"] == "invalid"
    assert unreadable["findings"][0]["id"] == "legacy_config_unreadable"
    assert str(unreadable_path) not in json.dumps(unreadable)
    assert unreadable["data"] == {}

    class MissingCreateFileW:
        pass

    for platform, path in (("linux", str(tmp_path / "missing-primitive.json")), ("win32", r"C:\safe\missing-primitive.json")):
        def simulated_missing_primitive(candidate: str, platform=platform) -> bytes:
            original_platform = hermetic_module.sys.platform
            original_open = hermetic_module.os.open
            original_windll = hermetic_module.ctypes.WinDLL
            try:
                hermetic_module.sys.platform = platform
                if platform == "linux":
                    hermetic_module.os.open = lambda *args, **kwargs: (_ for _ in ()).throw(AttributeError("missing os.open"))
                else:
                    hermetic_module.ctypes.WinDLL = lambda *args, **kwargs: MissingCreateFileW()
                return hermetic_module.read_bounded_legacy_config(candidate)
            finally:
                hermetic_module.sys.platform = original_platform
                hermetic_module.os.open = original_open
                hermetic_module.ctypes.WinDLL = original_windll

        monkeypatch.setattr(import_v5_config, "read_bounded_legacy_config", simulated_missing_primitive)
        code = main(["config", "import-v5-normalized", "--config", path])
        protected = json.loads(capsys.readouterr().out)

        assert code == 3
        assert protected["status"] == "blocked"
        assert protected["snapshot"] is not None
        assert protected["findings"][0]["id"] == "protected_path_denied"
        assert protected["data"] == {}
        assert path not in json.dumps(protected)


def test_cli_import_v5_rejects_all_output_forms_without_io(monkeypatch, capsys) -> None:
    calls: list[str] = []
    monkeypatch.setattr(import_v5_config, "import_v5_path", lambda path: calls.append(path))
    monkeypatch.setattr(import_v5_config, "_failure", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("_failure must not run")))
    monkeypatch.setattr(import_v5_config, "_snapshot", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("_snapshot must not run")))
    monkeypatch.setattr(import_v5_config, "load_registry", lambda: (_ for _ in ()).throw(AssertionError("registry IO must not run")))
    monkeypatch.setattr(import_v5_config, "load_v5_config_reference", lambda: (_ for _ in ()).throw(AssertionError("reference IO must not run")))
    monkeypatch.setattr(import_v5_config, "read_bounded_legacy_config", lambda path: (_ for _ in ()).throw(AssertionError("config IO must not run")))
    for value, args in (
        ("output-secret", ["--output", "output-secret", "config", "import-v5-normalized", "--malformed"]),
        ("equals-secret", ["config", "--output=equals-secret", "import-v5-normalized", "--malformed"]),
        ("bare-secret", ["config", "import-v5-normalized", "--output", "--malformed"]),
    ):
        code = main(args)
        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert code == 2
        assert value not in captured.out
        assert value not in captured.err
        assert output["status"] == "invalid"
        assert output["snapshot"] is None
        assert output["data"] == {}
        assert output["findings"] == [{
            "id": "legacy_config_projection_invalid",
            "message": "Canonical target config cannot satisfy the closed CLI config contract.",
            "severity": "high",
            "bucket": "B",
            "status_class": "invalid",
            "stage": "legacy_config_project",
            "path": "/target_config",
            "context": {},
        }]
    assert calls == []
