import pytest

from torq_cli.domain.credential_backend import BackendUnavailable, select_credential_backend


def test_headless_linux_selects_encrypted_file_fallback() -> None:
    selection = select_credential_backend("linux", headless=True, secret_service_available=False)
    assert selection.backend == "encrypted_file"
    assert selection.requires_attended_unlock is True


def test_desktop_backends_are_platform_specific() -> None:
    assert select_credential_backend("windows", headless=False).backend == "windows_credential_manager"
    assert select_credential_backend("darwin", headless=False).backend == "macos_keychain"
    assert select_credential_backend("linux", headless=False, secret_service_available=True).backend == "secret_service"


def test_linux_without_secret_service_or_explicit_headless_mode_fails_closed() -> None:
    with pytest.raises(BackendUnavailable, match="secret_service_unavailable"):
        select_credential_backend("linux", headless=False, secret_service_available=False)
