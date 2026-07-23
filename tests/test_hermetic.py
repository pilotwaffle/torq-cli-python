import ast
import errno
import inspect
import os
from pathlib import Path
import stat

import pytest

from torq_cli.domain.hermetic import (
    PROHIBITED_IMPORTS,
    ProtectedPathError,
    LegacyConfigUnreadable,
    ReadOnlyConfigReader,
    assert_read_allowed,
)
from torq_cli.domain import hermetic as hermetic_module
from torq_cli.domain import drift_oracle


def test_protected_path_denied_before_read() -> None:
    with pytest.raises(ProtectedPathError) as error:
        assert_read_allowed(r"E:\TORQ-CONSOLE\torq_console\secret.yaml")

    assert error.value.finding_id == "protected_path_denied"


def test_explicit_config_path_is_allowed(tmp_path) -> None:
    assert_read_allowed(str(tmp_path / "torq-config.yaml"))


def test_production_imports_forbid_subprocess() -> None:
    assert "subprocess" in PROHIBITED_IMPORTS
    source_root = Path("src/torq_cli")
    for source_path in source_root.rglob("*.py"):
        if source_path.name == "hermetic.py":
            local_allow = {"os", "stat", "ctypes"}
        elif source_path.as_posix().endswith("torq_cli/adapters/process.py"):
            local_allow = {"os", "subprocess"}
        elif source_path.as_posix().endswith(("torq_cli/safety/workspace.py", "torq_cli/safety/receipts.py")):
            local_allow = {"os"}
        else:
            local_allow = set()
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
                assert not (names & PROHIBITED_IMPORTS - local_allow), source_path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in PROHIBITED_IMPORTS - local_allow, source_path


def test_reader_checks_guard_before_open(monkeypatch) -> None:
    def fail_open(*args, **kwargs):
        raise AssertionError("open must not run for a protected path")

    monkeypatch.setattr("builtins.open", fail_open)
    with pytest.raises(ProtectedPathError):
        ReadOnlyConfigReader().read_utf8(r"E:\TORQ-CONSOLE\config.yaml")


def test_canonical_resolver_alias_is_denied_before_read() -> None:
    def resolver(path):
        return Path(r"E:\TORQ-CONSOLE\aliased.yaml")

    with pytest.raises(ProtectedPathError):
        assert_read_allowed(r"C:\safe\alias.yaml", resolver=resolver)


def test_oracle_has_no_upstream_worktree_read() -> None:
    source = inspect.getsource(drift_oracle)
    assert "TORQ-CONSOLE" not in source
    assert "subprocess" not in source


def test_allowed_config_read_does_not_touch_protected_root(tmp_path, monkeypatch) -> None:
    config = tmp_path / "torq-config.yaml"
    config.write_text("profile: torq-v5-6-live\n", encoding="utf-8")
    original_is_symlink = Path.is_symlink
    touched = []

    def spy_is_symlink(path):
        if "TORQ-CONSOLE" in str(path).upper():
            touched.append(str(path))
            raise AssertionError("protected root must not be inspected")
        return original_is_symlink(path)

    monkeypatch.setattr(Path, "is_symlink", spy_is_symlink)
    assert ReadOnlyConfigReader().read_utf8(str(config)) == config.read_bytes().decode("utf-8")
    assert touched == []


def test_long_path_alias_to_protected_root_is_denied_lexically() -> None:
    with pytest.raises(ProtectedPathError):
        assert_read_allowed(r"\\?\E:\TORQ-CONSOLE\config.yaml")


@pytest.mark.parametrize("state", ["symlink", "junction", "reparse", "uncertain"])
def test_reparse_or_uncertain_candidate_component_fails_closed(state: str) -> None:
    def classify(component: str) -> str:
        return state if component.endswith("unsafe") else "ordinary"

    with pytest.raises(ProtectedPathError):
        assert_read_allowed(r"C:\safe\unsafe\config.yaml", component_classifier=classify)


@pytest.mark.parametrize(
    "path",
    [
        "config.yaml",
        r".\config.yaml",
        r"C:config.yaml",
        r"C:.\config.yaml",
        r"\\server\share\config.yaml",
        r"\\?\C:\safe\config.yaml",
        r"\\.\C:\safe\config.yaml",
    ],
)
def test_non_absolute_path_forms_are_denied_before_inspection(path: str) -> None:
    def fail_classifier(component: str) -> str:
        raise AssertionError(f"component inspected for denied path: {component}")

    with pytest.raises(ProtectedPathError):
        assert_read_allowed(path, component_classifier=fail_classifier)


