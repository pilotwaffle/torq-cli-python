from pathlib import Path
import socket

import pytest

from conftest import guarded_getaddrinfo, validate_network_mode

from scripts.audit_extraction import REQUIRED_SUBSYSTEMS, audit_extraction
from torq_cli.domain.provider_matrix import (
    PROVIDERS,
    REQUIRED_SURFACES,
    load_provider_matrix,
    validate_provider_matrix,
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_extraction_audit_covers_every_required_subsystem_with_evidence(tmp_path: Path) -> None:
    _write(tmp_path, "torq_mmh/router/engine.py", "def run_pipeline():\n    return None\n")
    _write(tmp_path, "torq_mmh/router/adapters.py", "import httpx\n")
    _write(tmp_path, "torq_mmh/router/telemetry.py", "import sqlite3\n")
    _write(tmp_path, "torq_mmh/router/redaction.py", "def redact(value):\n    return value\n")
    _write(tmp_path, "torq_mmh/tests/test_phase0a.py", "def test_resume_after_failure_no_rebill():\n    pass\n")
    _write(tmp_path, "torq_console/conductor/compile.py", "def compile_task():\n    return {}\n")
    _write(tmp_path, "torq_console/conductor/policy.py", "def evaluate_policy():\n    return {}\n")
    _write(tmp_path, "torq_console/conductor/receipt_emitter.py", "import os\nSUPABASE_URL = os.environ.get('SUPABASE_URL')\n")
    _write(tmp_path, "torq_console/conductor/runner/drive_loop.py", "def drive():\n    return None\n")

    report = audit_extraction(tmp_path)

    assert set(report["subsystems"]) == set(REQUIRED_SUBSYSTEMS)
    for subsystem in report["subsystems"].values():
        assert subsystem["verdict"] in {"REUSE", "WRAP", "REBUILD"}
        assert subsystem["evidence"]
        assert subsystem["source_lines"] >= 1
        assert subsystem["estimated_rebuild_lines"] >= 0


def test_extraction_audit_fails_closed_when_required_sources_are_missing(tmp_path: Path) -> None:
    report = audit_extraction(tmp_path)

    assert report["complete"] is False
    assert set(report["missing_subsystems"]) == set(REQUIRED_SUBSYSTEMS)


def test_extraction_decision_document_records_verdicts_resume_and_scope() -> None:
    path = Path("docs/architecture/extraction-viability-audit.md")
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    for subsystem in REQUIRED_SUBSYSTEMS:
        assert f"`{subsystem}`" in text
    assert "397 estimated rebuild/wrapper lines" in text
    assert "test_resume_after_failure_no_rebill" in text
    assert "2 passed" in text


def test_provider_matrix_is_closed_complete_and_consumable() -> None:
    matrix = load_provider_matrix()

    assert set(matrix["providers"]) == set(PROVIDERS)
    assert matrix["surface_evidence_provenance"] == {
        "kind": "operator_transcribed_observation",
        "machine_generated": False,
        "receipt_backed": False,
    }
    assert validate_provider_matrix(matrix) == ()
    for provider in matrix["providers"].values():
        assert set(provider["surfaces"]) == set(REQUIRED_SURFACES)
        assert provider["decision"]["primary"]
        assert "usage_expectation" in provider["decision"]
        assert provider["tos"]["checked_at"] <= matrix["observed_at"]


def test_provider_matrix_rejects_missing_surface_and_missing_decision_flag() -> None:
    matrix = load_provider_matrix()
    matrix["providers"]["claude"]["surfaces"].pop("cancellation")
    matrix["providers"]["codex"]["decision"].pop("codex_direct_api_primary")

    errors = validate_provider_matrix(matrix)

    assert "claude:surface_set_invalid" in errors
    assert "codex:decision_flags_invalid" in errors


def test_runtime_repository_and_packaging_decision_is_closed() -> None:
    text = Path("docs/architecture/runtime-repository-packaging-decision.md").read_text(encoding="utf-8")
    assert "Runtime: Python 3.11+" in text
    assert "Repository: standalone" in text
    assert "Distribution: pipx/uv tool" in text
    assert "torq-cli-v0.1.0" in text
    assert "importlib.metadata" in text


def test_hermetic_network_policy_denies_non_loopback_and_rejects_invalid_mode() -> None:
    with pytest.raises(RuntimeError, match="network_egress_denied"):
        guarded_getaddrinfo(socket.getaddrinfo, "example.com", 443)
    assert guarded_getaddrinfo(socket.getaddrinfo, "127.0.0.1", 80)
    with pytest.raises(RuntimeError, match="invalid_test_network_mode"):
        validate_network_mode("count")


def test_ci_headless_job_exercises_encrypted_fallback_contract() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert workflow.count("actions/checkout@v6") == 4
    assert workflow.count("actions/setup-python@v6") == 4
    assert "actions/checkout@v4" not in workflow
    assert "actions/setup-python@v5" not in workflow
    assert "TORQ_TEST_NETWORK_MODE: deny" in workflow
    assert "TORQ_HEADLESS: '1'" in workflow
    assert "TORQ_CREDENTIAL_BACKEND: encrypted_file_test" in workflow
    assert "tests/test_headless_fallback.py" in workflow


def test_workspace_scaffold_reserves_core_cli_and_future_ui_boundaries() -> None:
    assert Path("src/torq_cli/core/__init__.py").is_file()
    assert Path("src/torq_cli/interfaces/cli.py").is_file()
    ui = Path("ui/README.md").read_text(encoding="utf-8")
    assert "v0.2" in ui
    assert "not part of the v0.1 build" in ui


def test_provider_matrix_document_names_every_provider_and_security_boundary() -> None:
    text = Path("docs/architecture/provider-surface-matrix.md").read_text(encoding="utf-8")
    for provider in PROVIDERS:
        assert f"`{provider}`" in text
    assert "2026-07-23" in text
    assert "explicit external credential source" in text
    assert "unattestable" in text
    assert "not machine-generated attestation" in text
    assert "not receipt-backed" in text
