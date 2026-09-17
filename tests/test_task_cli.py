from __future__ import annotations

from pathlib import Path

from torq_cli.interfaces import cli


class _Server:
    server_address = ("127.0.0.1", 8765)
    fleet_bootstrap_nonce = "nonce"

    def serve_forever(self) -> None:
        return None

    def server_close(self) -> None:
        return None


def test_task_only_fleet_launch_derives_state_roots_without_run_root(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    native = tmp_path / "claude.exe"
    native.write_bytes(b"native-placeholder")
    native.chmod(0o700)
    captured = {}

    def server(projector, **kwargs):
        del projector
        captured.update(kwargs)
        return _Server()

    monkeypatch.setattr(cli, "create_fleet_server", server)
    monkeypatch.setattr(
        cli,
        "current_environment",
        lambda: {"LOCALAPPDATA": str(tmp_path / "appdata"), "USERPROFILE": str(tmp_path)},
    )
    result = cli.main(
        [
            "fleet", "--serve", "--task-project", f"project={project}",
            "--task-provider", "claude", "--task-model", "sonnet",
            "--task-claude-bin", str(native),
        ]
    )
    output = capsys.readouterr().out
    assert result == 0, output
    assert '"status": "serving"' in output and "view=task" in output
    assert captured["task_service"] is not None


def test_ordinary_fleet_still_requires_run_root(capsys) -> None:
    assert cli.main(["fleet", "--serve"]) == 3
    assert "fleet_run_root_required" in capsys.readouterr().out