@pytest.mark.parametrize(
    "path",
    [r"\config.yaml", r"C:config.yaml"],
)
def test_windows_root_relative_and_drive_relative_forms_are_denied_before_inspection(path: str) -> None:
    def fail_classifier(component: str) -> str:
        raise AssertionError(f"component inspected for denied path: {component}")

    with pytest.raises(ProtectedPathError):
        assert_read_allowed(path, component_classifier=fail_classifier)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/tmp/Torq/Config.yaml", "/tmp/Torq/Config.yaml"),
        ("/tmp/Torq/../Config.yaml", "/tmp/Config.yaml"),
    ],
)
def test_posix_absolute_paths_are_lexical_and_case_preserving(monkeypatch, path: str, expected: str) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    inspected: list[str] = []

    def classify(component: str) -> str:
        inspected.append(component)
        return "ordinary"

    candidate = assert_read_allowed(path, component_classifier=classify)

    assert str(candidate).replace("\\", "/") == expected
    assert inspected[-1] == expected


def test_allowed_candidate_is_inspected_and_read_without_resolve(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    inspected: list[str] = []
    opened: list[str] = []

    def classify(component: str) -> str:
        inspected.append(component)
        return "ordinary"

    def fake_bounded(candidate):
        opened.append(candidate)
        return b"config"

    def forbidden_resolve(*args, **kwargs):
        raise AssertionError("resolve must not run")

    monkeypatch.setattr(hermetic_module, "read_bounded_legacy_config", fake_bounded)
    monkeypatch.setattr(Path, "resolve", forbidden_resolve)
    ReadOnlyConfigReader(component_classifier=classify).read_utf8("/tmp/Torq/Config.yaml")

    assert opened
    assert opened[0].replace("\\", "/") == "/tmp/Torq/Config.yaml"
    assert inspected[-1] == "/tmp/Torq/Config.yaml"


def test_reader_inspects_and_opens_the_same_normalized_candidate(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "win32")
    inspected: list[str] = []
    opened: list[str] = []

    def classify(component: str) -> str:
        inspected.append(component)
        return "ordinary"

    def fake_bounded(candidate):
        opened.append(candidate)
        return b"config"

    monkeypatch.setattr(hermetic_module, "read_bounded_legacy_config", fake_bounded)
    ReadOnlyConfigReader(component_classifier=classify).read_utf8(r"C:\safe\.\folder\..\config.yaml")

    assert opened
    normalized_open = opened[0].replace("\\", "/").casefold()
    assert normalized_open == "c:/safe/config.yaml"
    assert inspected[-1] == normalized_open


@pytest.mark.parametrize("path", ["/tmp/config.yaml", "//server/share/config.yaml", r"1:\safe\config.yaml", r"C:relative.yaml"])
def test_windows_rejects_posix_unc_and_other_platform_absolute_forms_before_access(monkeypatch, path: str) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "win32")

    def forbidden(*args, **kwargs):
        raise AssertionError("denied path must not reach resolver or classifier")

    with pytest.raises(ProtectedPathError):
        assert_read_allowed(path, resolver=forbidden, component_classifier=forbidden)


@pytest.mark.parametrize("path", ["relative.yaml", "//server/share/config.yaml", r"C:\safe\config.yaml", r"\root-relative.yaml"])
def test_posix_rejects_relative_unc_and_windows_absolute_forms_before_access(monkeypatch, path: str) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")

    def forbidden(*args, **kwargs):
        raise AssertionError("denied path must not reach resolver or classifier")

    with pytest.raises(ProtectedPathError):
        assert_read_allowed(path, resolver=forbidden, component_classifier=forbidden)


def test_native_windows_absolute_candidate_is_inspected_and_opened_identically(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "win32")
    inspected: list[str] = []
    opened: list[str] = []

    def classify(component: str) -> str:
        inspected.append(component)
        return "ordinary"

    def fake_bounded(candidate):
        opened.append(candidate)
        return b"config"

    monkeypatch.setattr(hermetic_module, "read_bounded_legacy_config", fake_bounded)
    ReadOnlyConfigReader(component_classifier=classify).read_utf8(r"C:\Safe\.\Folder\..\Config.yaml")

    assert opened
    normalized_open = opened[0].replace("\\", "/").casefold()
    assert normalized_open == "c:/safe/config.yaml"
    assert inspected[-1] == normalized_open


