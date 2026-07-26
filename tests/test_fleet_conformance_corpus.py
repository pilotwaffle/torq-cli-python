from __future__ import annotations

from dataclasses import replace

import pytest

import torq_cli.testing.fleet_conformance as fleet_conformance
from torq_cli.domain.evidence_transitions import (
    TRANSITION_RULES,
    transition_authority_finding,
)
from torq_cli.domain.run_evidence import (
    validate_receipt_payload,
    validate_v2_receipt_contract,
)
from torq_cli.testing.fleet_conformance import (
    INVALID_MUTATOR_STAGES,
    UI_AXIS_VALUES,
    corpus_digest,
    generate_authority_corpus,
    generate_ui_corpus,
)


def test_generated_corpus_is_complete_and_every_fixture_conforms() -> None:
    corpus = generate_authority_corpus()
    fixtures = corpus["fixtures"]
    positives = [row for row in fixtures if row["mutation_stage"] is None]
    rule_ids = {
        ":".join((rule.writer_role, rule.transition, rule.decision_value or "-"))
        for rule in TRANSITION_RULES
    }

    assert corpus["completeness"] == {
        "source": "TRANSITION_RULES",
        "rule_count": len(TRANSITION_RULES),
        "positive_per_rule": 1,
        "mutation_stages": [
            "writer_role",
            "evidence_basis",
            "decision_value_if_applicable",
            "precondition",
        ],
    }
    assert {row["rule_id"] for row in positives} == rule_ids
    assert len(positives) == len(TRANSITION_RULES)
    for rule_id in rule_ids:
        stages = {
            row["mutation_stage"]
            for row in fixtures
            if row["rule_id"] == rule_id
        }
        assert {None, "writer_role", "evidence_basis", "precondition"} <= stages
        if rule_id.split(":", 2)[1] == "run_decision":
            assert "decision_value" in stages

    for fixture in fixtures:
        if fixture["mutation_stage"] is None:
            chain = fixture["receipt_chain"]
            target = chain[-1]
            assert target["writer_role"] == fixture["writer_role"]
            assert target["transition"] == fixture["transition"]
            assert target["evidence_basis"] == fixture["evidence_basis"]
            assert validate_receipt_payload(
                target["transition"],
                target["payload"],
                writer_role=target["writer_role"],
            ) is None
            assert validate_v2_receipt_contract(chain, sealed=False) is None
            continue
        if fixture["mutation_stage"] == "precondition":
            assert validate_v2_receipt_contract(
                fixture["receipt_chain"], sealed=False
            ) == fixture["expected_finding"]
            continue
        assert (
            transition_authority_finding(
                fixture["writer_role"],
                fixture["transition"],
                fixture["evidence_basis"],
                fixture["payload"],
            )
            == fixture["expected_finding"]
        )


def test_generated_corpus_is_byte_reproducible() -> None:
    first = generate_authority_corpus()
    second = generate_authority_corpus()
    assert first == second
    assert corpus_digest(first) == corpus_digest(second)
    assert corpus_digest(first) == (
        "sha256:439604fb93cb57ff98b8c5da6cf479395654d833a558e05beeec8f2726a4d27c"
    )


def test_unknown_precondition_fails_corpus_generation_closed(
    monkeypatch,
) -> None:
    mutated = (
        replace(TRANSITION_RULES[0], precondition="mutated_precondition"),
        *TRANSITION_RULES[1:],
    )
    monkeypatch.setattr(fleet_conformance, "TRANSITION_RULES", mutated)

    with pytest.raises(
        ValueError,
        match="precondition_fixture_missing:mutated_precondition",
    ):
        corpus_digest()


def test_ui_corpus_pairs_every_chain_with_a_whole_snapshot_envelope() -> None:
    corpus = generate_ui_corpus()
    chains = {
        row["fixture_id"]: row for row in corpus["chain_fixtures"]
    }
    snapshots = {
        row["fixture_id"]: row for row in corpus["snapshot_fixtures"]
    }

    assert chains.keys() == snapshots.keys()
    assert len(chains) > len(TRANSITION_RULES)
    for fixture_id, chain in chains.items():
        paired = snapshots[fixture_id]
        assert chain["axes"] == paired["axes"]
        assert chain["receipts"]
        assert chain["manifest"]["receipt_count"] == len(chain["receipts"])
        assert set(paired["envelope"]) == {
            "snapshot",
            "annotations",
            "session",
            "eligibility",
            "pending",
        }
        assert paired["envelope"]["snapshot"]["schema"] == (
            "torq-fleet-snapshot-v3"
        )


def test_ui_corpus_declares_and_covers_every_axis_and_invalid_mutator() -> None:
    corpus = generate_ui_corpus()
    chains = corpus["chain_fixtures"]
    declared = corpus["completeness"]

    assert declared["axes"] == {
        name: list(values) for name, values in UI_AXIS_VALUES.items()
    }
    assert declared["reachable_tuple_rule"] == (
        "every_declared_axis_value_covered"
    )
    assert declared["invalid_mutator_stages"] == list(INVALID_MUTATOR_STAGES)
    assert declared["pairing_key"] == "fixture_id"
    for name, values in UI_AXIS_VALUES.items():
        assert {row["axes"][name] for row in chains} == set(values)
    assert {
        row["mutator_stage"]
        for row in chains
        if row["mutator_stage"] != "none"
    } == set(INVALID_MUTATOR_STAGES)


def test_ui_annotation_overlays_are_deterministic_and_outside_snapshot() -> None:
    first = generate_ui_corpus()
    second = generate_ui_corpus()
    assert first == second
    for fixture in first["snapshot_fixtures"]:
        envelope = fixture["envelope"]
        assert "annotations" not in envelope["snapshot"]
        for annotation in envelope["annotations"]:
            assert annotation["observed_at"] == "2026-07-25T12:00:00Z"
            assert annotation["kind"] == fixture["axes"]["annotation_kind"]


def test_invalid_ui_fixtures_never_render_evidence_values_as_available() -> None:
    corpus = generate_ui_corpus()
    unsafe = {"tampered", "unreadable", "incomplete"}
    for fixture in corpus["snapshot_fixtures"]:
        envelope = fixture["envelope"]
        if fixture["axes"]["verification_state"] not in unsafe:
            continue
        snapshot = envelope["snapshot"]
        assert snapshot["data_status"] == "unavailable"
        assert snapshot["run"] is None
        assert snapshot["summary"] is None
        assert snapshot["lanes"] == []
        assert snapshot["actions"] == []
