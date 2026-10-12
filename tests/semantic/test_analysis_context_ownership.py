from __future__ import annotations

from zlang.semantic.context import (
    AnalysisEnvironment,
    AnalysisServices,
    ExpressionContext,
    ExpressionScope,
)


def _context() -> ExpressionContext:
    return ExpressionContext(
        AnalysisEnvironment(functions={}),
        AnalysisServices(),
        ExpressionScope(allow_delay=False),
    )


def test_expression_context_derivation_shares_only_declared_owners() -> None:
    context = _context()

    lexical = context.with_scope(source_unit="fixture.module")
    configured = context.with_environment(clock_domains=("clk",))
    isolated = context.with_services(callables=context.services.callables.fork())

    assert lexical.environment is context.environment
    assert lexical.services is context.services
    assert lexical.scope is not context.scope

    assert configured.environment is not context.environment
    assert configured.services is context.services
    assert configured.scope is context.scope

    assert isolated.environment is context.environment
    assert isolated.services is not context.services
    assert isolated.scope is context.scope


def test_top_level_expression_contexts_never_share_services() -> None:
    first = _context()
    second = _context()

    assert first.services is not second.services
    assert first.services.expression_arena is not second.services.expression_arena
    first.services.callables.callable_definitions["fixture"] = object()  # type: ignore[assignment]
    assert "fixture" not in second.services.callables.callable_definitions