def test_native_posix_absolute_candidate_preserves_case_and_is_not_resolved(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    inspected: list[str] = []
    opened: list[str] = []

    def classify(component: str) -> str:
        inspected.append(component)
        return "ordinary"

    def fake_bounded(candidate):
        opened.append(candidate)
        return b"config"

    monkeypatch.setattr(hermetic_module, "read_bounded_legacy_config", fake_bounded)
    monkeypatch.setattr(Path, "resolve", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("resolve called")))
    ReadOnlyConfigReader(component_classifier=classify).read_utf8("/tmp/Torq/Config.yaml")

    assert opened
    assert str(opened[0]) == "/tmp/Torq/Config.yaml"
    assert inspected[-1] == "/tmp/Torq/Config.yaml"


def test_bounded_posix_reader_uses_exact_final_flags(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    calls: list[tuple[str, int, int | None, int]] = []
    closed: list[int] = []
    monkeypatch.setattr(hermetic_module, "_classify_component", lambda component: (_ for _ in ()).throw(AssertionError("pathname classifier must not protect bounded reads")))
    monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags: calls.append(("open", flags, None, 10)) or 10)
    monkeypatch.setattr(hermetic_module, "_posix_openat", lambda parent, path, flags: calls.append((path, flags, parent, 11 if path == "tmp" else 12)) or (11 if path == "tmp" else 12))
    monkeypatch.setattr(hermetic_module, "_posix_is_directory", lambda fd: True)
    monkeypatch.setattr(hermetic_module, "_posix_fstat", lambda fd: hermetic_module.PosixIdentity(1, 2, 3, 4, True))
    monkeypatch.setattr(hermetic_module, "_posix_read", lambda fd, size: b"")
    monkeypatch.setattr(hermetic_module, "_posix_close", lambda fd: closed.append(fd))

    assert hermetic_module.read_bounded_legacy_config("/tmp/config.json") == b""
    assert calls == [
        ("open", hermetic_module.POSIX_ANCESTOR_FLAGS, None, 10),
        ("tmp", hermetic_module.POSIX_ANCESTOR_FLAGS, 10, 11),
        ("config.json", hermetic_module.POSIX_FINAL_FLAGS, 11, 12),
    ]
    assert closed == [12, 11, 10]


def test_bounded_posix_reader_loops_short_reads_and_eintr(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    reads = [InterruptedError(), b"ab", b"cd", b""]
    monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags: 7)
    monkeypatch.setattr(hermetic_module, "_posix_openat", lambda parent, path, flags: 7)
    monkeypatch.setattr(hermetic_module, "_posix_is_directory", lambda fd: True)
    monkeypatch.setattr(hermetic_module, "_posix_fstat", lambda fd: hermetic_module.PosixIdentity(1, 2, 3, 4, True))
    monkeypatch.setattr(hermetic_module, "_posix_read", lambda fd, size: (_ for _ in ()).throw(reads.pop(0)) if isinstance(reads[0], BaseException) else reads.pop(0))
    monkeypatch.setattr(hermetic_module, "_posix_close", lambda fd: None)

    assert hermetic_module.read_bounded_legacy_config("/tmp/config.json") == b"abcd"


def test_bounded_posix_reader_rejects_nonregular_without_read(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    native_open = hermetic_module._posix_open
    reads: list[int] = []
    fds = iter([10, 11, 12])
    monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_openat", lambda parent, path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_is_directory", lambda fd: True)
    monkeypatch.setattr(hermetic_module, "_posix_fstat", lambda fd: hermetic_module.PosixIdentity(1, 2, 3, 4, False))
    monkeypatch.setattr(hermetic_module, "_posix_read", lambda fd, size: reads.append(fd) or b"")
    closed: list[int] = []
    monkeypatch.setattr(hermetic_module, "_posix_close", lambda fd: closed.append(fd))

    with pytest.raises(ProtectedPathError):
        hermetic_module.read_bounded_legacy_config("/tmp/config.json")
    assert reads == []
    assert closed == [12, 11, 10]

    for failure in (
        AttributeError("missing os.open"),
        NotImplementedError(),
        OSError(errno.ENOSYS, "unsupported"),
        OSError(errno.ENOTSUP, "unsupported"),
    ):
        monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags, failure=failure: (_ for _ in ()).throw(failure))
        with pytest.raises(ProtectedPathError):
            hermetic_module.read_bounded_legacy_config("/tmp/config.json")

    monkeypatch.setattr(
        hermetic_module,
        "_posix_open",
        native_open,
    )
    monkeypatch.setattr(
        hermetic_module.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(AttributeError("missing os.open")),
    )
    with pytest.raises(ProtectedPathError):
        hermetic_module._posix_open("/", hermetic_module.POSIX_ANCESTOR_FLAGS)


