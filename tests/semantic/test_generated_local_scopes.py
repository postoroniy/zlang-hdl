# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Capture-free structural generation and immutable declaration ownership."""

import pytest

from tests.support.hierarchy_observation_cases import GENERATED_LOCALS
from zlang.compilation_session import CompilationSession
from zlang.compiler import compile_source
from zlang.ir import expressions as ir_expr
from zlang.ir.traversal import walk_expression
from zlang.native_simulation import simulate
from zlang.semantic import SemanticError


def test_generated_aliases_bind_each_physical_child_to_its_own_element():
    result = compile_source(GENERATED_LOCALS, top="GeneratedLocals")
    assert [(b.instance, b.expression.index) for b in result.ir.instance_bindings] == [
        ("lane[0]", 0), ("lane[1]", 1),
    ]
    assert len({c.specialization_identity for c in result.ir.elaborated_instances}) == 1
    assert len({c.instance_identity for c in result.ir.elaborated_instances}) == 2


def test_generated_locals_have_distinct_identity_and_original_source_spans():
    module = CompilationSession(GENERATED_LOCALS, top="GeneratedLocals").semantic_ir
    chosen = [item for item in module.locals if "chosen" in item.name]
    assert len(chosen) == 2
    assert len({item.name for item in chosen}) == 2
    assert [item.expression.index for item in chosen] == [0, 1]
    assert chosen[0].expression.origin.span == chosen[1].expression.origin.span
    assert chosen[0].expression.origin.span.start_line == 11


def test_nested_generates_do_not_capture_each_others_locals():
    source = """module Leaf {in x:u8 out y:u8=x}
    module Top {in a:vec<2,u8> out y:vec<2,u8> leaf[2]:Leaf
      generate(i in 0..2) {
        chosen=a[i]
        generate(j in 0..1) {
          chosen=~a[i]
          leaf[i].x=chosen
        }
      }
      y=generate(i in 0..2) leaf[i].y
    }"""
    module = compile_source(source).ir
    assert [[node.index for node in walk_expression(b.expression)
             if isinstance(node, ir_expr.VectorIndex)]
            for b in module.instance_bindings] == [[0], [1]]


@pytest.mark.parametrize("declarations", (
    "x=a x=b", "x:u8=a x:u8=b", "x:u8=0 x:u8=1",
))
def test_duplicate_immutable_declarations_are_rejected_at_the_second_origin(declarations):
    with pytest.raises(SemanticError, match="immutable local 'x'.*more than once") as error:
        compile_source(f"module Top {{ in a,b:u8 out y:u8\n{declarations}\ny=x }}")
    assert error.value.primary.span.start_line == 2


def test_local_cannot_escape_its_generate_iteration():
    with pytest.raises(SemanticError):
        compile_source("module Top {in a:u8 out y:u8 generate(i in 0..1){ x=a } y=x}")


def test_parent_local_is_preserved_when_a_generate_has_the_same_local_name():
    source = GENERATED_LOCALS.replace(
        "lane[2]:EchoLane", "chosen:u8=77\n lane[2]:EchoLane\n out parent:u8=chosen",
    )
    result = compile_source(source)
    parent = next(a for a in result.ir.assignments if a.target.name == "parent")
    assert isinstance(parent.expression, ir_expr.Constant) and parent.expression.value == 77


def test_empty_and_single_iteration_have_no_phantom_bindings():
    for count in (0, 1):
        source = f"module Top {{ in a:u8 out y:u8=a generate(i in 0..{count}) {{ x=a }} }}"
        module = CompilationSession(source).semantic_ir
        assert len(module.locals) == count


def test_generated_local_names_avoid_user_identifier_collisions():
    source = GENERATED_LOCALS.replace(
        "lane[2]:EchoLane", "__generate_0_0_chosen:u8=55\n lane[2]:EchoLane",
    )
    module = CompilationSession(source).semantic_ir
    assert len({item.name for item in module.locals}) == 3


def test_changed_generated_local_source_does_not_reuse_the_old_selected_identity():
    first = compile_source(GENERATED_LOCALS)
    changed = GENERATED_LOCALS.replace("chosen=samples[k]", "chosen=~samples[k]")
    second = compile_source(changed)
    assert first.selected_ir_identity != second.selected_ir_identity
    assert first.selected_ir_identity == compile_source(GENERATED_LOCALS).selected_ir_identity


def test_functional_binder_inside_a_generate_keeps_its_own_scope():
    source = GENERATED_LOCALS.replace(
        "lane[k].sample=chosen", "lane[k].sample=extend<8>(sum(chosen in 0..2) chosen)",
    )
    result = compile_source(source)
    for binding in result.ir.instance_bindings:
        nodes = tuple(walk_expression(binding.expression))
        assert not any(isinstance(node, ir_expr.InputRef) for node in nodes)
    assert simulate(result.ir, samples=[3, 201]) == {"results": [1, 1]}


def test_struct_field_puns_capture_the_iteration_local_without_renaming_the_field():
    source = "struct Choice {chosen:u8}\n" + GENERATED_LOCALS
    source = (source.replace("in sample:u8", "in sample:Choice")
              .replace("out result:u8", "out result:Choice")
              .replace("out results:vec<2,u8>", "out results:vec<2,Choice>")
              .replace("lane[k].sample=chosen", "lane[k].sample=Choice {chosen}"))
    result = compile_source(source)
    assert [b.expression.fields[0][0] for b in result.ir.instance_bindings] == ["chosen", "chosen"]
    assert [b.expression.fields[0][1].index for b in result.ir.instance_bindings] == [0, 1]


def test_local_constant_generic_actuals_are_rebound_in_each_iteration():
    source = "fn helper<K:u8>() -> u8 { K }\n" + GENERATED_LOCALS
    source = (source.replace("chosen=samples[k]", "chosen=extend<8>(k+5)")
              .replace("lane[k].sample=chosen", "lane[k].sample=helper<K=chosen>()"))
    result = compile_source(source)
    assert simulate(result.ir, samples=[3, 201]) == {"results": [5, 6]}


def test_functional_index_can_shadow_the_outer_structural_index():
    source = GENERATED_LOCALS.replace(
        "lane[k].sample=chosen", "lane[k].sample=extend<8>(sum(k in 0..2) k)",
    )
    result = compile_source(source)
    assert simulate(result.ir, samples=[3, 201]) == {"results": [1, 1]}


def test_generated_local_cannot_hide_a_compile_time_parameter():
    source = GENERATED_LOCALS.replace("module GeneratedLocals {", "module GeneratedLocals<chosen=1> {")
    with pytest.raises(SemanticError, match="conflicts with a compile-time parameter"):
        compile_source(source)


def test_ordinary_value_generic_does_not_start_accepting_immutable_local_arguments():
    source = "fn helper<K>() -> u8 { K }\n" + GENERATED_LOCALS
    source = (source.replace("chosen=samples[k]", "chosen=k+5")
              .replace("lane[k].sample=chosen", "lane[k].sample=helper<K=chosen>()"))
    with pytest.raises(SemanticError, match="requires an integer"):
        compile_source(source)


def test_type_generic_actual_is_not_captured_by_an_equal_value_name():
    source = "type Selected = u8\nfn identity<type T>(x:T) -> T {x}\n" + GENERATED_LOCALS
    source = (source.replace("chosen=samples[k]", "Selected=samples[k]")
              .replace("lane[k].sample=chosen", "lane[k].sample=identity<T=Selected>(Selected)"))
    result = compile_source(source)
    assert simulate(result.ir, samples=[3, 201]) == {"results": [3, 201]}
