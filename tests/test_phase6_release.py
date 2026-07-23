from __future__ import annotations

import json
from pathlib import Path

from torq_cli.application.e2e import run_governed_fixture
from torq_cli.interfaces.cli import main
from torq_cli.safety.receipts import verify_receipt_store


def test_governed_e2e_heterogeneous_repair_apply_and_portability(tmp_path: Path) -> None:
    report_path = run_governed_fixture(tmp_path, date="2026-07-23")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["goal"] == "Implement and test a CLI flag parser module with documented edge cases"
    assert len(set(report["live_candidate"]["providers"])) >= 2
    assert report["live_candidate"]["routing"] == "refine_bug"
    assert report["live_candidate"]["targeted_reaudit"] is True
    assert report["live_candidate"]["application"] == "hash_verified"
    assert verify_receipt_store(Path(report["live_candidate"]["receipt_root"])).status == "verified"
    assert report["repo_compat"]["mode"] == "dry_run"
    assert report["repo_compat"]["status"] == "passed"


def test_audit_security_install_and_release_documents_cover_contracts() -> None:
    audit = Path("docs/security/production-readiness-audit-2026-07-23.md").read_text(encoding="utf-8")
    for scope in ("Credential handling", "Sandbox escape", "Receipt-chain integrity", "Dual redaction", "Approval boundary", "Extraction conformance"):
        assert scope in audit
    assert "Commit baseline:" in audit and "Sandbox re-test:" in audit
    security = Path("SECURITY.md").read_text(encoding="utf-8")
    for provider in ("Claude", "Codex", "Grok", "Kimi", "Z.ai", "DeepSeek"):
        assert provider in security
    for platform in ("Windows Credential Manager", "macOS Keychain", "Linux Secret Service"):
        assert platform in security
    assert "TORQ CLI sends no product telemetry" in security
    assert "vendor SDKs/CLIs may contact" in security
    install = Path("docs/install.md").read_text(encoding="utf-8")
    assert "Windows" in install and "macOS" in install and "Linux" in install
    notes = Path("docs/releases/torq-cli-v0.1.0.md").read_text(encoding="utf-8")
    assert "SECURITY.md" in notes and "production-readiness audit" in notes and "Non-goals" in notes


def test_version_cli_uses_distribution_source_of_truth(capsys) -> None:
    try:
        code = main(["--version"])
    except SystemExit as exc:
        code = int(exc.code or 0)
    assert code == 0
    assert capsys.readouterr().out.strip() == "torq 0.1.0"

