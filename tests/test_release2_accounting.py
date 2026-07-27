from __future__ import annotations

import json
import multiprocessing
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from torq_cli.application.orchestrator import GovernedOrchestrator, OrchestrationBlocked
from torq_cli.core.engine import NormalizedResponse
from torq_cli.core.graph import ExecutionMode
from torq_cli.domain.registry_schema import load_registry
from torq_cli.application.fleet import FleetProjector
from torq_cli.safety.accounting_registry import (
    PersistentEntitlementLedger,
    RegistryRollbackError,
)
from torq_cli.safety.entitlements import PlanWindow
from torq_cli.safety.pricing import RateTable
from torq_cli.safety.receipts import (
    FileRunKeyStore,
    ReceiptChain,
    restrict_receipt_trust_anchor,
)


_RESET = "2030-01-01T00:00:00Z"


def _windows() -> dict[str, PlanWindow]:
    return {
        "qwen-token-plan": PlanWindow(
            "qwen-token-plan",
            ("qwen", "deepseek"),
            "plan_covered",
            0,
            10,
            _RESET,
        )
    }


def _sealed_run(root: Path, run_id: str) -> None:
    chain = ReceiptChain(
        root,
        run_id,
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    chain.append("run_attested", {"mode": "dry_run"})
    chain.seal()


def _ledger(root: Path, *, now: datetime | None = None) -> PersistentEntitlementLedger:
    instant = (now or datetime(2029, 1, 1, tzinfo=UTC)).timestamp()
    return PersistentEntitlementLedger(root, _windows(), clock=lambda: instant)


def _reserve_in_process(
    root_value: str,
    start: multiprocessing.synchronize.Event,
    results: multiprocessing.queues.Queue[str],
) -> None:
    ledger = _ledger(Path(root_value))
    ledger.bind_run("run-process-race")
    start.wait(timeout=10)
    try:
        ledger.reserve("qwen", calls=10)
    except ValueError as exc:
        results.put(str(exc))
    else:
        results.put("reserved")


@pytest.mark.parametrize(("used", "reserved"), [(1, 0), (0, 1)])
def test_persistent_ledger_rejects_unauthenticated_baseline_before_key_creation(
    tmp_path: Path,
    used: int,
    reserved: int,
) -> None:
    root = tmp_path / "evidence"
    windows = _windows()
    original = windows["qwen-token-plan"]
    windows["qwen-token-plan"] = PlanWindow(
        account=original.account,
        providers=original.providers,
        settlement=original.settlement,
        used=used,
        limit=original.limit,
        resets_at=original.resets_at,
        used_source=original.used_source,
        limit_source=original.limit_source,
        reserved=reserved,
    )

    with pytest.raises(
        ValueError,
        match="^entitlement_baseline_unsupported$",
    ):
        PersistentEntitlementLedger(root, windows)
    assert not root.exists()


def test_persistent_ledger_from_config_rejects_baseline_without_filesystem_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    raw = {
        "qwen-token-plan": {
            "providers": {"qwen": True, "deepseek": True},
            "settlement": "plan_covered",
            "used": 1,
            "limit": 10,
            "resets_at": _RESET,
            "used_source": "provider_reported",
            "limit_source": "operator_declared",
        }
    }

    with pytest.raises(ValueError, match="^entitlement_baseline_unsupported$"):
        PersistentEntitlementLedger.from_config(root, raw)
    assert not root.exists()


def test_rate_table_content_hash_is_immutable_and_sealed_for_misses() -> None:
    first = RateTable.from_document(
        {
            "rate_table_version": "rates.v1",
            "rates": {
                "openai": {
                    "model": {
                        "input_usd_per_mtok": "0.1",
                        "output_usd_per_mtok": "0.2",
                    }
                }
            },
        }
    )
    changed = RateTable.from_document(
        {
            "rate_table_version": "rates.v1",
            "rates": {
                "openai": {
                    "model": {
                        "input_usd_per_mtok": "0.1000000001",
                        "output_usd_per_mtok": "0.2",
                    }
                }
            },
        }
    )

    assert first.sha256 != changed.sha256
    miss = first.quote("unknown", "unknown", {})
    assert miss.pricing_status == "rate_unknown"
    assert miss.rate_table_hash == first.sha256


def test_registry_enrollment_is_signed_append_only_and_rollback_detected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-one")
    ledger = _ledger(root)
    ledger.bind_run("run-one")
    original = ledger.registry_path.read_bytes()
    ledger.reserve("deepseek", calls=1)

    rows = ledger.verify_registry()
    assert [row["event"] for row in rows] == ["run_enrolled", "reservation_created"]
    assert rows[0]["signature"]
    ledger.registry_path.write_bytes(original)

    with pytest.raises(RegistryRollbackError, match="registry_rollback_detected"):
        ledger.verify_registry()


def test_reserve_capacity_check_and_append_are_atomic_across_threads(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-thread-race")
    ledger = _ledger(root)
    ledger.bind_run("run-thread-race")
    start = threading.Barrier(2)

    def reserve() -> str:
        start.wait(timeout=10)
        try:
            ledger.reserve("qwen", calls=10)
        except ValueError as exc:
            return str(exc)
        return "reserved"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve) for _ in range(2)]
        outcomes = sorted(future.result(timeout=20) for future in futures)

    assert outcomes == ["entitlement_window_exceeded", "reserved"]
    assert ledger.window("qwen").reserved == 10
    ledger.verify_registry()


