# SPDX-License-Identifier: Apache-2.0
"""Executed formal evidence, distinct from exact algebra certificates."""

from dataclasses import replace
import json
from pathlib import Path

import pytest
import z3

from tests.semantic.test_bilinear_intent import source, expanded_value
from zlang.compiler import compile_source
from zlang.formal_candidate import (
    SemanticEquivalenceDirectSystemVerilogCandidateVerifier,
)
from zlang.formal_exploration import FormalExplorationConfig, FormalPolicy
from zlang.ir import expressions as e
from zlang.ir.formal import FormalStatus
from zlang.opt.bilinear_polynomial import expression_term, read_bilinear
from zlang.opt.bilinear_search import explore_bilinear
from zlang.ir.joint_values import joint_value_boundary
from zlang.opt.rewrite_model import term_to_expression


def test_full_s16_integer_identity_and_every_finite_width_boundary():
    packed = joint_value_boundary(expanded_value(source(intent=False))).packed
    original = expression_term(packed)
    semantics = read_bilinear(original)
    variables = {item.name: z3.Int(item.name) for item in semantics.inputs}

    def integer(node):
        if isinstance(node, e.InputRef):
            return variables[node.name]
        if isinstance(node, e.Constant):
            return z3.IntVal(node.value)
        if isinstance(node, (e.Extend, e.Truncate, e.Bitcast)):
            return integer(node.expression)
        left, right = integer(node.left), integer(node.right)
        if isinstance(node, e.Add):
            return left + right
        return (
            left * right if node.operator is e.BinaryOperator.MULTIPLY else left - right
        )

    for alternative in explore_bilinear(original).alternatives:
        # This proves casts are lossless first; the Int theorem alone would
        # not establish finite-width equivalence.
        assert read_bilinear(alternative).signature == semantics.signature
        selected = term_to_expression(alternative)
        differences = [
            z3.simplify(integer(a) - integer(b), som=True) != 0
            for a, b in zip(packed.operands, selected.operands, strict=True)
        ]
        solver = z3.SolverFor("QF_NIA")
        solver.set(timeout=10_000)
        solver.add(z3.Or(*differences))
        assert solver.check() == z3.unsat


@pytest.mark.parametrize("policy", ("required_bmc", "required_proven"))
@pytest.mark.parametrize("width", (2, 16))
def test_real_joint_value_and_physical_miter(policy, width, tmp_path):
    compiled = compile_source(
        source(width),
        target="xc7z030ffg676-1",
        formal_policy=policy,
        formal_depth=6,
        formal_timeout=45,
        formal_max_candidates=1,
        formal_work_directory=tmp_path,
    )
    expected = (
        FormalStatus.BOUNDED_PASS if policy == "required_bmc" else FormalStatus.PROVEN
    )
    assert len(compiled.implementation_graph.resources) == 3
    assert compiled.physical_formal_records
    eligible = tuple(
        record for record in compiled.physical_formal_records if record.eligible
    )
    assert eligible and all(record.status is expected for record in eligible)
    if policy == "required_proven":
        assert all(
            not record.eligible
            for record in compiled.physical_formal_records
            if record.status is FormalStatus.BOUNDED_PASS
        )
    assert all(
        record.artifact_hash and record.harness_hash
        for record in compiled.physical_formal_records
    )
    for record in eligible:
        root = Path(record.work_directory) / "rtl_arithmetic"
        proof = json.loads((root / "proof.json").read_text())
        assert proof["assumptions_added"] == 0
        assert proof["widening_lemmas"]
        assert all(item["result"] == "unsat" for item in proof["widening_lemmas"])
        assert all(item["round_trip_result"] == "unsat" for item in proof["roots"])
        netlist = json.loads((root / "original.json").read_text())
        cells = next(iter(netlist["modules"].values()))["cells"]
        # The miter contains all four source products AND the actual three
        # bound DSP products. The reference is never swapped to the selected IR.
        assert sum(cell["type"] == "$mul" for cell in cells.values()) == 7


def test_mutated_selected_arithmetic_has_a_real_counterexample(tmp_path):
    compiled = compile_source(source(2), target="xc7z030ffg676-1")
    result = compiled.exploration_results[0]
    selected = result.selected
    value = selected.expression.expression
    swapped = replace(
        value,
        fields=(
            (value.fields[0][0], value.fields[1][1]),
            (value.fields[1][0], value.fields[0][1]),
        ),
    )
    mutated = replace(
        selected, expression=replace(selected.expression, expression=swapped)
    )
    verifier = SemanticEquivalenceDirectSystemVerilogCandidateVerifier(
        result.request.root,
        clock_domain_contract=compiled.ir.clock_domains[0],
    )
    evidence = verifier(
        mutated,
        FormalExplorationConfig(
            policy=FormalPolicy.REQUIRED_BMC,
            bmc_depth=6,
            timeout_seconds=45,
            work_directory=tmp_path,
        ),
    )
    assert evidence["status"] is FormalStatus.FAILED
    assert evidence["counterexample"] is not None


def test_combinational_joint_physical_bmc(tmp_path):
    text = (
        source(2)
        .replace("clock clk reset rst", "")
        .replace("latency == 1", "latency == 0")
    )
    compiled = compile_source(
        text,
        target="xc7z030ffg676-1",
        formal_policy="required_bmc",
        formal_depth=6,
        formal_timeout=45,
        formal_max_candidates=1,
        formal_work_directory=tmp_path,
    )
    assert compiled.implementation_graph.latency == 0
    assert len(compiled.implementation_graph.resources) == 3
    assert any(
        record.eligible and record.status is FormalStatus.BOUNDED_PASS
        for record in compiled.physical_formal_records
    )
