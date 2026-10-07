from __future__ import annotations

from zlang.semantic.analyze import _ExpressionContext


def test_expression_context_derivation_shares_only_declared_owners() -> None:
    context = _ExpressionContext({}, allow_delay=False)

    lexical = context.derive(source_unit="fixture.module")
    configured = context.derive(clock_domains=("clk",))
    isolated = context.derive(callable_definitions={})

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
    first = _ExpressionContext({}, allow_delay=False)
    second = _ExpressionContext({}, allow_delay=False)

    assert first.services is not second.services
    assert first.expression_arena is not second.expression_arena
    first.callable_definitions["fixture"] = object()  # type: ignore[assignment]
    assert "fixture" not in second.callable_definitions
