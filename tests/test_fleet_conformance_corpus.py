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
    corpus_digest,
    generate_authority_corpus,
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
