from __future__ import annotations

import os

import pytest

from torq_cli.adapters.process import OwnedProcess


@pytest.mark.skipif(os.name == "nt", reason="POSIX fail-closed contract")
def test_posix_owned_process_fails_closed_before_starting_provider() -> None:
    with pytest.raises(OSError, match="strong_containment_unavailable"):
        OwnedProcess(("provider-must-not-start",), cwd=".", env={})
