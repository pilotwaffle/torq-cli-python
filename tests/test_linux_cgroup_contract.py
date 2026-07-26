from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from torq_cli.adapters import linux_cgroup
from torq_cli.adapters.linux_cgroup import LinuxSystemdCgroup


def _bare_owner(tmp_path: Path) -> LinuxSystemdCgroup:
    owner = object.__new__(LinuxSystemdCgroup)
    owner.unit = "torq-chat-contract.service"
    owner._control_group = tmp_path
    owner._leader_pid = 42
    owner._started = True
    owner._environment = {"PATH": os.environ.get("PATH", "")}
    owner._systemd_run = "/usr/bin/systemd-run"
    owner._systemctl = "/usr/bin/systemctl"
    owner._stat = "/usr/bin/stat"
    return owner


def test_command_has_pre_exec_systemd_guards_and_no_provider_secrets(tmp_path: Path) -> None:
    owner = _bare_owner(tmp_path)
    command = owner.launch_command(("provider", "--model", "safe"), cwd=str(tmp_path))
    joined = " ".join(command)
    assert "--service-type=exec" in command
    assert "--property=KillMode=control-group" in command
    assert "--property=ProtectControlGroups=yes" in command
    assert "torq_cli.adapters.linux_cgroup_exec" in command
    assert "API_SECRET" not in joined
    systemd_boundary = command.index("--")
    helper_boundary = command.index("--", systemd_boundary + 1)
    assert command[0] == "/usr/bin/systemd-run"
    assert command[systemd_boundary + 1] == sys.executable
    assert command[helper_boundary + 1 :] == ("provider", "--model", "safe")

    environment = owner.launcher_environment(
        {
            "PATH": "/usr/bin",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "ANTHROPIC_AUTH_TOKEN": "API_SECRET",
        }
    )
    assert environment == {"PATH": "/usr/bin", "XDG_RUNTIME_DIR": "/run/user/1000"}


def test_path_and_working_directory_shadows_never_replace_pinned_control_binary(
    tmp_path: Path,
) -> None:
    owner = _bare_owner(tmp_path)
    (tmp_path / "systemd-run").write_text("attacker", encoding="utf-8")
    command = owner.launch_command(("provider",), cwd=str(tmp_path))
    launcher_environment = owner.launcher_environment({"PATH": str(tmp_path)})
    assert command[0] == "/usr/bin/systemd-run"
    assert command[0] != str(tmp_path / "systemd-run")
    assert launcher_environment["PATH"] == str(tmp_path)


def test_environment_and_prompt_are_private_stdin_frame() -> None:
    frame = LinuxSystemdCgroup.framed_input(
        {"ANTHROPIC_AUTH_TOKEN": "secret", "LANG": "C.UTF-8"}, b"hello"
    )
    env_size = int.from_bytes(frame[:4], "big")
    environment = json.loads(frame[4 : 4 + env_size])
    prompt_start = 4 + env_size
    prompt_size = int.from_bytes(frame[prompt_start : prompt_start + 8], "big")
    assert environment["ANTHROPIC_AUTH_TOKEN"] == "secret"
    assert frame[prompt_start + 8 :] == b"hello"
    assert prompt_size == 5


