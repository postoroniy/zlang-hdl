"""Shared recursion for compiler-side simulation expression erasure."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace

from zlang.ir import expressions as expr


class SimulationExpressionRewriter:
    """Memoized immutable expression/dataclass rewriter with one special hook."""

    def __init__(self) -> None:
        self._expression_memo: dict[
            int,
            tuple[expr.Expression, expr.Expression],
        ] = {}
        self._value_memo: dict[int, tuple[object, object]] = {}

    def expression(self, value: expr.Expression) -> expr.Expression:
        cached = self._expression_memo.get(id(value))
        if cached is not None and cached[0] is value:
            return cached[1]
        result = self.rewrite_special(value)
        if result is None:
            updates: dict[str, object] = {}
            for descriptor in fields(value):
                if not descriptor.init or descriptor.name in {
                    "origin",
                    "source_origin",
                }:
                    continue
                current = getattr(value, descriptor.name)
                rewritten = self.value(current)
                if rewritten is not current:
                    updates[descriptor.name] = rewritten
            result = replace(value, **updates) if updates else value
        self._expression_memo[id(value)] = (value, result)
        return result

    def value(self, value: object) -> object:
        if isinstance(value, expr.Expression):
            return self.expression(value)
        cacheable = isinstance(value, (tuple, list, dict)) or (
            is_dataclass(value) and not isinstance(value, type)
        )
        if cacheable:
            cached = self._value_memo.get(id(value))
            if cached is not None and cached[0] is value:
                return cached[1]
        if isinstance(value, tuple):
            rewritten = tuple(self.value(item) for item in value)
            result: object = (
                value
                if all(a is b for a, b in zip(value, rewritten, strict=True))
                else rewritten
            )
        elif isinstance(value, list):
            rewritten_list = [self.value(item) for item in value]
            result = (
                value
                if all(
                    before is after
                    for before, after in zip(value, rewritten_list, strict=True)
                )
                else rewritten_list
            )
        elif isinstance(value, dict):
            rewritten_dict = {
                key: self.value(item) for key, item in value.items()
            }
            result = (
                value
                if all(rewritten_dict[key] is item for key, item in value.items())
                else rewritten_dict
            )
        elif is_dataclass(value) and not isinstance(value, type):
            updates: dict[str, object] = {}
            for descriptor in fields(value):
                if not descriptor.init or descriptor.name in {
                    "origin",
                    "source_origin",
                    "type",
                }:
                    continue
                current = getattr(value, descriptor.name)
                rewritten = self.value(current)
                if rewritten is not current:
                    updates[descriptor.name] = rewritten
            result = replace(value, **updates) if updates else value
        else:
            result = value
        if cacheable:
            self._value_memo[id(value)] = (value, result)
        return result

    def rewrite_special(
        self,
        value: expr.Expression,
    ) -> expr.Expression | None:
        """Return a replacement, or ``None`` to recurse through the node."""

        del value
        return None


__all__ = ["SimulationExpressionRewriter"]