def test_enrollment_check_and_append_are_atomic_across_instances(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-enrollment-race")
    ledgers = [_ledger(root), _ledger(root)]
    start = threading.Barrier(2)

    def bind(ledger: PersistentEntitlementLedger) -> None:
        start.wait(timeout=10)
        ledger.bind_run("run-enrollment-race")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(bind, ledger) for ledger in ledgers]
        for future in futures:
            future.result(timeout=20)

    rows = ledgers[0].verify_registry()
    assert [row["event"] for row in rows] == ["run_enrolled"]


def test_reserve_capacity_check_and_append_are_atomic_across_processes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-process-race")
    ledger = _ledger(root)
    ledger.bind_run("run-process-race")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_reserve_in_process,
            args=(str(root), start, results),
        )
        for _ in range(2)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        outcomes = sorted(results.get(timeout=20) for _ in processes)
    finally:
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()

    assert all(process.exitcode == 0 for process in processes)

    assert outcomes == ["entitlement_window_exceeded", "reserved"]
    assert _ledger(root).window("qwen").reserved == 10
    ledger.verify_registry()


def test_reconcile_selection_and_append_are_atomic_across_instances(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-reconcile-race")
    first = _ledger(root)
    first.bind_run("run-reconcile-race")
    first.reserve("qwen", calls=2)
    second = _ledger(root)
    second.bind_run("run-reconcile-race")
    ledgers = [first, second]
    start = threading.Barrier(2)

    def reconcile(ledger: PersistentEntitlementLedger) -> str:
        start.wait(timeout=10)
        try:
            ledger.reconcile("qwen", calls=1)
        except ValueError as exc:
            return str(exc)
        return "reconciled"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reconcile, ledger) for ledger in ledgers]
        outcomes = sorted(future.result(timeout=20) for future in futures)

    assert outcomes == ["entitlement_reservation_missing", "reconciled"]
    current = first.window("qwen")
    assert current.used == 1
    assert current.reserved == 0


def test_cross_run_coverage_fails_closed_when_an_enrolled_run_is_missing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-present")
    ledger = _ledger(root)
    ledger.bind_run("run-present")
    ledger.enroll("run-deleted", "qwen-token-plan", _RESET)

    report = ledger.coverage("qwen")

    assert report.as_dict()["coverage"] == "1/2"
    assert report.statuses["run-deleted"] == "missing"
    with pytest.raises(ValueError, match="entitlement_coverage_incomplete"):
        ledger.preflight("deepseek")


def test_qwen_and_deepseek_share_reservations_and_signed_reconciliation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-shared")
    ledger = _ledger(root)
    ledger.bind_run("run-shared")

    ledger.reserve("deepseek", calls=2)
    assert ledger.window("qwen").used == 0
    assert ledger.window("qwen").reserved == 2
    ledger.reconcile("deepseek", calls=1)

    assert ledger.window("qwen").used == 1
    history = json.loads(ledger.reconciliation_path.read_text(encoding="utf-8"))
    assert history["old_used"] == 0
    assert history["new_used"] == 1
    assert history["reservation_entry_ids"][0].startswith("reserve-")
    assert history["signature"]