def test_contained_supervisor_forwards_prompt_and_environment() -> None:
    command = (
        sys.executable,
        "-m",
        "torq_cli.adapters.linux_cgroup_exec",
        "--",
        sys.executable,
        "-c",
        "import os,sys; print(os.environ['TOKEN']); print(sys.stdin.read())",
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    process.stdin.write(LinuxSystemdCgroup.framed_input({"TOKEN": "private"}, b"prompt"))
    process.stdin.flush()
    assert process.stdout is not None
    assert process.stdout.readline().strip() == b"private"
    assert process.stdout.readline().strip() == b"prompt"
    assert process.wait(timeout=10) == 0


def test_population_is_read_from_kernel_cgroup_events(tmp_path: Path) -> None:
    owner = _bare_owner(tmp_path)
    (tmp_path / "cgroup.events").write_text("populated 1\nfrozen 0\n", encoding="ascii")
    assert owner.active_processes() == 1
    (tmp_path / "cgroup.events").write_text("populated 0\nfrozen 0\n", encoding="ascii")
    assert owner.active_processes() == 0


def test_missing_population_is_unknown_not_empty(tmp_path: Path) -> None:
    owner = _bare_owner(tmp_path)
    (tmp_path / "cgroup.events").write_text("frozen 0\n", encoding="ascii")
    with pytest.raises(OSError, match="population_invalid"):
        owner.active_processes()


def test_disappeared_cgroup_is_empty_only_after_unit_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _bare_owner(tmp_path)
    owner._control_group = tmp_path / "collected-unit"
    monkeypatch.setattr(linux_cgroup, "_filesystem_type", lambda path, stat: "cgroup2fs")
    assert owner.active_processes() == 0


def test_disappeared_cgroup_is_unknown_if_unified_mount_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _bare_owner(tmp_path)
    owner._control_group = tmp_path / "collected-unit"
    monkeypatch.setattr(linux_cgroup, "_filesystem_type", lambda path, stat: "tmpfs")
    with pytest.raises(OSError, match="observation_unavailable"):
        owner.active_processes()


def test_cgroup_path_rejects_traversal() -> None:
    with pytest.raises(OSError, match="path_invalid"):
        linux_cgroup._cgroup_path("/../../tmp")


def test_show_parses_properties_without_assuming_systemd_output_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _bare_owner(tmp_path)

    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["systemctl"],
            0,
            "MainPID=77\nControlGroup=/user.slice/torq.service\nActiveState=active\n",
            "",
        )

    monkeypatch.setattr(linux_cgroup.subprocess, "run", completed)
    assert owner._show() == ("active", 77, "/user.slice/torq.service")


def test_kill_targets_every_cgroup_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _bare_owner(tmp_path)
    captured: list[tuple[str, ...]] = []

    def completed(command: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(linux_cgroup.subprocess, "run", completed)
    owner._kill_unit()
    assert "--kill-who=all" in captured[0]
    assert "--signal=KILL" in captured[0]


def test_control_binary_resolution_rejects_relative_or_untrusted_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linux_cgroup.shutil, "which", lambda name, path=None: "relative")
    with pytest.raises(OSError, match="systemd_unavailable"):
        linux_cgroup._trusted_system_tool("systemd-run")

    attacker = tmp_path / "systemctl"
    attacker.write_text("attacker", encoding="utf-8")
    monkeypatch.setattr(
        linux_cgroup.shutil, "which", lambda name, path=None: str(attacker.resolve())
    )
    with pytest.raises(OSError, match="systemd_unavailable"):
        linux_cgroup._trusted_system_tool("systemctl")


@pytest.mark.parametrize(
    ("platform", "systemd", "systemctl", "filesystem", "message"),
    (
        ("darwin", "/bin/systemd-run", "/bin/systemctl", "cgroup2fs", "strong"),
        ("linux", None, "/bin/systemctl", "cgroup2fs", "systemd"),
        ("linux", "/bin/systemd-run", "/bin/systemctl", "tmpfs", "cgroup_v2"),
    ),
)
def test_feature_detection_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    systemd: str | None,
    systemctl: str | None,
    filesystem: str,
    message: str,
) -> None:
    monkeypatch.setattr(linux_cgroup.sys, "platform", platform)
    monkeypatch.setattr(
        linux_cgroup.shutil,
        "which",
        lambda command, path=None: systemd if command == "systemd-run" else systemctl,
    )
    monkeypatch.setattr(linux_cgroup, "_trusted_system_tool", lambda name: f"/usr/bin/{name}")
    if systemd is None:
        monkeypatch.setattr(
            linux_cgroup,
            "_trusted_system_tool",
            lambda name: (_ for _ in ()).throw(OSError("owned_process_systemd_unavailable")),
        )
    monkeypatch.setattr(linux_cgroup, "_filesystem_type", lambda path, stat: filesystem)
    with pytest.raises(OSError, match=message):
        LinuxSystemdCgroup()
