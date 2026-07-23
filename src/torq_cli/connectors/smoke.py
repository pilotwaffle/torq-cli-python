"""Manual-only independent live smoke report orchestration."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


class LiveSmokeRunner:
    ci_allowed = False

    def __init__(self, report_root: Path) -> None:
        self.report_root = report_root

    def run_independently(self, providers: Mapping[str, Callable[[], Mapping[str, Any]]], *, date: str) -> Path:
        results: dict[str, Any] = {}
        for name, operation in providers.items():
            started = time.monotonic()
            try:
                result = dict(operation())
                result.update({"status": "passed", "latency_ms": round((time.monotonic() - started) * 1000, 3)})
            except Exception as exc:
                result = {"status": "failed", "failure_class": type(exc).__name__, "latency_ms": round((time.monotonic() - started) * 1000, 3)}
            results[name] = result
        target = self.report_root / f"live-smoke-{date}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"date": date, "mode": "manual_only", "providers": results}, sort_keys=True), encoding="utf-8")
        return target

