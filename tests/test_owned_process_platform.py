from __future__ import annotations

import os
import sys

import pytest

from torq_cli.adapters.process import OwnedProcess


@pytest.mark.skipif(os.name == "nt" or sys.platform.startswith("linux"), reason="macOS gate")
def test_macos_owned_process_fails_closed_before_starting_provider() -> None:
    with pytest.raises(OSError, match="strong_containment_unavailable"):
        OwnedProcess(("provider-must-not-start",), cwd=".", env={})
