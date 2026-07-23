from __future__ import annotations

import json
from pathlib import Path

import pytest

from torq_cli.core.conformance import assert_conformant, extracted_projection, mmh_reference_normalize


def _fixtures() -> list[dict]:
    return json.loads(Path("tests/fixtures/extraction/mmh_normalization.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", _fixtures())
def test_mmh_normalization_contract_is_conformant(fixture: dict) -> None:
    seat = {"provider": fixture["provider"], "model": fixture["model"]}
    assert_conformant(seat, fixture["response"])


def test_conformance_suite_detects_a_seeded_behavioral_divergence() -> None:
    fixture = _fixtures()[1]
    seat = {"provider": fixture["provider"], "model": fixture["model"]}
    original = mmh_reference_normalize(seat, fixture["response"])
    mutant = extracted_projection(seat, fixture["response"])
    mutant["visible_text"] = "<think>plan the diff first</think>Here is the patch."
    with pytest.raises(AssertionError):
        assert original == mutant


def test_conformance_is_deterministic_across_consecutive_runs() -> None:
    first = [extracted_projection({"provider": f["provider"], "model": f["model"]}, f["response"]) for f in _fixtures()]
    second = [extracted_projection({"provider": f["provider"], "model": f["model"]}, f["response"]) for f in _fixtures()]
    assert first == second