def test_bounded_posix_reader_rejects_hardlink_identity_without_read(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    original = tmp_path / "original.yaml"
    hardlink = tmp_path / "hardlink.yaml"
    original.write_bytes(b"config")
    os.link(original, hardlink)
    metadata = os.stat(hardlink, follow_symlinks=False)
    assert metadata.st_nlink > 1
    identity = hermetic_module.PosixIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1,
    )
    fds = iter([10, 11, 12])
    reads: list[int] = []
    closed: list[int] = []
    monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_openat", lambda parent, path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_is_directory", lambda fd: True)
    monkeypatch.setattr(hermetic_module, "_posix_fstat", lambda fd: identity)
    monkeypatch.setattr(hermetic_module, "_posix_read", lambda fd, size: reads.append(fd) or b"")
    monkeypatch.setattr(hermetic_module, "_posix_close", lambda fd: closed.append(fd))

    with pytest.raises(ProtectedPathError):
        hermetic_module.read_bounded_legacy_config("/tmp/hardlink.yaml")

    assert reads == []
    assert closed == [12, 11, 10]


def test_bounded_posix_reader_closes_descriptor_on_failure(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    closed: list[int] = []
    fds = iter([10, 11, 12])
    monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_openat", lambda parent, path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_is_directory", lambda fd: True)
    monkeypatch.setattr(hermetic_module, "_posix_fstat", lambda fd: hermetic_module.PosixIdentity(1, 2, 3, 4, True))
    monkeypatch.setattr(hermetic_module, "_posix_read", lambda fd, size: (_ for _ in ()).throw(OSError("read")))
    monkeypatch.setattr(hermetic_module, "_posix_close", lambda fd: closed.append(fd))

    with pytest.raises(LegacyConfigUnreadable):
        hermetic_module.read_bounded_legacy_config("/tmp/config.json")
    assert closed == [12, 11, 10]


def test_windows_reserved_stems_are_rejected_before_handle_open(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "win32")
    monkeypatch.setattr(hermetic_module, "_windows_open", lambda *args: (_ for _ in ()).throw(AssertionError("opened")))

    for path in (r"C:\CON.txt", r"C:\NUL.json", r"C:\COM1.cfg", r"C:\LPT9.anything"):
        with pytest.raises(ProtectedPathError):
            hermetic_module.read_bounded_legacy_config(path)


def test_windows_reader_passes_exact_handle_constants(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "win32")
    calls: list[tuple[str, int, int, int, int]] = []
    events: list[str] = []
    fds = iter([10, 11, 12])
    monkeypatch.setattr(hermetic_module, "_windows_open", lambda path, access, share, disposition, flags: (events.append("open"), calls.append((path, access, share, disposition, flags)), next(fds))[2])
    monkeypatch.setattr(hermetic_module, "_windows_is_directory", lambda handle: events.append("directory") or True)
    monkeypatch.setattr(hermetic_module, "_windows_read", lambda handle, size: events.append("read") or b"")
    monkeypatch.setattr(hermetic_module, "_windows_identity", lambda handle: events.append("identity") or (1, 2, 3, 4, True))
    monkeypatch.setattr(hermetic_module, "_windows_long_path", lambda path: events.append("long") or path)
    monkeypatch.setattr(hermetic_module, "_windows_volume_guid", lambda path: events.append("volume") or "\\\\?\\Volume{1}\\")
    monkeypatch.setattr(hermetic_module, "_windows_canonical_path", lambda path, handle: events.append("canonical") or r"\\?\Volume{1}\safe\config.json")
    closed: list[int] = []
    monkeypatch.setattr(hermetic_module, "_windows_close", lambda handle: (events.append("close"), closed.append(handle)))

    assert hermetic_module.read_bounded_legacy_config(r"C:\safe\config.json") == b""
    assert calls == [
        ("c:\\", hermetic_module.FILE_READ_ATTRIBUTES, hermetic_module.FILE_SHARE_READ, hermetic_module.OPEN_EXISTING, hermetic_module.WINDOWS_ANCESTOR_FLAGS),
        (r"c:\safe", hermetic_module.FILE_READ_ATTRIBUTES, hermetic_module.FILE_SHARE_READ, hermetic_module.OPEN_EXISTING, hermetic_module.WINDOWS_ANCESTOR_FLAGS),
        (r"c:\safe\config.json", hermetic_module.WINDOWS_FINAL_ACCESS, hermetic_module.FILE_SHARE_READ, hermetic_module.OPEN_EXISTING, hermetic_module.FILE_FLAG_OPEN_REPARSE_POINT),
    ]
    assert closed == [12, 11, 10]
    assert events == ["open", "directory", "open", "directory", "open", "identity", "long", "volume", "canonical", "read", "identity", "close", "close", "close"]