def test_uncertain_reservation_remains_until_window_expiry(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-uncertain")
    before_reset = datetime(2029, 12, 31, tzinfo=UTC)
    ledger = _ledger(root, now=before_reset)
    ledger.bind_run("run-uncertain")
    ledger.reserve("qwen", calls=3)
    assert ledger.window("deepseek").used == 0
    assert ledger.window("deepseek").reserved == 3

    after_reset = datetime(2030, 1, 2, tzinfo=UTC)
    reopened = _ledger(root, now=after_reset)
    assert reopened.window("qwen").used == 0
    assert reopened.window("qwen").reserved == 0


def test_distrusted_root_stays_in_coverage_denominator(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-compromised")
    ledger = _ledger(root)
    ledger.bind_run("run-compromised")
    ledger.set_root_trust(ledger.root_key_id, "distrusted", actor="operator")

    report = ledger.coverage("deepseek")

    assert report.enrolled == 1
    assert report.verified == 0
    assert report.statuses == {"run-compromised": "distrusted"}


def test_rotated_root_counts_only_when_explicitly_trusted_as_legacy(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    current_root = tmp_path / "current"
    _sealed_run(legacy_root, "run-legacy")
    shutil.copytree(legacy_root / "run-legacy", current_root / "run-legacy")
    current_keys = FileRunKeyStore(current_root)
    current_keys.get_or_create_run_keys("run-legacy")
    source_anchor = next(
        (legacy_root / ".torq-run-identities").glob("*/manifest-head.v1.json")
    )
    target_anchor = next(
        (current_root / ".torq-run-identities").glob("*/manifest.key")
    ).with_name("manifest-head.v1.json")
    target_anchor.write_bytes(source_anchor.read_bytes())
    restrict_receipt_trust_anchor(target_anchor)
    legacy_public = bytes.fromhex(
        (legacy_root / ".torq-receipt-signing-key.pub")
        .read_text(encoding="ascii")
        .strip()
    )
    ledger = _ledger(current_root)

    ledger.enroll_legacy(
        "run-legacy",
        "qwen-token-plan",
        _RESET,
        public_key=legacy_public,
        actor="operator",
    )

    report = ledger.coverage("qwen")
    assert report.complete
    assert report.statuses == {"run-legacy": "trusted_legacy"}


def test_reanchor_requires_confirmation_and_records_quarantine(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-reanchor")
    ledger = _ledger(root)
    ledger.bind_run("run-reanchor")
    ledger.anchor_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="confirmation_required"):
        ledger.reanchor(actor="operator", operator_confirmation="")
    ledger.reanchor(actor="operator", operator_confirmation="ticket-42")

    ledger.verify_registry()
    record = json.loads(
        (root / "dispatch-registry-reanchors.jsonl").read_text(encoding="utf-8")
    )
    assert record["quarantined_entries"] == 1
    assert record["operator_confirmation"] == "ticket-42"


def test_reanchor_cannot_legitimize_a_modified_registry_entry(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-tampered")
    ledger = _ledger(root)
    ledger.bind_run("run-tampered")
    row = json.loads(ledger.registry_path.read_text(encoding="utf-8"))
    row["account"] = "attacker-account"
    ledger.registry_path.write_text(json.dumps(row), encoding="utf-8")

    with pytest.raises(RegistryRollbackError, match="registry_signature_invalid"):
        ledger.reanchor(actor="operator", operator_confirmation="ticket-43")


def test_accounting_snapshot_projects_into_fleet_backend(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    _sealed_run(root, "run-ui")
    ledger = _ledger(root)
    ledger.bind_run("run-ui")
    accounting = ledger.accounting_snapshot()

    snapshot = FleetProjector(
        root / "run-ui",
        accounting=accounting,
    ).snapshot()

    account = snapshot["accounting"]["accounts"]["qwen-token-plan"]
    assert account["providers"] == ["qwen", "deepseek"]
    assert account["used"] == 0
    assert account["reserved"] == 0
    assert account["coverage"]["coverage"] == "1/1"


def test_transport_failure_retains_persistent_reservation(tmp_path: Path) -> None:
    class FailingDispatcher:
        def dispatch(self, **_: object) -> NormalizedResponse:
            raise RuntimeError("transport outcome unknown")

    root = tmp_path / "evidence"
    chain = ReceiptChain(
        root,
        "run-live",
        FileRunKeyStore(root),
        profile_version="1.0.0",
        policy_version="3.1.3",
    )
    chain.append("run_attested", {"mode": "live"})
    ledger = PersistentEntitlementLedger(
        root,
        {
            "anthropic-plan": PlanWindow(
                "anthropic-plan",
                ("anthropic",),
                "plan_covered",
                0,
                10,
                _RESET,
            )
        },
        clock=lambda: datetime(2029, 1, 1, tzinfo=UTC).timestamp(),
    )
    ledger.bind_run("run-live")
    orchestrator = GovernedOrchestrator(
        FailingDispatcher(),
        entitlement_ledger=ledger,
    )

    with pytest.raises(OrchestrationBlocked, match="unexpected_stage_failure:g1d"):
        orchestrator.execute(
            goal="exercise uncertain transport",
            profile=load_registry().profiles["torq-v5-6-live"],
            mode=ExecutionMode.LIVE,
            chain=chain,
        )

    window = ledger.window("anthropic")
    assert window.used == 0
    assert window.reserved == 1
