from __future__ import annotations

from zlang.ast.nodes import Assignment, PatternConstantExpr, PatternConstantKind
from zlang.parser import parse


def test_ordinary_width_patterns_retain_kind_and_width_expression() -> None:
    module = parse(
        "module Patterns<N=7> { "
        "out z:bits<7> out o:bits<8> "
        "z=zeros<N> o=ones<N+1> }"
    )
    assignments = {
        item.target: item.expression
        for item in module.assignments
        if isinstance(item, Assignment)
    }
    assert assignments["z"] == PatternConstantExpr(
        PatternConstantKind.ZEROS,
        "N",
        origin=assignments["z"].origin,
    )
    assert assignments["o"] == PatternConstantExpr(
        PatternConstantKind.ONES,
        "N+1",
        origin=assignments["o"].origin,
    )
    assert assignments["z"].origin is not None
    assert assignments["o"].origin is not None


def test_ordinary_width_patterns_accept_parentheses_and_intrinsics() -> None:
    module = parse(
        "module Patterns<N=8> { "
        "out z:bits<9> out o:bits<3> "
        "z=zeros<(N+1)> o=ones<floor_log2(N)> }"
    )
    assignments = {
        item.target: item.expression
        for item in module.assignments
        if isinstance(item, Assignment)
    }
    assert assignments["z"].witness == "(N+1)"
    assert assignments["o"].witness == "floor_log2(N)"


def test_equiv_pattern_constants_keep_frozen_singular_spelling() -> None:
    source = parse(
        "equiv bit_identity { x | zero<x> <=> x & ones<x> } "
        "module M { in x:u8 out y:u8 y=x }"
    )
    rule = source.equivalences[0]
    assert rule.left.right.kind is PatternConstantKind.ZERO
    assert rule.right.right.kind is PatternConstantKind.ONES
    assert rule.left.right.witness == "x"
    assert rule.right.right.witness == "x"
