from __future__ import annotations

from pathlib import Path

import pytest

from torq_cli.application.run_command import ResumeMismatch, RunController, RunIdentity
from torq_cli.application.setup import SetupError, SetupService
from torq_cli.application.status_effective import effective_status
from torq_cli.safety.receipts import FileRunKeyStore, ReceiptChain, verify_receipt_store


def _answers() -> dict:
    return {
        "config_version": 1,
        "profile": "torq-v5-6-live",
        "profile_version": "1.0.0",
        "policy_version": "3.1.3",
        "bindings": {
            "g1d": {"provider": "claude", "model": "claude-fable-5"},
            "g1r": {"provider": "claude", "model": "claude-opus-4-8"},
            "builder": {"provider": "deepseek", "model": "deepseek-v4-pro"},
            "g2a": {"provider": "codex", "model": "gpt-5.5"},
            "refine_bug": {"provider": "kimi", "model": "k3"},
            "refine_ui": {"provider": "zai", "model": "glm-5.2"},
        },
        "grants": {
            "claude": ["claude-fable-5", "claude-opus-4-8"], "deepseek": ["deepseek-v4-pro"],
            "codex": ["gpt-5.5"], "kimi": ["k3"], "zai": ["glm-5.2"],
        },
        "policy": {"independence_mode": "profile_minimum", "loop_budget": 2, "ceilings": {"runtime_seconds": 300, "cost_usd": 5, "file_count": 50, "changed_lines": 1000}},
    }


def test_setup_zero_to_valid_idempotent_and_named_failures(tmp_path: Path) -> None:
    target = tmp_path / "config.yaml"
    service = SetupService()
    first = service.configure(target, _answers())
    second = service.configure(target, _answers())
    assert first == second
    bad = _answers()
    bad["bindings"]["builder"] = {"provider": "zai", "model": "glm-5.2"}
    with pytest.raises(SetupError, match="eligibility:ru_only"):
        service.configure(target, bad)
    ungranted = _answers()
    ungranted["grants"]["claude"] = ["claude-opus-4-8"]
    with pytest.raises(SetupError, match="model_not_granted:claude-fable-5"):
        service.configure(target, ungranted)


def test_run_prime_directive_default_dry_double_opt_in_and_resume_divergences(tmp_path: Path) -> None:
    identity = RunIdentity("1.0.0", "3.1.3", "prompt-v1", "claude:fable-5", "sandbox-1", 1, "receipt-hash")
    controller = RunController(tmp_path)
    with pytest.raises(ValueError, match="attestation_mismatch:model"):
        controller.start(identity, {"model": "opus-4.8"}, expected={"model": "fable-5"})
    with pytest.raises(ValueError, match="attestation_unattestable:model"):
        controller.start(identity, {"model": None}, expected={"model": "fable-5"})
    started = controller.start(identity, {"model": "fable-5"}, expected={"model": "fable-5"})
    assert started["mode"] == "dry_run"
    assert started["verdict"] == "dry_run_complete"
    assert started["planned_roles"] == ("g1d", "g1r", "builder", "g2a")
    with pytest.raises(ValueError, match="double_opt_in_required"):
        controller.start(identity, {"model": "fable-5"}, expected={"model": "fable-5"}, live=True, live_opt_in=True)
    with pytest.raises(ValueError, match="live_dispatcher_required"):
        controller.start(
            identity,
            {"model": "fable-5"},
            expected={"model": "fable-5"},
            live=True,
            live_opt_in=True,
            policy_opt_in=True,
        )
    checkpoint = controller.cancel("run-1", identity, completed=("g1d",))
    assert checkpoint.exists()
    assert controller.resume(checkpoint, identity, stages=("g1d", "g1r", "builder")) == ("g1r", "builder")
    for field in RunIdentity.__dataclass_fields__:
        changed = RunIdentity(**{**identity.__dict__, field: "changed" if field != "config_version" else 2})
        with pytest.raises(ResumeMismatch, match=field):
            controller.resume(checkpoint, changed, stages=("g1d", "g1r"))


@pytest.mark.parametrize("broken", ["disconnected", "ungranted", "ineligible", "degraded"])
def test_require_effective_is_nonzero_for_four_broken_states(broken: str) -> None:
    lanes = {role: {"state": "available", "granted": True, "eligible": True} for role in _answers()["bindings"]}
    if broken == "disconnected":
        lanes["g1d"]["state"] = "unavailable"
    elif broken == "ungranted":
        lanes["g1d"]["granted"] = False
    elif broken == "ineligible":
        lanes["builder"]["eligible"] = False
    else:
        lanes["refine_bug"]["state"] = "degraded"
    report = effective_status(_answers(), lanes)
    assert report["exit_code"] != 0
    assert report["profile"] == "torq-v5-6-live"
    assert report["policy_version"] == "3.1.3"
    assert report["independence_mode"] == "profile_minimum"


def test_effective_status_zero_only_when_all_lanes_effective() -> None:
    lanes = {role: {"state": "available", "granted": True, "eligible": True} for role in _answers()["bindings"]}
    assert effective_status(_answers(), lanes)["exit_code"] == 0


def test_evidence_verify_names_tamper_and_incomplete_without_network(tmp_path: Path) -> None:
    chain = ReceiptChain(tmp_path, "run", FileRunKeyStore(tmp_path), profile_version="1.0.0", policy_version="3.1.3")
    chain.append("design", {"ok": True})
    manifest = chain.seal()
    assert verify_receipt_store(chain.root).status == "verified"
    receipt_path = chain.root / "receipts.jsonl"
    receipt_path.write_text(receipt_path.read_text(encoding="utf-8").replace("design", "edited"), encoding="utf-8")
    assert verify_receipt_store(chain.root).finding == "receipt_hash_mismatch"
    receipt_path.write_text("", encoding="utf-8")
    assert verify_receipt_store(chain.root).status == "incomplete"
    manifest.write_text("{}", encoding="utf-8")
    assert verify_receipt_store(chain.root).status == "incomplete"
