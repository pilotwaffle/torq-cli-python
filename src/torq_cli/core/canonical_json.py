"""One pinned canonical JSON encoding for hashes, signatures, and storage."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


__all__ = ["canonical_json"]
