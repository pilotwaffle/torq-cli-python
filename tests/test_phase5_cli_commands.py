from __future__ import annotations

import json

from torq_cli.interfaces.cli import main
from torq_cli.safety.receipts import MemoryRunKeyStore, ReceiptChain

from test_phase5_cli_experience import _answers


def test_setup_and_effective_status_commands(tmp_path, capsys) -> None:
    answers = tmp_path / "answers.json"
    config = tmp_path / "config.yaml"
    answers.write_text(json.dumps(_answers()), encoding="utf-8")
    assert main(["setup", "--config", str(config), "--answers", str(answers)]) == 0
    setup_output = json.loads(capsys.readouterr().out)
    assert setup_output["status"] == "configured"
    lanes = tmp_path / "lanes.json"
    lanes.write_text(json.dumps({role: {"state": "available", "granted": True, "eligible": True} for role in _answers()["bindings"]}), encoding="utf-8")
    assert main(["status", "--config", str(config), "--require-effective", "--runtime", str(lanes)]) == 0
    assert json.loads(capsys.readouterr().out)["attestation"] == "matched"


def test_run_command_dry_default_and_evidence_verify_exit_codes(tmp_path, capsys) -> None:
    identity = tmp_path / "identity.json"
    expected = tmp_path / "expected.json"
    actual = tmp_path / "actual.json"
    identity.write_text(json.dumps({"profile_version": "1", "policy_version": "3.1.3", "prompt_binding": "p", "model_resolution": "claude:fable-5", "sandbox_identity": "s", "config_version": 1, "receipt_chain_hash": "h"}), encoding="utf-8")
    expected.write_text(json.dumps({"model": "fable-5"}), encoding="utf-8")
    actual.write_text(json.dumps({"model": "fable-5"}), encoding="utf-8")
    assert main(["run", "--goal", "test", "--run-root", str(tmp_path / "runs"), "--identity", str(identity), "--expected", str(expected), "--actual", str(actual)]) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry_run"
    chain = ReceiptChain(tmp_path, "evidence", MemoryRunKeyStore(), profile_version="1", policy_version="3.1.3")
    chain.append("done", {})
    chain.seal()
    assert main(["evidence", "verify", "--run-root", str(chain.root)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "verified"
    (chain.root / "receipts.jsonl").write_text("", encoding="utf-8")
    assert main(["evidence", "verify", "--run-root", str(chain.root)]) == 4

