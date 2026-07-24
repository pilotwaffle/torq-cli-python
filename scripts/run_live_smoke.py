"""Manual T-21 six-provider live smoke runner.

This command is intentionally excluded from CI. It accepts one explicit env
file, scopes every provider child to its own credential, and persists only
redacted result metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from torq_cli.connectors.credential_sources import (
    ExplicitEnvVault,
    claude_compatible_environment,
    safe_child_environment,
)
from torq_cli.connectors.smoke import LiveSmokeRunner


PROMPT = "Return exactly TORQ_SMOKE_OK and nothing else. Do not use tools."
MODELS = {
    "claude": "claude-fable-5",
    "codex": "gpt-5.5",
    "qwen": "qwen3.8-max-preview",
    "kimi": "k3",
    "zai": "glm-5.2",
    "deepseek": "deepseek-v4-pro",
}


def _result_text(payload: Mapping[str, Any]) -> str:
    value = payload.get("result")
    return value.strip() if isinstance(value, str) else ""


def _claude_probe(
    provider: str,
    model: str,
    vault: ExplicitEnvVault,
    base_environment: Mapping[str, str],
) -> Mapping[str, Any]:
    environment = (
        safe_child_environment(base_environment)
        if provider == "claude"
        else claude_compatible_environment(provider, vault, base_environment)
    )
    completed = subprocess.run(
        (
            "claude", "-p", PROMPT, "--output-format", "json", "--model", model,
            "--tools", "", "--no-session-persistence", "--permission-mode", "plan",
            "--disable-slash-commands",
        ),
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{provider}_provider_command_failed")
    payload = json.loads(completed.stdout)
    usage = payload.get("usage")
    model_usage = payload.get("modelUsage")
    resolved = sorted(model_usage) if isinstance(model_usage, dict) else []
    if _result_text(payload) != "TORQ_SMOKE_OK":
        raise RuntimeError(f"{provider}_response_invalid")
    if model not in resolved:
        raise RuntimeError(f"{provider}_model_unattested")
    return {
        "requested_model": model,
        "resolved_models": resolved,
        "usage": usage if isinstance(usage, dict) else "unreported",
        "response_valid": True,
    }


def _openai_probe(vault: ExplicitEnvVault) -> Mapping[str, Any]:
    credential = vault.get("codex")
    if credential is None:
        raise RuntimeError("codex_credential_missing")
    body = json.dumps({
        "model": MODELS["codex"],
        "input": "Return exactly TORQ_SMOKE_OK and nothing else.",
        "max_output_tokens": 32,
    }).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    texts = [
        content.get("text", "")
        for item in payload.get("output", [])
        for content in item.get("content", [])
        if isinstance(content, dict)
    ]
    resolved = payload.get("model")
    if "".join(texts).strip() != "TORQ_SMOKE_OK":
        raise RuntimeError("codex_response_invalid")
    if not isinstance(resolved, str) or not resolved.startswith("gpt-5.5"):
        raise RuntimeError("codex_model_unattested")
    return {
        "requested_model": MODELS["codex"],
        "resolved_models": [resolved],
        "usage": payload.get("usage", "unreported"),
        "response_valid": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--credential-file", type=Path, required=True)
    parser.add_argument("--report-root", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--authorize-live", action="store_true")
    args = parser.parse_args()
    if not args.authorize_live:
        parser.error("--authorize-live is required")
    vault = ExplicitEnvVault(args.credential_file)
    providers = {
        "claude": lambda: _claude_probe("claude", MODELS["claude"], vault, os.environ),
        "codex": lambda: _openai_probe(vault),
        "qwen": lambda: _claude_probe("qwen", MODELS["qwen"], vault, os.environ),
        "kimi": lambda: _claude_probe("kimi", MODELS["kimi"], vault, os.environ),
        "zai": lambda: _claude_probe("zai", MODELS["zai"], vault, os.environ),
        "deepseek": lambda: _claude_probe("deepseek", MODELS["deepseek"], vault, os.environ),
    }
    report = LiveSmokeRunner(args.report_root).run_independently(providers, date=args.date)
    payload = json.loads(report.read_text(encoding="utf-8"))
    passed = all(result.get("status") == "passed" for result in payload["providers"].values())
    print(json.dumps({"report": str(report), "status": "passed" if passed else "failed"}, sort_keys=True))
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
