# SPDX-License-Identifier: Apache-2.0
"""Type-independent bounded value discovery, admission and intent selection."""

from dataclasses import replace
from itertools import product

import pytest

from zlang.compiler import compile_source
from zlang.intent_structural_exploration import (
    IntentStructuralExplorationCache,
    explore_intent_structures,
)
from zlang.ir import expressions as e
from zlang.ir.callables import expand_callable_calls
from zlang.ir.expression_graph import ExpressionDagIndex
from zlang.ir.types import SIntType
from zlang.opt.bilinear_polynomial import expression_term, read_bilinear
from zlang.opt.bilinear_search import BilinearSearchLimits, explore_bilinear
from zlang.ir.joint_values import joint_value_boundary
from zlang.opt.rewrite_model import term_to_expression
from zlang.opt.rewrite_spec import RewriteRule, TypedRewriteSpec
from zlang.opt.saturation import saturate
from zlang.opt.module_lowering import lower
from zlang.opt.value_certificate import (
    checked_value_certificate,
    verify_checked_value_certificate,
)
from zlang.semantic.errors import SemanticError


def source(width=16, *, budget="<= 3", intent=True, renamed=False, factored=False):
    fields = ("first", "second") if renamed else ("re", "im")
    left, right = fields
    type_name = "UserPayload" if renamed else "UserComplex"
    operator = "+" if factored else "*"
    real = (
        f"a.{left}*b.{left} + a.{right}*b.{left}"
        if factored
        else f"a.{left}*b.{left} - a.{right}*b.{right}"
    )
    imag = (
        f"a.{left}*b.{right} + a.{right}*b.{right}"
        if factored
        else f"a.{left}*b.{right} + a.{right}*b.{left}"
    )
    value = f"a {operator} b"
    if intent:
        value = f"implement {{ {value} intent {{ dsp {budget} latency == 1 ii == 1 minimize lut }} }}"
    return f"""
struct {type_name}<type T> {{ {left}:T {right}:T }}
operator {operator}<type A,type B>(a:{type_name}<A>, b:{type_name}<B>) {{
    {type_name} {{ {left} = {real} {right} = {imag} }}
}}
module BilinearWitness {{ clock clk reset rst
    in a,b:{type_name}<s{width}> out y:{type_name}<s{2 * width + 1}>
    y = {value}
}}
"""


def expanded_value(text):
    module = compile_source(text).ir
    return expand_callable_calls(
        module.assignments[0].expression,
        (*module.functions, *module.callable_definitions),
    )


def multiply_count(value):
    return sum(
        isinstance(item, e.Binary) and item.operator is e.BinaryOperator.MULTIPLY
        for item in ExpressionDagIndex((value,))
    )


@pytest.mark.parametrize(
    ("renamed", "factored", "budget", "count"),
    (
        (False, False, "<= 3", 3),
        (True, False, "<= 3", 3),
        (True, True, "<= 2", 2),
        (False, False, "== 4", 4),
    ),
)
def test_overloads_and_other_arithmetic_need_no_nominal_rewrite(
    renamed, factored, budget, count
):
    result = compile_source(
        source(renamed=renamed, factored=factored, budget=budget),
        target="xc7z030ffg676-1",
    )
    selected = result.exploration_results[0].selected
    assert selected.cost.dsp.value == count
    assert selected.cost.latency.value == selected.cost.ii.value == 1
    assert len(result.implementation_graph.resources) == count
    assert not result.implementation_graph.is_generic
    assert selected.expression.type.fields[0].type == SIntType(33)
    if count < 4:
        assert "egglog candidate" in selected.provenance
        assert (
            selected.selected_value_identity
            != result.exploration_results[0].source_semantic_identity
        )


def test_search_is_bounded_and_not_a_hardcoded_formula():
    value = expanded_value(source(intent=False))
    term = expression_term(joint_value_boundary(value).packed)
    bounded = explore_bilinear(term, BilinearSearchLimits(max_basis_tests=1))
    assert bounded.basis_tests == 1 and bounded.truncated
    assert not bounded.alternatives
    found = explore_bilinear(term)
    assert 1 <= found.basis_tests <= 4096
    assert 1 <= len(found.alternatives) <= 8
    assert all(
        multiply_count(term_to_expression(item)) == 3 for item in found.alternatives
    )
    assert all(
        read_bilinear(item).signature == read_bilinear(term).signature
        for item in found.alternatives
    )


def test_egglog_extracts_three_products_only_with_general_bilinear_exploration():
    value = expanded_value(source(intent=False))
    boundary = joint_value_boundary(value)
    # Use the same compiler-owned scalar adapter as the production site.
    from zlang.ir.module import Assignment, Module, Port, PortDirection

    inputs = tuple(
        Port(PortDirection.INPUT, name, item.type) for name, item in boundary.bindings
    )
    output = Port(PortDirection.OUTPUT, "result", boundary.packed.type)
    canonical = lower(
        Module("Proof", (*inputs, output), (Assignment(output, boundary.packed),))
    )
    root = canonical.assignments[0].expression
    local = saturate(canonical, root, include_commutative_aliases=False)
    assert all(
        multiply_count(term_to_expression(item)) >= 4 for item in local.alternatives
    )
    searched = saturate(
        canonical, root, include_commutative_aliases=False, include_bilinear=True
    )
    assert RewriteRule.BILINEAR_FACTOR in searched.rules
    assert any(
        multiply_count(term_to_expression(item)) == 3 for item in searched.alternatives
    )
    assert len(searched.certificates) == len(searched.alternatives)


