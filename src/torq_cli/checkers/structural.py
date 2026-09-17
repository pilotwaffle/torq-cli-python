"""Isolated structural-v1 checker. Never imports or executes candidate code."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


def _reject_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError("duplicate_key")
        value[key] = child
    return value


def _reject_constant(value: str) -> object:
    del value
    raise ValueError("non_finite")


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        manifest = json.loads(
            Path(sys.argv[1]).read_text(encoding="utf-8"),
            object_pairs_hook=_reject_pairs,
            parse_constant=_reject_constant,
        )
        if not isinstance(manifest, dict) or set(manifest) != {"contract", "files"}:
            raise ValueError("manifest_schema")
        if manifest["contract"] != "torq-structural-check-v1" or not isinstance(manifest["files"], list):
            raise ValueError("manifest_schema")
        checked: list[dict[str, str]] = []
        for raw_path in manifest["files"]:
            if not isinstance(raw_path, str):
                raise ValueError("path_schema")
            path = Path(raw_path)
            content = path.read_text(encoding="utf-8")
            suffix = path.suffix.casefold()
            if suffix == ".py":
                ast.parse(content, filename=path.name)
                validator = "python_ast"
            elif suffix == ".json":
                json.loads(
                    content,
                    object_pairs_hook=_reject_pairs,
                    parse_constant=_reject_constant,
                )
                validator = "strict_json"
            elif suffix in {".md", ".txt"}:
                validator = "utf8_text"
            else:
                raise ValueError("unsupported_extension")
            checked.append({"path": raw_path, "validator": validator})
        print(json.dumps({"contract": "torq-structural-result-v1", "checked": checked, "status": "passed"}, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, UnicodeError, SyntaxError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"contract": "torq-structural-result-v1", "reason": type(exc).__name__, "status": "failed"}, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
