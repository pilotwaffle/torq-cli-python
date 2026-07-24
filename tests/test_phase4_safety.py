from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from torq_cli.safety.approval import ApprovalBoundary, ChangeProposal
from torq_cli.safety.governed import EvidenceBundle, GovernedRun, RunState
from torq_cli.safety.policy import ExecutionPolicy, ManagedProcess, PolicyHalt, ResourceCeilings
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    MemoryRunKeyStore,
    ReceiptChain,
    verify_receipt_store,
)
from torq_cli.safety.usage import summarize_usage
from torq_cli.safety.workspace import PathAccessDenied, WorkspaceBusy, WorkspaceManager, tree_hash


def test_copy_sandbox_primary_untouched_lock_dirty_policy_and_path_guards(tmp_path: Path) -> None:
    primary = tmp_path / "repo"
    primary.mkdir()
    (primary / "source.py").write_text("before", encoding="utf-8")
    (primary / ".env").write_text("FAKE_SECRET=never-read", encoding="utf-8")
    manager = WorkspaceManager(tmp_path / "sandboxes")
    before = tree_hash(primary)
    handle = manager.create(primary, "run-1", dirty=False)
    (handle.root / "source.py").write_text("after", encoding="utf-8")
    assert tree_hash(primary) == before
    with pytest.raises(WorkspaceBusy, match="workspace_lock_busy"):
        manager.create(primary, "run-2", dirty=False)
    with pytest.raises(PathAccessDenied, match="protected_path:.env"):
        handle.paths.read_text(handle.root / ".env")
    with pytest.raises(PathAccessDenied, match="path_escape"):
        handle.paths.write_text(handle.root / ".." / "escape.txt", "bad")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = handle.root / "escape-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pass  # Windows may deny unprivileged symlink creation.
    else:
        with pytest.raises(PathAccessDenied, match="path_escape|symlink_escape"):
            handle.paths.read_text(link)
    with pytest.raises(ValueError, match="dirty_primary_refused"):
        WorkspaceManager(tmp_path / "other").create(primary, "run-3", dirty=True)
    handle.release()


def test_tree_hash_does_not_follow_external_symlink(tmp_path: Path) -> None:
    primary = tmp_path / "repo-link"
    primary.mkdir()
    outside = tmp_path / "external-secret"
    outside.write_text("first", encoding="utf-8")
    link = primary / "linked"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation unavailable")
    first = tree_hash(primary)
    outside.write_text("second", encoding="utf-8")
    assert tree_hash(primary) == first


def test_execution_policy_blocks_commands_network_env_and_each_ceiling() -> None:
    policy = ExecutionPolicy(
        commands={"python", "pytest"},
        network_hosts={"localhost"},
        ceilings=ResourceCeilings(runtime_seconds=2, cost_usd=1, file_count=2, changed_lines=10),
    )
    with pytest.raises(PolicyHalt, match="command_not_allowed"):
        policy.check_command(("powershell", "x"))
    with pytest.raises(PolicyHalt, match="network_host_not_allowed"):
        policy.check_network("example.com")
    assert "API_KEY" not in policy.filter_environment({"PATH": "ok", "API_KEY": "secret"})
    fault_cases = ({"runtime_seconds": 3}, {"cost_usd": 2}, {"file_count": 3}, {"changed_lines": 11})
    expected = ("runtime_seconds", "cost_usd", "file_count", "changed_lines")
    for metrics, ceiling in zip(fault_cases, expected, strict=True):
        with pytest.raises(PolicyHalt, match=f"ceiling_exceeded:{ceiling}"):
            policy.enforce_resources(**metrics)


def test_cancellation_terminates_managed_process_tree(tmp_path: Path) -> None:
    child_code = "import time; time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]]); "
        "print(p.pid,flush=True); time.sleep(60)"
    )
    managed = ManagedProcess((sys.executable, "-c", parent_code, child_code), cwd=str(tmp_path), env=os.environ)
    assert managed.process.stdout is not None
    child_pid = int(managed.process.stdout.readline().strip())
    managed.cancel_tree()
    assert managed.process.poll() is not None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("orphaned child process survived cancellation")


def test_receipt_chain_artifact_tamper_signature_redaction_and_permissions(tmp_path: Path) -> None:
    keys = MemoryRunKeyStore()
    chain = ReceiptChain(tmp_path / "evidence", "run-1", keys, profile_version="1.0.0", policy_version="3.1.3")
    artifact = chain.write_artifact("diff.patch", "api_key='abcdefghijklmnop'")
    chain.append("design", {"artifact": str(artifact.relative_to(chain.root)), "artifact_hash": chain.hash_file(artifact)})
    chain.append("audit", {"verdict": "approve"})
    manifest = chain.seal()
    assert chain.verify(manifest).ok
    assert "abcdefghijklmnop" not in (chain.root / "receipts.jsonl").read_text(encoding="utf-8")
    if os.name != "nt":
        assert artifact.stat().st_mode & 0o077 == 0
    else:
        assert artifact.exists()  # Windows access is governed by the user profile ACL.
    original = artifact.read_bytes()
    artifact.write_bytes(original + b"x")
    assert chain.verify(manifest).ok is False


