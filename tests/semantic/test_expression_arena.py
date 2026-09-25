from __future__ import annotations

from dataclasses import replace
import importlib

import pytest

from zlang.ir import expressions as expr
from zlang.ir.expression_arena import (
    ExpressionArenaStatistics,
    SemanticExpressionArena,
)
from zlang.ir.types import UIntType
from zlang.ir.module import LocalValue
from zlang.opt import canonical_ir_identity, lower
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.semantic.analyze import (
    AnalysisServices,
    _expand_analysis_calls,
    _expand_immutable_locals,
)
from zlang.semantic import SemanticError
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


def test_immutable_local_analysis_preserves_shared_typed_subgraphs() -> None:
    type8 = UIntType(8)
    type9 = UIntType(9)
    type10 = UIntType(10)
    local = LocalValue("local", type9, expr.Add(
        expr.InputRef("x", type8), expr.Constant(1, type8), type9,
    ))
    branch = expr.Add(expr.InputRef("local", type9), expr.InputRef("local", type9), type10)
    root = expr.Add(branch, branch, UIntType(11))

    expanded = _expand_immutable_locals(root, {"local": local})
    assert isinstance(expanded, expr.Add)
    assert expanded.left is expanded.right
    assert isinstance(expanded.left, expr.Add)
    assert expanded.left.left is expanded.left.right


def test_call_free_analysis_keeps_original_typed_dag() -> None:
    shared = expr.Add(
        expr.InputRef("x", UIntType(8)), expr.Constant(1, UIntType(8)), UIntType(9),
    )
    root = expr.Add(shared, shared, UIntType(10))
    assert _expand_analysis_calls(root, object(), purpose="test") is root


def test_immutable_local_expansion_has_a_shared_analysis_work_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyzer = importlib.import_module("zlang.semantic.analyze")
    monkeypatch.setattr(analyzer, "_MAX_ANALYSIS_LOCAL_EXPANSION_NODES", 2)
    budget = AnalysisServices()
    expression = expr.Add(
        expr.InputRef("x", UIntType(8)),
        expr.InputRef("y", UIntType(8)),
        UIntType(9),
    )
    with pytest.raises(SemanticError, match="bounded immutable-local expansion") as failure:
        _expand_immutable_locals(expression, {}, work_budget=budget)
    assert failure.value.code == "ZL-IR-EXPANSION-LIMIT"
    assert budget.local_expansion_nodes == 3


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
