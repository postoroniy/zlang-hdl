"""Deterministic exact-value normalization for selected typed IR.

This pass deliberately runs after semantic analysis.  Source-facing IR keeps
every callable specialization for diagnostics and editor navigation, while the
selected hardware graph may fold pure calls and discard executable definitions
that no longer have a use.  No e-graph or backend syntax participates here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, fields, is_dataclass, replace
from enum import Enum
from typing import get_args

from zlang.fixed_point import apply_overflow, quantize_rational, round_ratio
from zlang.ir import expressions as expr
from zlang.ir.callables import (
    CallableReachabilityError,
    reachable_module_callables,
)
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.exact_simplification import (
    simplify_add,
    simplify_binary,
    simplify_mux,
    static_unsigned_range,
)
from zlang.ir.module import Function, Module
from zlang.ir.runtime_values import RuntimeValueError, normalize_scalar
from zlang.ir.traversal import walk_expression
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    UFixedType,
    UIntType,
)


NORMALIZATION_SCHEMA = "zlang-selected-value-normalization-v3"
DEFAULT_SINGLE_USE_INLINE_NODES = 64
DEFAULT_TOTAL_INLINE_NODES = 16_384


class SelectedValueNormalizationError(ValueError):
    """Selected typed IR cannot be normalized without losing exactness."""


@dataclass(frozen=True)
class SelectedValueNormalizationStatistics:
    expression_requests: int
    expression_cache_hits: int
    unique_expression_visits: int
    unique_object_visits: int


_SCALAR_TYPES = (BitType, BitsType, EnumType, FixedType, SIntType, UFixedType, UIntType)
_HARDWARE_TYPE_CLASSES = tuple(get_args(HardwareType))


def _with_origin(value: expr.Expression, origin) -> expr.Expression:
    return replace(value, origin=origin) if origin is not None else value


def _constant(value: int, type_: HardwareType, origin=None) -> expr.Constant:
    try:
        normalized = normalize_scalar(value, type_)
    except RuntimeValueError as error:
        raise SelectedValueNormalizationError(str(error)) from error
    return expr.Constant(normalized, type_, origin=origin)


def _binary_constant(value: expr.Binary) -> expr.Constant | None:
    if not isinstance(value.left, expr.Constant) or not isinstance(value.right, expr.Constant):
        return None
    left = value.left.value
    right = value.right.value
    operator = value.operator
    if operator is expr.BinaryOperator.SUBTRACT:
        result = left - right
    elif operator is expr.BinaryOperator.MULTIPLY:
        result = left * right
    elif operator is expr.BinaryOperator.BIT_AND:
        result = left & right
    elif operator is expr.BinaryOperator.BIT_OR:
        result = left | right
    elif operator is expr.BinaryOperator.BIT_XOR:
        result = left ^ right
    elif operator is expr.BinaryOperator.SHIFT_LEFT:
        result = left << right
    elif operator is expr.BinaryOperator.SHIFT_RIGHT:
        result = left >> right
    elif operator is expr.BinaryOperator.EQUAL:
        result = int(left == right)
    elif operator is expr.BinaryOperator.NOT_EQUAL:
        result = int(left != right)
    elif operator is expr.BinaryOperator.LESS:
        result = int(left < right)
    elif operator is expr.BinaryOperator.LESS_EQUAL:
        result = int(left <= right)
    elif operator is expr.BinaryOperator.GREATER:
        result = int(left > right)
    elif operator is expr.BinaryOperator.GREATER_EQUAL:
        result = int(left >= right)
    else:  # pragma: no cover - closed enum guard
        return None
    return _constant(result, value.type, value.origin)


def _fold_local(value: expr.Expression) -> expr.Expression:
    """Fold one already-child-normalized typed node."""

    if isinstance(value, expr.Add):
        if isinstance(value.left, expr.Constant) and isinstance(value.right, expr.Constant):
            return _constant(value.left.value + value.right.value, value.type, value.origin)
        simplified = simplify_add(value.left, value.right, value.type)
        return _with_origin(simplified, value.origin) if simplified is not None else value
    if isinstance(value, expr.Binary):
        folded = _binary_constant(value)
        if folded is not None:
            return folded
        simplified = simplify_binary(
            value.operator,
            value.left,
            value.right,
            value.type,
            range_of=static_unsigned_range,
        )
        return _with_origin(simplified, value.origin) if simplified is not None else value
    if isinstance(value, expr.Mux):
        simplified = simplify_mux(
            value.condition,
            value.when_true,
            value.when_false,
        )
        return _with_origin(simplified, value.origin) if simplified is not None else value
    if isinstance(value, expr.Switch) and isinstance(value.selector, expr.Constant):
        selected = next(
            (case.expression for case in value.cases if case.key == value.selector.value),
            value.default,
        )
        return _with_origin(selected, value.origin)
    if isinstance(value, expr.Extend) and isinstance(value.expression, expr.Constant):
        return _constant(value.expression.value, value.type, value.origin)
    if isinstance(value, expr.Truncate) and isinstance(value.expression, expr.Constant):
        return _constant(value.expression.value, value.type, value.origin)
    if isinstance(value, expr.FixedConvert) and isinstance(value.expression, expr.Constant):
        raw = value.expression.value
        if value.kind in {expr.FixedConversionKind.FROM_RAW, expr.FixedConversionKind.TO_RAW}:
            return _constant(raw, value.type, value.origin)
        if value.rational_denominator is not None:
            converted = quantize_rational(
                raw,
                value.rational_denominator,
                fraction=value.type.fraction,
                width=value.type.width,
                signed=isinstance(value.type, FixedType),
                rounding=value.rounding,
                overflow=value.overflow,
            )
        else:
            delta = value.type.fraction - getattr(value.expression.type, "fraction", 0)
            converted = raw << delta if delta >= 0 else round_ratio(raw, 1 << -delta, value.rounding)
            converted = apply_overflow(
                converted,
                width=value.type.width,
                signed=isinstance(value.type, FixedType),
                policy=value.overflow,
            )
        return _constant(converted, value.type, value.origin)
    if isinstance(value, expr.FieldAccess) and isinstance(value.expression, expr.StructConstruct):
        selected = dict(value.expression.fields).get(value.field)
        return _with_origin(selected, value.origin) if selected is not None else value
    if isinstance(value, expr.TupleProject) and isinstance(value.expression, expr.TupleConstruct):
        return _with_origin(value.expression.elements[value.index], value.origin)
    if (
        isinstance(value, expr.VectorIndex)
        and isinstance(value.index, int)
        and isinstance(value.expression, (expr.Generate, expr.Map))
    ):
        return _with_origin(
            value.expression.elements[value.index],
            value.origin,
        )
    if isinstance(value.type, _SCALAR_TYPES):
        try:
            result = constant_runtime_value(value)
        except ConstantExpressionError:
            return value
        if isinstance(result, int) and not isinstance(result, bool):
            return _constant(result, value.type, value.origin)
    return value


def _substitute(
    value: object,
    bindings: dict[str, expr.Expression],
    memo: dict[int, tuple[object, object]] | None = None,
) -> object:
    if memo is None:
        memo = {}
    if isinstance(value, expr.ParameterRef) and value.name in bindings:
        return bindings[value.name]
    if isinstance(value, (expr.Expression, tuple)) or (
        is_dataclass(value) and not isinstance(value, type)
    ):
        cached = memo.get(id(value))
        if cached is not None and cached[0] is value:
            return cached[1]
    if isinstance(value, expr.Expression):
        updates = {
            item.name: _substitute(getattr(value, item.name), bindings, memo)
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        result = replace(value, **updates) if updates else value
        memo[id(value)] = (value, result)
        return result
    if isinstance(value, tuple):
        result = tuple(_substitute(item, bindings, memo) for item in value)
        memo[id(value)] = (value, result)
        return result
    if is_dataclass(value) and not isinstance(value, type):
        updates = {
            item.name: _substitute(getattr(value, item.name), bindings, memo)
            for item in fields(value)
            if item.init and item.name not in {"type", "origin", "source_origin"}
        }
        result = replace(value, **updates) if updates else value
        memo[id(value)] = (value, result)
        return result
    return value


def _parameter_dependencies(value: expr.Expression) -> frozenset[str]:
    return frozenset(
        item.name for item in walk_expression(value) if isinstance(item, expr.ParameterRef)
    )


def _call_counts(module: Module) -> Counter[str]:
    counts: Counter[str] = Counter()
    seen_calls: set[tuple[object, ...]] = set()
    visited: dict[int, object] = {}

    def visit(value: object) -> None:
        if isinstance(value, (tuple, list, dict)) or (
            is_dataclass(value) and not isinstance(value, type)
        ):
            previous = visited.get(id(value))
            if previous is value:
                return
            visited[id(value)] = value
        if isinstance(value, expr.Call):
            identity = value.callee_identity or f"name:{value.function}"
            # A local aggregate call projected at several fields is
            # represented by several traced Call nodes at ``name local``
            # references, but is one executable value and one materialized
            # use.  Genuine source call sites retain their distinct spans.
            occurrence_origin = value.origin
            if (
                occurrence_origin is not None
                and occurrence_origin.construct.startswith("name ")
            ):
                occurrence_origin = None
            occurrence = (
                identity,
                occurrence_origin,
                tuple(id(argument) for argument in value.arguments),
                value.type,
            )
            if occurrence not in seen_calls:
                seen_calls.add(occurrence)
                counts[identity] += 1
        if isinstance(value, expr.Expression):
            for item in fields(value):
                if item.name not in {"type", "origin"}:
                    visit(getattr(value, item.name))
            return
        if isinstance(value, tuple):
            for item in value:
                visit(item)
            return
        if is_dataclass(value) and not isinstance(value, type):
            for item in fields(value):
                if item.name not in {"origin", "source_origin", "children"}:
                    visit(getattr(value, item.name))

    for item in fields(module):
        if item.name not in {
            "children",
            "functions",
            "callable_definitions",
            "generic_specializations",
        }:
            visit(getattr(module, item.name))
    # Count executable definitions only in the actual root closure.  Counting
    # every elaboration/provenance definition makes a physically single-use
    # specialization look multi-use and defeats bounded inlining before DCE.
    try:
        reachable = reachable_module_callables(module)
    except CallableReachabilityError as error:
        raise SelectedValueNormalizationError(str(error)) from error
    for definition in reachable:
        visit(definition.body)
    return counts


class _Normalizer:
    def __init__(
        self,
        module: Module,
        *,
        single_use_inline_nodes: int,
        total_inline_nodes: int,
        inline_all_calls: bool,
    ) -> None:
        definitions = (*module.functions, *module.callable_definitions)
        self.by_identity = {item.callee_identity: item for item in definitions}
        self.by_name: dict[str, list[Function]] = {}
        for item in definitions:
            self.by_name.setdefault(item.name, []).append(item)
        self.call_counts = _call_counts(module)
        self.dependencies = {
            item.callee_identity: _parameter_dependencies(item.body)
            for item in definitions
        }
        self.body_nodes = {
            item.callee_identity: sum(1 for _ in walk_expression(item.body))
            for item in definitions
        }
        self.single_use_inline_nodes = single_use_inline_nodes
        self.remaining_inline_nodes = total_inline_nodes
        self.inline_all_calls = inline_all_calls
        # Retaining the input object beside its result prevents Python object
        # ID reuse while allowing each shared DAG node to be rewritten once.
        self.memo: dict[int, tuple[expr.Expression, expr.Expression]] = {}
        self.object_memo: dict[int, tuple[object, object]] = {}
        self.active: set[str] = set()
        self.expression_requests = 0
        self.expression_cache_hits = 0
        self.unique_expression_visits = 0
        self.unique_object_visits = 0

    @property
    def statistics(self) -> SelectedValueNormalizationStatistics:
        return SelectedValueNormalizationStatistics(
            expression_requests=self.expression_requests,
            expression_cache_hits=self.expression_cache_hits,
            unique_expression_visits=self.unique_expression_visits,
            unique_object_visits=self.unique_object_visits,
        )

    def resolve(self, call: expr.Call) -> Function | None:
        if call.callee_identity is not None:
            candidate = self.by_identity.get(call.callee_identity)
            return candidate if candidate is not None and candidate.name == call.function else None
        candidates = self.by_name.get(call.function, ())
        return candidates[0] if len(candidates) == 1 else None

    def rewrite_expression(self, value: expr.Expression) -> expr.Expression:
        self.expression_requests += 1
        object_id = id(value)
        cached = self.memo.get(object_id)
        if cached is not None and cached[0] is value:
            self.expression_cache_hits += 1
            return cached[1]
        self.unique_expression_visits += 1
        if isinstance(value, expr.Call):
            arguments = tuple(self.rewrite_expression(item) for item in value.arguments)
            call = replace(value, arguments=arguments)
            definition = self.resolve(call)
            if (
                definition is None
                or definition.callee_identity in self.active
            ):
                result = call
            else:
                identity = definition.callee_identity
                key = identity if call.callee_identity is not None else f"name:{call.function}"
                parameter_independent = not (
                    self.dependencies[identity]
                    & {parameter.name for parameter in definition.parameters}
                )
                constant_arguments = all(isinstance(item, expr.Constant) for item in arguments)
                single_use = self.call_counts[key] == 1
                body_nodes = self.body_nodes[identity]
                bounded_inline = (
                    single_use
                    and body_nodes <= self.single_use_inline_nodes
                    and body_nodes <= self.remaining_inline_nodes
                )
                forced_inline = (
                    self.inline_all_calls
                    and body_nodes <= self.remaining_inline_nodes
                )
                if (
                    parameter_independent
                    or constant_arguments
                    or bounded_inline
                    or forced_inline
                ):
                    if forced_inline:
                        self.remaining_inline_nodes -= body_nodes
                    elif bounded_inline and not (
                        parameter_independent or constant_arguments
                    ):
                        self.remaining_inline_nodes -= body_nodes
                    bindings = {
                        parameter.name: argument
                        for parameter, argument in zip(
                            definition.parameters, arguments, strict=True
                        )
                    }
                    try:
                        substituted = _substitute(definition.body, bindings)
                    except ValueError:
                        # A callable-owned FunctionalRegion deliberately keeps
                        # effect-free formal captures.  Substituting a live
                        # state/protocol argument into that region would move
                        # the observation across the callable boundary and
                        # violate the region's purity invariant.  Retaining the
                        # already-normalized call is the exact bounded fallback.
                        result = call
                        self.memo[object_id] = (value, result)
                        return result
                    if not isinstance(substituted, expr.Expression):
                        raise SelectedValueNormalizationError(
                            f"callable '{definition.name}' substitution lost expression type"
                        )
                    self.active.add(identity)
                    try:
                        result = self.rewrite_expression(substituted)
                    finally:
                        self.active.remove(identity)
                    result = _with_origin(result, value.origin)
                else:
                    result = call
            self.memo[object_id] = (value, result)
            return result
        updates = {
            item.name: self.rewrite_value(getattr(value, item.name))
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        rewritten = replace(value, **updates) if updates else value
        result = _fold_local(rewritten)
        self.memo[object_id] = (value, result)
        return result

    def rewrite_value(self, value: object) -> object:
        if isinstance(value, expr.Expression):
            return self.rewrite_expression(value)
        if isinstance(value, (str, bytes, int, float, bool, type(None), Enum)):
            return value
        if isinstance(value, _HARDWARE_TYPE_CLASSES):
            return value
        if isinstance(value, tuple):
            return tuple(self.rewrite_value(item) for item in value)
        if isinstance(value, list):
            return [self.rewrite_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self.rewrite_value(item) for key, item in value.items()}
        if is_dataclass(value) and not isinstance(value, type):
            cached = self.object_memo.get(id(value))
            if cached is not None and cached[0] is value:
                return cached[1]
            self.unique_object_visits += 1
            updates = {
                item.name: self.rewrite_value(getattr(value, item.name))
                for item in fields(value)
                if item.init and item.name not in {"origin", "source_origin"}
            }
            try:
                rewritten = replace(value, **updates) if updates else value
            except (TypeError, ValueError):
                rewritten = value
            self.object_memo[id(value)] = (value, rewritten)
            return rewritten
        return value


def normalize_selected_values(
    module: Module,
    *,
    single_use_inline_nodes: int = DEFAULT_SINGLE_USE_INLINE_NODES,
    total_inline_nodes: int = DEFAULT_TOTAL_INLINE_NODES,
    prune_callables: bool = True,
    inline_all_calls: bool = False,
) -> Module:
    """Fold exact pure calls and prune dead executable callables recursively."""

    if single_use_inline_nodes < 0 or total_inline_nodes < 0:
        raise SelectedValueNormalizationError("normalization budgets must be non-negative")
    children = tuple(
        normalize_selected_values(
            child,
            single_use_inline_nodes=single_use_inline_nodes,
            total_inline_nodes=total_inline_nodes,
            prune_callables=prune_callables,
            inline_all_calls=inline_all_calls,
        )
        for child in module.children
    )
    normalizer = _Normalizer(
        module,
        single_use_inline_nodes=single_use_inline_nodes,
        total_inline_nodes=total_inline_nodes,
        inline_all_calls=inline_all_calls,
    )
    updates = {
        item.name: normalizer.rewrite_value(getattr(module, item.name))
        for item in fields(module)
        if item.init
        and item.name not in {
            "functions",
            "callable_definitions",
            "children",
            "generic_specializations",
            "semantic_expression_arena_statistics",
            "semantic_expression_provenance",
            "selected_value_normalization_statistics",
        }
    }
    functions = tuple(
        replace(function, body=normalizer.rewrite_expression(function.body))
        for function in module.functions
    )
    callables = tuple(
        replace(function, body=normalizer.rewrite_expression(function.body))
        for function in module.callable_definitions
    )
    rewritten = replace(
        module,
        **updates,
        functions=functions,
        callable_definitions=callables,
        children=children,
        selected_value_normalization_statistics=normalizer.statistics,
    )
    if not prune_callables:
        return rewritten
    try:
        reachable = reachable_module_callables(rewritten)
    except CallableReachabilityError as error:
        raise SelectedValueNormalizationError(str(error)) from error
    live = {function.callee_identity for function in reachable}
    return replace(
        rewritten,
        functions=tuple(item for item in functions if item.callee_identity in live),
        callable_definitions=tuple(
            item for item in callables if item.callee_identity in live
        ),
    )


__all__ = [
    "DEFAULT_SINGLE_USE_INLINE_NODES",
    "DEFAULT_TOTAL_INLINE_NODES",
    "NORMALIZATION_SCHEMA",
    "SelectedValueNormalizationError",
    "SelectedValueNormalizationStatistics",
    "normalize_selected_values",
]
