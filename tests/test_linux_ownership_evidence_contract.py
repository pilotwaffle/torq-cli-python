from __future__ import annotations

from pathlib import Path


def test_ci_linux_experimental_gate_is_fail_closed_and_uploads_provenance() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "linux-systemd-experimental-evidence:" in workflow
    assert "timeout-minutes: 45" in workflow
    assert 'sudo /usr/bin/systemctl start "user-runtime-dir@${uid}.service"' in workflow
    assert "run_linux_ownership_evidence.py" in workflow
    assert "--bootstrap-status \"$bootstrap_status\"" in workflow
    assert "if: always()" in workflow
    assert "if-no-files-found: error" in workflow
    assert "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803" in workflow
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in workflow
    assert "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f" in workflow
    assert "persist-credentials: false" in workflow
    assert "docker" not in workflow.lower()


def test_linux_experimental_driver_pins_real_tests_and_rejects_skips() -> None:
    driver = Path("scripts/run_linux_ownership_evidence.py").read_text(encoding="utf-8")
    assert '"tests/test_owned_process_linux_kernel.py"' in driver
    assert '"tests/test_chat_end_to_end.py"' in driver
    assert '"TORQ_TEST_LINUX_SYSTEMD_CGROUP": "1"' in driver
    assert 'raise RuntimeError("linux_ownership_test_skipped")' in driver
    assert '"machine_generated_linux_systemd_experimental_evidence"' in driver
    assert '"cgroup2fs"' in driver
    assert '"--property=ProtectControlGroups=yes"' in driver
    assert '"--property=InaccessiblePaths=' in driver
    assert '"--property=RestrictAddressFamilies=AF_INET AF_INET6"' in driver
    assert '"junit_sha256"' in driver
    assert '"head_sha"' in driver
