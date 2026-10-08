from __future__ import annotations

from dataclasses import fields
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.semantic.analyze import SemanticAnalyzer
from zlang.semantic.callables import CallableSpecializationCache
from zlang.semantic.context import (
    AnalysisContext,
    AnalysisEnvironment,
    AnalysisServices,
    ExpressionContext,
    ExpressionScope,
    SourceAnalysisContext,
)


def _expression_context() -> ExpressionContext:
    return ExpressionContext(
        AnalysisEnvironment({}),
        AnalysisServices(),
        ExpressionScope(allow_delay=False),
    )


def test_expression_context_derivation_shares_only_declared_owners() -> None:
    context = _expression_context()

    lexical = context.with_scope(source_unit="fixture.module")
    configured = context.with_environment(clock_domains=("clk",))
    isolated = context.with_services(callables=CallableSpecializationCache())

    assert lexical.environment is context.environment
    assert lexical.services is context.services
    assert lexical.scope is not context.scope

    assert configured.environment is not context.environment
    assert configured.services is context.services
    assert configured.scope is context.scope

    assert isolated.environment is context.environment
    assert isolated.services is not context.services
    assert isolated.scope is context.scope
    assert isolated.services.callables is not context.services.callables
    assert isolated.services.callables.callable_definitions == {}


def test_top_level_expression_contexts_never_share_services() -> None:
    first = _expression_context()
    second = _expression_context()

    assert first.services is not second.services
    assert first.services.expression_arena is not second.services.expression_arena
    first.services.callables.callable_definitions["fixture"] = object()  # type: ignore[assignment]
    assert "fixture" not in second.services.callables.callable_definitions


def test_callable_specialization_cache_owns_snapshot_and_use_state() -> None:
    cache = CallableSpecializationCache()
    cache.record_use("callee")
    cache.specializations_in_progress.add("pending")
    cache.specialization_budget_costs["callee"] = (3, 5)
    snapshot = cache.snapshot()

    cache.record_use("callee")
    cache.specializations_in_progress.clear()
    cache.specialization_budget_costs.clear()
    cache.restore(snapshot)

    assert cache.callable_use_counts == {"callee": 1}
    assert cache.specializations_in_progress == {"pending"}
    assert cache.specialization_budget_costs == {"callee": (3, 5)}


def test_analysis_context_composes_bounded_subsystem_owners() -> None:
    syntax = parse("module Add { in a:u8 in b:u8 out y:u9 y=a+b }")

    first = AnalysisContext(source=SourceAnalysisContext(syntax))
    second = AnalysisContext(source=SourceAnalysisContext(syntax))

    assert tuple(item.name for item in fields(AnalysisContext)) == (
        "source",
        "resolution",
        "hierarchy",
        "specialization",
        "compile_time",
        "implementation",
        "verification",
        "tooling",
    )
    assert first.resolution is not second.resolution
    assert first.hierarchy is not second.hierarchy
    assert first.tooling is not second.tooling


def test_public_analyze_wrapper_matches_semantic_analyzer_facade() -> None:
    syntax = parse("module Add { in a:u8 in b:u8 out y:u9 y=a+b }")

    public_result = analyze(syntax)
    facade_result = SemanticAnalyzer().analyze(
        AnalysisContext(source=SourceAnalysisContext(syntax))
    )

    assert facade_result == public_result
    assert public_result.assignments[0].expression.origin is not None
