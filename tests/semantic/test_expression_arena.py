from __future__ import annotations

from dataclasses import replace

from zlang.ir import expressions as expr
from zlang.ir.expression_arena import (
    ExpressionArenaStatistics,
    SemanticExpressionArena,
)
from zlang.ir.types import UIntType
from zlang.opt import canonical_ir_identity, lower
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.source import SourceOrigin, SourceSpan


def _origin(line: int) -> SourceOrigin:
    return SourceOrigin(SourceSpan(line, 1, line, 2), "test expression")


def test_arena_interns_exact_typed_nodes_and_retains_all_origins() -> None:
    arena = SemanticExpressionArena()
    type8 = UIntType(8)
    type9 = UIntType(9)
    first = expr.Add(
        expr.InputRef("a", type8),
        expr.InputRef("b", type8),
        type9,
        origin=_origin(1),
    )
    second = expr.Add(
        expr.InputRef("a", type8),
        expr.InputRef("b", type8),
        type9,
        origin=_origin(2),
    )

    selected_first = arena.intern(first)
    selected_second = arena.intern(second)

    assert selected_first is selected_second
    assert arena.origins(selected_first) == (_origin(1), _origin(2))
    assert arena.statistics.hits >= 1


def test_arena_does_not_merge_call_occurrence_boundaries() -> None:
    arena = SemanticExpressionArena()
    type8 = UIntType(8)
    first = expr.Call("helper", (), type8, callee_identity="fixture:helper")
    second = expr.Call("helper", (), type8, callee_identity="fixture:helper")

    assert arena.intern(first) is not arena.intern(second)


def test_arena_provenance_includes_noninterned_barrier_nodes() -> None:
    arena = SemanticExpressionArena()
    type8 = UIntType(8)
    call = expr.Call(
        "helper",
        (),
        type8,
        callee_identity="fixture:helper",
        origin=_origin(7),
    )

    selected = arena.intern(call)

    assert arena.provenance_table.origins(selected) == (_origin(7),)


def test_arena_host_statistics_do_not_change_canonical_identity() -> None:
    module = analyze(parse("module Top{in x:u8 out y:u9 y=x+1}"))
    changed = replace(
        module,
        semantic_expression_arena_statistics=ExpressionArenaStatistics(
            requests=999,
            hits=998,
            unique_nodes=1,
            provenance_occurrences=999,
        ),
    )

    assert canonical_ir_identity(lower(module)) == canonical_ir_identity(
        lower(changed)
    )
