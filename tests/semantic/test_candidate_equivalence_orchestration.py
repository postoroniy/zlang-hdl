"""Direct-SystemVerilog selected-candidate M36 orchestration tests."""

from __future__ import annotations

import hashlib
import json

import pytest

import zlang.candidate_equivalence as orchestration
from zlang.backend.manifest import BackendArtifact, MANIFEST_VERSION
from zlang.candidate_sites import (
    CandidateRankRecord,
    CandidateRewriteKind,
    CandidateSiteKind,
    CandidateSiteRecord,
    SelectedCandidateSite,
)
from zlang.formal_candidate import PreparedCandidateEquivalence
from zlang.formal_orchestration import CandidateEquivalencePlanReference
from zlang.ir import BitType, ComparisonWindow
from zlang.ir.equivalence import (
    BindingSide,
    EquivalenceBinding,
    EquivalenceProperty,
    EquivalenceRelation,
    SignalRole,
)


CANDIDATE = "selected:candidate"


def _property() -> EquivalenceProperty:
    return EquivalenceProperty(
        "m36.candidate", EquivalenceRelation.SAME_CYCLE_VALUE,
        "reference", CANDIDATE, BitType(), (), "port:result", "port:result",
        0, 0, 1, 1, None, None, None, None, 0,
        ComparisonWindow.same_cycle(),
    )


def _artifact(backend: str, module: str, side: BindingSide) -> BackendArtifact:
    text = f"module {module}(output result); assign result = 1'b0; endmodule\n"
    digest = hashlib.sha256(text.encode()).hexdigest()
    binding = EquivalenceBinding(
        MANIFEST_VERSION, side, "port:result", CANDIDATE, module, "result",
        1, "bit", SignalRole.OUTPUT, None, None, backend, digest,
    )
    return BackendArtifact(backend, module, CANDIDATE, digest, text, (binding,))


def _prepared() -> PreparedCandidateEquivalence:
    property_ = _property()
    reference = _artifact("semantic_reference", "Reference", BindingSide.REFERENCE)
    implementation = _artifact(
        "direct_systemverilog", "Implementation", BindingSide.IMPLEMENTATION
    )
    return PreparedCandidateEquivalence(
        property_, "module m36_candidate; endmodule\n", "m36_candidate",
        reference.artifact_hash, implementation.artifact_hash, "d" * 64,
        property_.id, "e" * 64, "f" * 64, "direct_systemverilog",
        reference, implementation, (),
    )


def _selected() -> SelectedCandidateSite:
    candidate = type("Candidate", (), {"implementation_identity": CANDIDATE})()
    rank = CandidateRankRecord(CANDIDATE, "semantic:candidate", 1, (0,))
    site = CandidateSiteRecord(
        CandidateSiteKind.SOURCE_EXPLORE, "owner", "y", "source:expression",
        CANDIDATE, (rank,), CandidateRewriteKind.OUTPUT_ASSIGNMENT,
    )
    return SelectedCandidateSite(site, candidate, object(), "m27")


def test_direct_candidate_replay_is_strict_deterministic_and_path_free() -> None:
    selected = _selected()
    prepared = _prepared()
    goal = orchestration._m36_plan(selected, prepared.property, prepared)
    plan = CandidateEquivalencePlanReference(
        selected.site.identity, CANDIDATE, goal, "port:result"
    )
    frozen = orchestration.FrozenCandidateEquivalenceSite(
        plan, prepared.property, prepared
    )
    data = frozen.to_data()
    assert orchestration.FrozenCandidateEquivalenceSite.from_data(data) == frozen
    rendered = json.dumps(data, sort_keys=True)
    assert "/tmp/" not in rendered
    assert "work_directory" not in rendered

    corrupted = json.loads(rendered)
    corrupted["direct_systemverilog"]["harness_hash"] = "0" * 64
    with pytest.raises(ValueError, match="replay identity"):
        orchestration.FrozenCandidateEquivalenceSite.from_data(corrupted)


def test_direct_replay_rejects_an_artifact_for_another_backend() -> None:
    selected = _selected()
    prepared = _prepared()
    wrong = PreparedCandidateEquivalence(
        prepared.property, prepared.source, prepared.top,
        prepared.reference_artifact_hash, prepared.implementation_artifact_hash,
        prepared.harness_hash, prepared.property_identity,
        prepared.assumptions_identity, prepared.backend_identity,
        "retired_backend", prepared.reference_artifact,
        prepared.implementation_artifact, prepared.input_semantic_ids,
        prepared.trace_metadata,
    )
    goal = orchestration._m36_plan(selected, prepared.property, prepared)
    plan = CandidateEquivalencePlanReference(
        selected.site.identity, CANDIDATE, goal, "port:result"
    )
    with pytest.raises(ValueError, match="direct-SystemVerilog"):
        orchestration.FrozenCandidateEquivalenceSite(plan, prepared.property, wrong)
