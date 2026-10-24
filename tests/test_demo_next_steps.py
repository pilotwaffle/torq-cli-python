from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from torq_cli.interfaces.cli import main
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain


def _json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines() if line]


def _verify_commands(text: str) -> list[str]:
    return [
        line
        for line in text.splitlines()
        if line.startswith("torq evidence verify --run-root ")
    ]


def _folder_stdout(run_ids: list[str], verify_command: str) -> str:
    return json.dumps(
        {
            "status": "run_folder",
            "run_ids": run_ids,
            "verify_command": verify_command,
        }
    ) + "\n"


def _folder_stderr(run_ids: list[str], verify_command: str) -> str:
    listed = "\n".join(f"  {run_id}" for run_id in run_ids)
    return (
        "This is a folder of runs.\n"
        "Run ids:\n"
        f"{listed}\n"
        "Verify one run:\n"
        f"{verify_command}\n"
    )


def test_demo_run_note_is_on_stderr_and_parent_folder_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_root = tmp_path / "demo runs"
    code = main(
        [
            "demo",
            "--goal",
            "Add input validation to the login form",
            "--run",
            "--run-root",
            str(run_root),
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    lines = _json_lines(captured.out)
    assert [line["status"] for line in lines] == ["scaffolded", "dry_run_complete"]
    report = lines[1]["report"]
    assert isinstance(report, dict)
    assert report["mode"] == "dry_run"
    assert report["verdict"] == "dry_run_complete"
    assert report["dispatched_roles"] == []
    assert report["proposal"] is None
    run_id = report["run_id"]
    receipts = report["receipts"]
    assert isinstance(run_id, str)
    assert isinstance(receipts, str)
    assert "No AI provider was contacted (dry run)." in captured.err
    assert "No project files were changed." in captured.err
    assert f"Run id: {run_id}" in captured.err
    assert "evidence_missing" not in captured.out
    assert "evidence_missing" not in captured.err
    commands = _verify_commands(captured.err)
    assert commands == ["torq evidence verify --run-root " + shlex.quote(receipts)]
    assert _verify_commands(captured.out) == []
    parsed = shlex.split(commands[0])
    assert parsed[4] == receipts

    verify_code = main(parsed[1:])
    verify_captured = capsys.readouterr()
    assert verify_code == 0
    assert json.loads(verify_captured.out) == {"finding": None, "status": "verified"}

    parent_code = main(["evidence", "verify", "--run-root", str(run_root)])
    parent = capsys.readouterr()
    verify_command = "torq evidence verify --run-root " + shlex.quote(receipts)
    assert parent_code == 2
    assert parent.out == _folder_stdout([run_id], verify_command)
    assert parent.err == _folder_stderr([run_id], verify_command)
    assert json.loads(parent.out)["verify_command"] == verify_command
    assert shlex.split(verify_command)[4] == receipts
    assert "demo-inputs" not in parent.out
    assert "demo-inputs" not in parent.err
    assert "evidence_missing" not in parent.out
    assert "evidence_unstable" not in parent.out
    assert "evidence_missing" not in parent.err
    assert "evidence_unstable" not in parent.err

    fleet_code = main(["fleet", "--run-root", str(run_root)])
    fleet = capsys.readouterr()
    assert fleet_code == 2
    assert fleet.out == parent.out
    assert fleet.err == parent.err
    assert "torq-fleet-snapshot" not in fleet.out

    fleet_run_code = main(["fleet", "--run-root", receipts])
    fleet_run = capsys.readouterr()
    assert fleet_run_code == 0
    snapshot = json.loads(fleet_run.out)
    assert snapshot["verification"]["finding"] is None
    assert snapshot["verification"]["state"] == "sealed_verified"
    assert snapshot["run"]["run_id"] == run_id


def test_demo_without_run_does_not_print_the_dry_run_note(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["demo", "--goal", "plan only", "--run-root", str(tmp_path / "runs")])
    assert code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert [json.loads(line)["status"] for line in captured.out.splitlines()] == [
        "scaffolded",
        "hint",
    ]


@pytest.mark.parametrize("command", (["evidence", "verify"], ["fleet"]))
def test_folder_of_runs_lists_every_run_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: list[str]
) -> None:
    parent = tmp_path / "runs"
    parent.mkdir()
    for name in ("run-b", "run-a"):
        chain = ReceiptChain(
            parent,
            name,
            FileRunKeyStore(parent),
            profile_version="1",
            policy_version="3.1.3",
        )
        chain.append("done", {})
        chain.seal()

    code = main([*command, "--run-root", str(parent)])
    captured = capsys.readouterr()
    verify_command = "torq evidence verify --run-root " + shlex.quote(str(parent / "run-a"))
    assert code == 2
    assert captured.out == _folder_stdout(["run-a", "run-b"], verify_command)
    assert captured.err == _folder_stderr(["run-a", "run-b"], verify_command)
    assert json.loads(captured.out) == {
        "status": "run_folder",
        "run_ids": ["run-a", "run-b"],
        "verify_command": verify_command,
    }
    assert "evidence_missing" not in captured.out
    assert "evidence_unstable" not in captured.out
    assert "evidence_missing" not in captured.err
    assert "evidence_unstable" not in captured.err

    single = main(["evidence", "verify", "--run-root", str(parent / "run-a")])
    single_out = capsys.readouterr()
    assert single == 0
    assert json.loads(single_out.out) == {"finding": None, "status": "verified"}


def test_single_run_failures_keep_their_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing-run"
    missing.mkdir()
    assert main(["evidence", "verify", "--run-root", str(missing)]) == 4
    assert json.loads(capsys.readouterr().out) == {
        "finding": "evidence_missing",
        "status": "incomplete",
    }

    partial = tmp_path / "partial"
    (partial / "run-partial").mkdir(parents=True)
    assert main(["evidence", "verify", "--run-root", str(partial)]) == 4
    assert json.loads(capsys.readouterr().out)["finding"] == "evidence_missing"

    chain = ReceiptChain(
        tmp_path,
        "run-real",
        FileRunKeyStore(tmp_path),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    chain.append("design", {"ok": True})
    chain.seal()
    receipt_path = chain.root / "receipts.jsonl"
    receipt_path.write_text(
        receipt_path.read_text(encoding="utf-8").replace("design", "edited"),
        encoding="utf-8",
    )
    assert main(["evidence", "verify", "--run-root", str(chain.root)]) == 3
    tampered = json.loads(capsys.readouterr().out)
    assert tampered == {"finding": "receipt_hash_mismatch", "status": "tampered"}

    assert main(["fleet", "--run-root", str(chain.root)]) == 3
    fleet = json.loads(capsys.readouterr().out)
    assert fleet["verification"]["state"] == "tampered"
    assert fleet["verification"]["finding"] == "receipt_hash_mismatch"

    empty = tmp_path / "empty-fleet"
    empty.mkdir()
    assert main(["fleet", "--run-root", str(empty)]) == 4
    fleet_empty = json.loads(capsys.readouterr().out)
    assert fleet_empty["verification"]["finding"] == "evidence_unstable"
    assert fleet_empty["verification"]["state"] == "incomplete"


def test_top_level_help_describes_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for phrase in (
        "Validate a role profile",
        "Attest machine and role status",
        "Import Console V5 configuration",
        "Store and check provider credentials",
        "Inspect the live harness",
        "Write local config from answers",
        "Run a governed plan, dry-run by default",
        "Verify a run's receipt chain",
        "Show the evidence-backed fleet view for one run",
        "Scaffold a zero-config dry-run demo (no providers contacted)",
        "Report production-trust gaps",
    ):
        assert phrase in help_text