def test_bounded_reader_fails_closed_on_identity_mutation_and_closes(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "linux")
    closed: list[int] = []
    identities = iter([
        hermetic_module.PosixIdentity(1, 2, 3, 4, True),
        hermetic_module.PosixIdentity(1, 2, 9, 4, True),
    ])
    fds = iter([10, 11, 12])
    monkeypatch.setattr(hermetic_module, "_posix_open", lambda path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_openat", lambda parent, path, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_posix_is_directory", lambda fd: True)
    monkeypatch.setattr(hermetic_module, "_posix_fstat", lambda fd: next(identities))
    monkeypatch.setattr(hermetic_module, "_posix_read", lambda fd, size: b"")
    monkeypatch.setattr(hermetic_module, "_posix_close", lambda fd: closed.append(fd))

    with pytest.raises(ProtectedPathError):
        hermetic_module.read_bounded_legacy_config("/tmp/config.json")
    assert closed == [12, 11, 10]


def test_windows_alias_and_volume_mismatch_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(hermetic_module.sys, "platform", "win32")
    native_open = hermetic_module._windows_open
    fds = iter([10, 11, 12])
    monkeypatch.setattr(hermetic_module, "_windows_open", lambda path, access, share, disposition, flags: next(fds))
    monkeypatch.setattr(hermetic_module, "_windows_is_directory", lambda handle: True)
    monkeypatch.setattr(hermetic_module, "_windows_long_path", lambda path: path)
    monkeypatch.setattr(hermetic_module, "_windows_volume_guid", lambda path: "\\\\?\\Volume{1}\\")
    monkeypatch.setattr(hermetic_module, "_windows_canonical_path", lambda path, handle: r"\\?\Volume{other}\safe\CONFIG.JSON")
    monkeypatch.setattr(hermetic_module, "_windows_identity", lambda handle: (1, 2, 3, 4, True))
    closed: list[int] = []
    monkeypatch.setattr(hermetic_module, "_windows_close", lambda handle: closed.append(handle))

    with pytest.raises(ProtectedPathError):
        hermetic_module.read_bounded_legacy_config(r"C:\safe\config.json")
    assert closed == [12, 11, 10]

    for failure in (
        AttributeError("missing GetLongPathNameW"),
        NotImplementedError(),
        OSError(120, "unsupported"),
        OSError(1, "unsupported"),
    ):
        fds = iter([10, 11, 12])
        monkeypatch.setattr(hermetic_module, "_windows_open", lambda path, access, share, disposition, flags: next(fds))
        monkeypatch.setattr(hermetic_module, "_windows_long_path", lambda path, failure=failure: (_ for _ in ()).throw(failure))
        with pytest.raises(ProtectedPathError):
            hermetic_module.read_bounded_legacy_config(r"C:\safe\config.json")

    class MissingCreateFileW:
        pass

    fds = iter([10, 11, 12])
    monkeypatch.setattr(hermetic_module, "_windows_open", native_open)
    monkeypatch.setattr(
        hermetic_module.ctypes,
        "WinDLL",
        lambda *args, **kwargs: MissingCreateFileW(),
        raising=False,
    )
    with pytest.raises(ProtectedPathError):
        hermetic_module._windows_open(
            r"C:\safe\config.json",
            hermetic_module.WINDOWS_FINAL_ACCESS,
            hermetic_module.FILE_SHARE_READ,
            hermetic_module.OPEN_EXISTING,
            hermetic_module.FILE_FLAG_OPEN_REPARSE_POINT,
        )