def test_certificate_rejects_wrong_math_and_premature_narrowing():
    term = expression_term(
        joint_value_boundary(expanded_value(source(intent=False))).packed
    )
    candidate = explore_bilinear(term).alternatives[0]
    spec = TypedRewriteSpec.builtin(
        "test.bilinear", RewriteRule.BILINEAR_FACTOR, ("bilinear", None)
    )
    certificate = checked_value_certificate(term, candidate, (spec,), ())
    verify_checked_value_certificate(certificate, term, candidate, (spec,), ())
    changed = replace(candidate, operands=tuple(reversed(candidate.operands)))
    with pytest.raises(ValueError):
        checked_value_certificate(term, changed, (spec,), ())
    packed = term_to_expression(candidate)
    component = packed.operands[0]
    damaged = replace(
        packed,
        operands=(
            replace(
                component,
                expression=e.Extend(
                    e.Truncate(component.expression, SIntType(8)),
                    component.expression.type,
                ),
            ),
            *packed.operands[1:],
        ),
    )
    with pytest.raises(ValueError, match="loses finite-width"):
        read_bilinear(expression_term(damaged))


def test_structural_cache_replays_joint_certificates_without_search(monkeypatch):
    cache = IntentStructuralExplorationCache()
    value = expanded_value(source(intent=False))

    def run():
        return explore_intent_structures(
            value,
            (),
            max_saturation_iterations=6,
            max_eclasses=4096,
            max_enodes=16384,
            max_raw_extractions=16,
            max_structural_alternatives=8,
            cache=cache,
        )

    first = run()

    def unexpected_search(*args, **kwargs):
        pytest.fail("cache replay must check the certificate, not repeat value search")

    monkeypatch.setattr("zlang.opt.bilinear_search.explore_bilinear", unexpected_search)
    second = run()
    assert tuple(item.selected_value_identity for item in first.alternatives) == tuple(
        item.selected_value_identity for item in second.alternatives
    )
    assert cache.info().hits == 1
    assert multiply_count(second.alternatives[0].expression) == 4
    assert any(multiply_count(item.expression) == 3 for item in second.alternatives[1:])


def test_joint_boundary_does_not_hide_storage_as_a_pure_input():
    value = expanded_value(source(intent=False))
    boundary = joint_value_boundary(value)
    projection = boundary.bindings[0][1]
    from zlang.ir.joint_values import map_joint_value

    state_value = map_joint_value(
        value,
        lambda item: replace(
            projection, expression=e.RegisterRef("state", projection.expression.type)
        )
        if item == projection
        else None,
    )
    assert joint_value_boundary(state_value) is None


def test_bilinear_checker_version_does_not_change_scalar_certificates():
    from zlang.opt.value_certificate import CHECKER_VERSION, BILINEAR_CHECKER_VERSION

    assert CHECKER_VERSION == "exact-local-value-v1"
    term = expression_term(
        joint_value_boundary(expanded_value(source(intent=False))).packed
    )
    spec = TypedRewriteSpec.builtin(
        "test.bilinear", RewriteRule.BILINEAR_FACTOR, ("bilinear", None)
    )
    checked = checked_value_certificate(
        term, explore_bilinear(term).alternatives[0], (spec,), ()
    )
    assert checked.checker_version == BILINEAR_CHECKER_VERSION


def test_impossible_budget_does_not_use_unknown_as_zero():
    with pytest.raises(SemanticError, match="no implementation satisfies"):
        compile_source(source(budget="<= 2"), target="xc7z030ffg676-1")


def test_minimal_combinational_intent_and_mixed_signed_widths():
    result = compile_source(
        """
        import std.math.complex
        module Mixed { in a:Complex<s16> in b:Complex<s12> out y:Complex<s29>
            y = implement { a*b intent { dsp <= 3 } }
        }
    """,
        target="xc7z030ffg676-1",
    )
    assert len(result.implementation_graph.resources) == 3
    assert result.implementation_graph.latency == 0
    assert result.implementation_graph.initiation_interval == 1
    assert result.ir.outputs[0].type.fields[0].type == SIntType(29)


def test_source_identity_survives_structure_selection():
    three = compile_source(source(budget="<= 3"))
    four = compile_source(source(budget="== 4"))
    a, b = three.exploration_results[0], four.exploration_results[0]
    assert a.source_semantic_identity == b.source_semantic_identity
    assert (
        a.selected.semantic_identity
        == b.selected.semantic_identity
        == a.source_semantic_identity
    )
    assert a.selected.selected_value_identity != b.selected.selected_value_identity
    assert a.selected.implementation_identity != b.selected.implementation_identity


@pytest.mark.parametrize("width", (1, 2))
def test_small_width_native_results_are_exhaustive(width, tmp_path):
    from zlang import sim

    path = tmp_path / "bilinear.zhl"
    path.write_text(source(width))
    program = sim.compile(path)
    low, high = -(1 << (width - 1)), 1 << (width - 1)
    with program.create() as dut:
        dut.reset("rst", asserted=True)
        dut.edge("clk")
        dut.reset("rst", asserted=False)
        for ar, ai, br, bi in product(range(low, high), repeat=4):
            dut.set("a", {"re": ar, "im": ai})
            dut.set("b", {"re": br, "im": bi})
            assert dut.edge("clk")["y"] == {
                "re": ar * br - ai * bi,
                "im": ar * bi + ai * br,
            }
