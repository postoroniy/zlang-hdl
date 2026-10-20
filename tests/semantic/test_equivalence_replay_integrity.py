"""Retained formal source must match the typed miter before cache lookup."""

from dataclasses import replace

import pytest

from tests.semantic.test_candidate_equivalence_orchestration import _prepared
from zlang.candidate_equivalence import _semantic_equivalence_execution_recipe
from zlang.equivalence import artifact_hash
from zlang.formal import FormalToolchainContext
from zlang.formal_candidate import (
    prepared_candidate_equivalence_from_data,
    prepared_candidate_equivalence_to_data,
)
from zlang.formal_exploration import FormalExplorationConfig
from zlang.ir.equivalence import EquivalenceMode


@pytest.mark.parametrize(
    "mutation", ("assertion", "source", "top", "harness_hash", "inputs", "trace", "schema")
)
def test_replay_rejects_modified_source_contract(mutation):
    original = _prepared()
    data = prepared_candidate_equivalence_to_data(original)
    assert prepared_candidate_equivalence_from_data(data) == original
    if mutation == "assertion":
        assert "assert" in data["source"]
        data["source"] = data["source"].replace("assert", "assume")
    elif mutation == "source":
        data["source"] += "\n// changed proof source\n"
    elif mutation == "inputs":
        data["input_semantic_ids"] = ["port:not_an_input"]
    elif mutation == "trace":
        data["trace_metadata"]["reference_output"] = "not_the_checker"
    else:
        data[mutation] = "wrong"
    with pytest.raises(ValueError):
        prepared_candidate_equivalence_from_data(data)


@pytest.mark.parametrize("field", ("source", "top", "reference_artifact_hash", "implementation_artifact_hash"))
def test_execution_recipe_checks_in_memory_integrity(field):
    prepared = _prepared()
    tools = FormalToolchainContext.discover(engine="sby", solver="z3")
    config = FormalExplorationConfig()
    recipe = _semantic_equivalence_execution_recipe(prepared, config, EquivalenceMode.PROVE, tools)
    assert recipe["schema"] == "zlang-candidate-semantic_equivalence-execution-v4"
    assert recipe["equivalence_preparation"] == "zlang-equivalence-flattened-preparation-v1"
    assert recipe["source_hash"] == artifact_hash(prepared.source)
    assert recipe["top"] == prepared.top
    altered = prepared.source.replace("assert", "assume") if field == "source" else "wrong"
    with pytest.raises(ValueError, match="replay identity"):
        _semantic_equivalence_execution_recipe(replace(prepared, **{field: altered}), config, EquivalenceMode.PROVE, tools)
