"""Exact II=1 generic-logic multiplier candidate coverage."""

from __future__ import annotations

from dataclasses import replace

import pytest

from zlang.backend.systemverilog.emitter import emit
from zlang.compiler import compile_source
from zlang.equivalence import (
    emit_miter_with_metadata,
    emit_reference_model,
    make_equivalence_property,
    run_equivalence_formal,
)
from zlang.formal_exploration import FormalPolicy
from zlang.ir.equivalence import (
    BindingMap,
    BindingSide,
    EquivalenceBinding,
    EquivalenceStatus,
    SignalRole,
)
from zlang.native_simulation import simulate
from zlang.structural_multiplier import csa_multiplier_candidate


def _module_with_csa(type_name: str):
    output = "u8" if type_name == "u4" else "s8"
    compiled = compile_source(
        f"module Mul {{ in a:{type_name} in b:{type_name} out y:{output} y=a*b }}"
    )
    candidate = csa_multiplier_candidate(compiled.ir.assignments[0].expression)
    assert candidate is not None
    return compiled.ir, replace(
        compiled.ir,
        assignments=(replace(compiled.ir.assignments[0], expression=candidate.expression),),
    ), candidate


@pytest.mark.parametrize(
    ("type_name", "values"),
    (
        ("u4", range(16)),
        ("s4", range(-8, 8)),
    ),
)
def test_csa_candidate_is_exact_for_small_signed_and_unsigned_products(
    type_name: str, values: range,
) -> None:
    native, structural, candidate = _module_with_csa(type_name)
    assert candidate.strategy == "multiply:csa_tree"
    assert candidate.partial_products == 4
    for left in values:
        for right in values:
            assert simulate(structural, a=left, b=right) == simulate(
                native, a=left, b=right
            )


def test_csa_candidate_renders_without_native_multiply_operator() -> None:
    _native, structural, _candidate = _module_with_csa("u4")
    rtl = emit(structural)
    assert " * " not in rtl
    assert " << " in rtl
    assert " & " in rtl
    assert " ^ " in rtl


def test_implement_exposes_one_bounded_structural_multiply_candidate() -> None:
    result = compile_source(
        "module M { in a:u4 in b:u4 out y:u8 "
        "y=implement { a*b intent { minimize lut } } }"
    )
    candidates = result.exploration_results[0].generated_candidates
    structural = tuple(
        item for item in candidates if "multiply:csa_tree" in item.stages
    )
    assert len(structural) == 1
    assert structural[0].cost.ii.value == 1
    assert structural[0].cost.dsp.value == 0


def test_structural_candidate_enters_the_existing_formal_equivalence_gate() -> None:
    result = compile_source(
        "module M { clock clk reset rst in a:u4 in b:u4 out y:u8 "
        "y=implement { a*b intent { minimize lut } } }",
        formal_policy=FormalPolicy.AVAILABLE,
    )
    exploration = result.exploration_results[0]
    identities = {item.implementation_identity for item in exploration.generated_candidates}
    assert {item.candidate_identity for item in exploration.formal_records} == identities


def test_structural_candidate_is_formally_equivalent_when_tools_are_available() -> None:
    native, structural, _candidate = _module_with_csa("u4")
    reference = native.assignments[0].expression
    implementation = structural.assignments[0].expression
    property_ = make_equivalence_property(
        reference,
        implementation,
        candidate_class="architecture_alternatives",
        reference_root="native-multiply",
        implementation_root="csa-multiply",
        inputs=("a", "b"),
        reference_output="y",
        implementation_output="y",
    )
    inputs = tuple((port.name, port.type) for port in native.ports[:2])

    def bindings(side: BindingSide, module_name: str, digest: str) -> tuple[EquivalenceBinding, ...]:
        return tuple(
            EquivalenceBinding(
                2,
                side,
                name,
                "csa-multiplier-proof",
                module_name,
                name,
                type_.width,
                "unsigned",
                SignalRole.INPUT,
                None,
                None,
                "direct_systemverilog",
                digest,
            )
            for name, type_ in inputs
        ) + (
            EquivalenceBinding(
                2,
                side,
                "y",
                "csa-multiplier-proof",
                module_name,
                "y",
                reference.type.width,
                "unsigned",
                SignalRole.OUTPUT,
                None,
                None,
                "direct_systemverilog",
                digest,
            ),
        )

    miter = emit_miter_with_metadata(
        property_,
        BindingMap((
            *bindings(BindingSide.REFERENCE, "MulReference", "reference"),
            *bindings(BindingSide.IMPLEMENTATION, "MulCsa", "implementation"),
        )),
        reference_module="MulReference",
        implementation_module="MulCsa",
    )
    source = "\n".join((
        emit_reference_model("MulReference", "y", reference.type, inputs, reference),
        emit_reference_model("MulCsa", "y", implementation.type, inputs, implementation),
        miter.source,
    ))
    result = run_equivalence_formal(
        property_,
        source,
        top=f"semantic_equivalence_{property_.id.replace('.', '_')}",
        depth=1,
        timeout_seconds=30,
        trace_metadata=miter.trace_metadata,
    )
    if result.status is not EquivalenceStatus.SKIPPED:
        assert result.status is EquivalenceStatus.BOUNDED_PASS


def test_structural_candidate_fails_closed_for_wide_products() -> None:
    wide = compile_source("module Wide { in a:u17 in b:u17 out y:u34 y=a*b }")
    assert csa_multiplier_candidate(wide.ir.assignments[0].expression) is None