def test_receipt_verifier_rejects_artifact_path_escape_before_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"do-not-read")
    chain = ReceiptChain(tmp_path / "evidence", "run-escape", MemoryRunKeyStore(), profile_version="1", policy_version="3.1.3")
    chain.append("audit", {"artifact": "../../outside.bin", "artifact_hash": chain.hash_file(outside)})
    chain.seal()
    result = verify_receipt_store(chain.root)
    assert result.status == "tampered"
    assert result.finding == "artifact_path_escape"


def test_receipt_verifier_rejects_chain_resigned_by_untrusted_key(tmp_path: Path) -> None:
    trusted = ReceiptChain(
        tmp_path / "evidence",
        "run-1",
        MemoryRunKeyStore(),
        profile_version="1",
        policy_version="3.1.3",
    )
    trusted.append("run_attested", {"mode": "dry_run"})
    trusted.seal()
    assert verify_receipt_store(trusted.root).status == "verified"

    forged = ReceiptChain(
        tmp_path / "attacker",
        "run-1",
        MemoryRunKeyStore(),
        profile_version="1",
        policy_version="3.1.3",
    )
    forged.append("run_attested", {"mode": "live"})
    forged.seal()
    (trusted.root / "receipts.jsonl").write_bytes((forged.root / "receipts.jsonl").read_bytes())
    (trusted.root / "terminal-manifest.json").write_bytes(
        (forged.root / "terminal-manifest.json").read_bytes()
    )

    result = verify_receipt_store(trusted.root)
    assert result.status == "tampered"
    assert result.finding == "manifest_signer_untrusted"


def test_file_key_store_persists_one_signing_identity_outside_run_directories(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    first = ReceiptChain(
        evidence_root,
        "run-1",
        FileRunKeyStore(evidence_root),
        profile_version="1",
        policy_version="3.1.3",
    )
    first.append("run_attested", {"mode": "dry_run"})
    first_manifest = json.loads(first.seal().read_text(encoding="utf-8"))

    second = ReceiptChain(
        evidence_root,
        "run-2",
        FileRunKeyStore(evidence_root),
        profile_version="1",
        policy_version="3.1.3",
    )
    second.append("run_attested", {"mode": "dry_run"})
    second_manifest = json.loads(second.seal().read_text(encoding="utf-8"))

    assert first_manifest["public_key"] == second_manifest["public_key"]
    assert verify_receipt_store(first.root).status == "verified"
    assert verify_receipt_store(second.root).status == "verified"
    assert (evidence_root / ".torq-receipt-signing-key").is_file()
    assert (evidence_root / ".torq-receipt-signing-key.pub").is_file()


def test_governed_run_evidence_routing_escalation_timeline_and_loop_limit(tmp_path: Path) -> None:
    run = GovernedRun(loop_budget=1)
    with pytest.raises(ValueError, match="g2a_evidence_incomplete"):
        run.audit(EvidenceBundle((), "", "", "", {}))
    evidence = EvidenceBundle(("src/x.py",), "sha256:diff", "pytest", "passed", {"src/x.py": "sha256:file"})
    result = run.execute(evidence=evidence, defect={"severity": "HIGH", "class": "bug"}, escalation_trigger="high_risk", escalation_model="gpt-5.6-sol-high", escalation_cost=0.4)
    assert result.state is RunState.AWAITING_APPROVAL
    assert result.receipt["routing"] == "refine_bug"
    assert result.receipt["escalation"]["trigger"] == "high_risk"
    assert result.receipt["escalation"]["model"] == "gpt-5.6-sol-high"
    assert [event.stage for event in result.timeline] == ["Design", "Review", "Build", "Audit", "Repair", "Re-audit", "Awaiting approval"]
    exhausted = GovernedRun(loop_budget=0).execute(evidence=evidence, defect={"severity": "HIGH", "class": "bug"})
    assert exhausted.state is RunState.LOOP_BUDGET_EXHAUSTED


def test_apply_requires_explicit_approval_pinned_tree_and_exact_content(tmp_path: Path) -> None:
    primary = tmp_path / "repo"
    primary.mkdir()
    target = primary / "a.txt"
    target.write_text("old", encoding="utf-8")
    pinned = tree_hash(primary)
    proposal = ChangeProposal.create(pinned, {"a.txt": b"new"})
    boundary = ApprovalBoundary(primary)
    assert target.read_text(encoding="utf-8") == "old"
    with pytest.raises(PermissionError, match="explicit_approval_required"):
        boundary.apply(proposal, approved_by=None)
    target.write_text("drift", encoding="utf-8")
    with pytest.raises(ValueError, match="primary_tree_hash_mismatch.*re-run"):
        boundary.apply(proposal, approved_by="operator")
    target.write_text("old", encoding="utf-8")
    receipt = boundary.apply(proposal, approved_by="operator")
    assert target.read_text(encoding="utf-8") == "new"
    assert receipt["diff_hash"] == proposal.diff_hash


def test_usage_summary_reconstructs_totals_and_preserves_unreported() -> None:
    receipts = [
        {"agent": "g1d", "provider": "claude", "cost_usd": 0.2, "usage": {"tokens": 10}},
        {"agent": "g2a", "provider": "codex", "cost_usd": 0.3, "usage": "unreported"},
    ]
    summary = summarize_usage(receipts, budget_usd=1.0)
    assert summary["budget"] == {"consumed_usd": 0.5, "remaining_usd": 0.5}
    assert summary["providers"]["codex"]["usage"] == "unreported"
    assert json.loads(json.dumps(summary)) == summary
