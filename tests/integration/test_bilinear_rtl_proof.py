# SPDX-License-Identifier: Apache-2.0
"""Full-s16 actual-RTL equivalence and deliberately broken backend mutations."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest

from zlang.backend.manifest import publish_artifact
from zlang.backend.systemverilog.target import emit_target_artifact
from zlang.compiler import compile_file
from zlang.formal_candidate import (
    PhysicalTargetFormalCandidate,
    SemanticEquivalenceDirectSystemVerilogCandidateVerifier,
)
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.ir.formal import FormalStatus


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def witness():
    compiled = compile_file(
        ROOT / "examples/fft/complex_multiply_three_dsp.zhl",
        top="ComplexMultiply3DSP",
        target="xc7z030ffg676-1",
    )
    selected = compiled.exploration_results[0].selected
    candidate = PhysicalTargetFormalCandidate(
        compiled.ir.assignments[0].expression,
        compiled.ir,
        compiled.implementation_graph,
        selected.semantic_identity,
        compiled.implementation_graph.identity,
        selected.cost,
    )
    return compiled, candidate


def verifier_for(compiled):
    return SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        compiled.exploration_results[0].request.root,
        clock_domain_contract=compiled.ir.clock_domains[0],
    )


def test_full_width_required_proven_preserves_production_artifacts(witness, tmp_path):
    compiled, candidate = witness
    config = FormalExplorationConfig(
        policy=FormalPolicy.REQUIRED_PROVEN,
        bmc_depth=6,
        timeout_seconds=30,
        work_directory=tmp_path,
    )
    verifier = verifier_for(compiled)
    before = emit_target_artifact(compiled.ir, compiled.implementation_graph)
    evidence = verifier(candidate, config)
    after = emit_target_artifact(compiled.ir, compiled.implementation_graph)
    assert evidence["status"] is FormalStatus.PROVEN
    assert before == after
    root = Path(evidence["work_directory"]) / "rtl_arithmetic"
    proof = json.loads((root / "proof.json").read_text())
    assert (
        proof["source_hash"]
        == sha256(verifier.prepare(candidate, config).source.encode()).hexdigest()
    )
    assert proof["assumptions_added"] == 0
    # Policy v2 retains explicit carrier-composition obligations before Z3
    # simplifies extensions. All 19 saved obligations (15+4 round trips)
    # were independently replayed UNSAT before updating this exact snapshot.
    assert proof["schema"] == "zlang-rtl-arithmetic-normalization-v2"
    assert len(proof["roots"]) == 4 and len(proof["widening_lemmas"]) == 15
    assert proof["unchanged_state_and_controls_hash"]
    # Replay each query without the e-graph, compiler polynomial checker or IR.
    import subprocess

    for query in sorted(root.glob("*.smt2")):
        checked = subprocess.run(
            ["z3", str(query)], capture_output=True, text=True, timeout=10
        )
        assert checked.returncode == 0 and checked.stdout.strip() == "unsat", (
            checked.stdout + checked.stderr
        )


def test_preparation_and_queries_repeat_identically(witness, tmp_path):
    from zlang.formal_rtl_arithmetic import normalize_rtl_arithmetic
    import time

    compiled, candidate = witness
    bundle = verifier_for(compiled).prepare(candidate, FormalExplorationConfig())
    preparations = [
        normalize_rtl_arithmetic(
            bundle.source,
            top=bundle.top,
            work_directory=tmp_path / str(index),
            deadline=time.monotonic() + 30,
        )
        for index in range(2)
    ]
    assert preparations[0] == preparations[1]


@pytest.mark.parametrize(
    "mutation", ("arithmetic", "binding", "signed_width", "reset", "latency")
)
def test_full_width_backend_mutations_are_not_hidden(
    witness, tmp_path, monkeypatch, mutation
):
    import zlang.formal_candidate as owner

    compiled, candidate = witness

    def broken_backend(module, graph, **kwargs):
        original = emit_target_artifact(module, graph, **kwargs)
        text = original.text
        if mutation == "arithmetic":
            text, count = re.subn(
                r"assign product = joint_output;",
                "assign product = {joint_output[32:0], joint_output[65:33]};",
                text,
                count=1,
            )
        elif mutation == "binding":
            text, count = re.subn(
                r"(?m)^    \.B\(.+\), \.D\(25'sd0\), \.C\(48'sd0\),$",
                "    .B(18'sd0), .D(25'sd0), .C(48'sd0),",
                text,
                count=1,
            )
        elif mutation == "signed_width":
            text, count = re.subn(
                r"logic signed \[32:0\] product0;",
                "logic signed [15:0] product0;",
                text,
                count=1,
            )
        elif mutation == "reset":
            text, count = re.subn(
                r"if \(rst\) joint_output", "if (!rst) joint_output", text, count=1
            )
        else:
            text, count = re.subn(
                r"assign product = joint_output;",
                "logic [65:0] late_output;\n  always_ff @(posedge clk)\n"
                "    if (rst) late_output <= '0; else late_output <= joint_output;\n"
                "  assign product = late_output;",
                text,
                count=1,
            )
        assert count == 1 and text != original.text
        artifact = publish_artifact(
            module,
            text,
            backend=original.backend,
            selected_ir_identity=original.selected_ir_identity,
        )
        return replace(
            artifact,
            implementation=replace(
                original.implementation,
                implementation_artifact_hash=artifact.artifact_hash,
            ),
        )

    monkeypatch.setattr(owner, "emit_target_artifact", broken_backend)
    evidence = verifier_for(compiled)(
        candidate,
        FormalExplorationConfig(
            policy=FormalPolicy.REQUIRED_BMC,
            bmc_depth=6,
            timeout_seconds=30,
            work_directory=tmp_path,
        ),
    )
    assert evidence["status"] is FormalStatus.FAILED, evidence["reason"]
    assert evidence["counterexample"] is not None


def test_preparation_schema_invalidates_proof_identity(witness, monkeypatch):
    import zlang.formal_joint_value as preparation

    compiled, candidate = witness
    verifier = verifier_for(compiled)
    config = FormalExplorationConfig()
    before = verifier.cache_identity(candidate, config)
    monkeypatch.setattr(
        preparation,
        "RTL_ARITHMETIC_PROOF_SCHEMA",
        "deliberately-different-proof-policy",
    )
    after = verifier.cache_identity(candidate, config)
    assert before["artifact_hash"] == after["artifact_hash"]
    assert before["harness_hash"] == after["harness_hash"]
    assert before["backend_identity"] != after["backend_identity"]


def test_frozen_physical_bundle_replays_and_caches_without_ir(
    witness, tmp_path, monkeypatch
):
    import zlang.candidate_equivalence as execution
    from zlang.formal import FormalToolchainContext
    from zlang.formal_artifact_provider import FormalArtifactProvider
    from zlang.formal_candidate import (
        prepared_candidate_equivalence_to_data,
        prepared_candidate_equivalence_from_data,
    )
    from zlang.ir.equivalence import EquivalenceMode, EquivalenceStatus

    compiled, candidate = witness
    config = FormalExplorationConfig(
        policy=FormalPolicy.REQUIRED_PROVEN,
        bmc_depth=6,
        timeout_seconds=30,
        work_directory=tmp_path / "work",
    )
    prepared = verifier_for(compiled).prepare(candidate, config)
    frozen = prepared_candidate_equivalence_from_data(
        prepared_candidate_equivalence_to_data(prepared)
    )
    tools = FormalToolchainContext.discover(engine="sby", solver="z3")
    cache = tmp_path / "cache"
    result = execution._execute_semantic_equivalence(
        frozen, config, EquivalenceMode.PROVE, FormalArtifactProvider(cache), tools
    )
    assert result.status is EquivalenceStatus.PROVEN
    assert "solver-checked RTL arithmetic" in result.reason

    def unexpected_execution(*args, **kwargs):
        pytest.fail("frozen equivalence replay did not reuse its exact proof cache")

    monkeypatch.setattr(execution, "run_equivalence_formal", unexpected_execution)
    repeated = execution._execute_semantic_equivalence(
        frozen, config, EquivalenceMode.PROVE, FormalArtifactProvider(cache), tools
    )
    assert repeated == result
