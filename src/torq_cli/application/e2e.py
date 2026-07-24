"""Credential-free governed composition fixture used by release validation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from torq_cli.connectors import Connector, MemoryVault, MockSurface, all_connector_specs
from torq_cli.safety.approval import ApprovalBoundary, ChangeProposal
from torq_cli.safety.governed import EvidenceBundle, GovernedRun
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store
from torq_cli.safety.usage import summarize_usage
from torq_cli.safety.workspace import WorkspaceManager


_GOAL = "Implement and test a CLI flag parser module with documented edge cases"


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _connector(name: str, model: str, agent: str, root: Path) -> tuple[Connector, str]:
    spec = all_connector_specs()[name]
    surface = MockSurface(spec.primary_surface, {"provider": name, "model": model, "choices": [{"message": {"content": f"{agent}:ok"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}}, grants={model})
    connector = Connector(spec, (surface,), MemoryVault({name: "opaque-test-handle"}), work_root=root)
    response = connector.call(model=model, prompt=_GOAL, agent=agent)
    return connector, response.visible_text


def run_governed_fixture(root: Path, *, date: str) -> Path:
    """Run the pinned composition using recorded/mock transports only."""
    primary = root / "primary"
    primary.mkdir(parents=True, exist_ok=True)
    (primary / "README.md").write_text("fixture\n", encoding="utf-8")
    handle = WorkspaceManager(root / "sandboxes").create(primary, "e2e", dirty=False)
    try:
        _claude, design = _connector("claude", "claude-fable-5", "g1d", root / "sessions")
        _deepseek, build = _connector("deepseek", "deepseek-v4-pro", "builder", root / "sessions")
        _kimi, repair = _connector("kimi", "kimi-k3", "refine_bug", root / "sessions")
        content = (
            "\"\"\"Small deterministic flag parser fixture.\"\"\"\n"
            "def parse_flag(value: str) -> bool:\n"
            "    if value in {'1', 'true', 'yes'}:\n"
            "        return True\n"
            "    if value in {'0', 'false', 'no'}:\n"
            "        return False\n"
            "    raise ValueError('unsupported flag')\n"
        ).encode()
        changed = handle.root / "flag_parser.py"
        changed.write_bytes(content)
        diff_hash = _sha256(content)
        evidence = EvidenceBundle(("flag_parser.py",), diff_hash, "pytest -q", "passed", {"flag_parser.py": _sha256(content)})
        governed = GovernedRun(loop_budget=1).execute(evidence=evidence, defect={"severity": "HIGH", "class": "bug"})
        evidence_root = root / "evidence"
        chain = ReceiptChain(evidence_root, "e2e", FileRunKeyStore(evidence_root), profile_version="1.0.0", policy_version="3.1.3")
        artifact = chain.write_artifact("flag-parser.diff", content.decode())
        chain.append("design", {"provider": "claude", "result": design})
        chain.append("build", {"provider": "deepseek", "result": build})
        chain.append("repair", {"provider": "kimi", "result": repair, "routing": governed.receipt["routing"]})
        chain.append("reaudit", {"artifact": str(artifact.relative_to(chain.root)), "artifact_hash": chain.hash_file(artifact), "verdict": "approve"})
        usage_receipts = [
            {"agent": "g1d", "provider": "claude", "cost_usd": 0.1, "usage": {"tokens": 6}},
            {"agent": "builder", "provider": "deepseek", "cost_usd": 0.2, "usage": {"tokens": 6}},
            {"agent": "refine_bug", "provider": "kimi", "cost_usd": 0.1, "usage": "unreported"},
        ]
        usage = summarize_usage(usage_receipts, budget_usd=1.0)
        chain.append("usage", usage)
        proposal = ChangeProposal.create(handle.pinned_tree_hash, {"flag_parser.py": content})
        application = ApprovalBoundary(primary).apply(proposal, approved_by="fixture-operator")
        chain.append("approval_apply", application)
        chain.seal()
        verification = verify_receipt_store(chain.root)
        report = {
            "date": date,
            "goal": _GOAL,
            "transport": "recorded_mock_only",
            "live_candidate": {
                "providers": ["claude", "deepseek", "kimi"],
                "routing": governed.receipt["routing"],
                "targeted_reaudit": governed.receipt["targeted_reaudit"],
                "application": "hash_verified" if application["diff_hash"] == proposal.diff_hash else "failed",
                "receipt_root": str(chain.root.resolve()),
                "evidence": verification.status,
                "usage": usage,
            },
            "repo_compat": {"mode": "dry_run", "profile": "torq-v5-repo-compat", "status": "passed"},
        }
        target = root / f"governed-e2e-{date}.json"
        target.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        return target
    finally:
        handle.release()
