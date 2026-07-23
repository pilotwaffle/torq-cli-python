from __future__ import annotations

import json

from torq_cli.interfaces.cli import main


def test_auth_status_cli_reports_all_six_and_nonzero(capsys) -> None:
    code = main(["auth", "status"])
    output = json.loads(capsys.readouterr().out)
    assert set(output["providers"]) == {"claude", "codex", "grok", "kimi", "zai", "deepseek"}
    assert code == output["exit_code"] == 3


def test_harness_inspect_cli_flags_mismatch_and_unattestable(tmp_path, capsys) -> None:
    expected = tmp_path / "expected.json"
    actual = tmp_path / "actual.json"
    expected.write_text(json.dumps({"g1d": ["claude", "fable-5"], "g2a": ["codex", "gpt-5.5-thinking"]}), encoding="utf-8")
    actual.write_text(json.dumps({"g1d": {"provider": "claude", "model": "opus-4.8"}, "g2a": {"provider": "codex", "model": None}}), encoding="utf-8")
    code = main(["harness", "inspect", "--expected", str(expected), "--actual", str(actual)])
    output = json.loads(capsys.readouterr().out)
    assert output["agents"]["g1d"]["status"] == "mismatch"
    assert output["agents"]["g2a"]["status"] == "unattestable"
    assert code == 3

