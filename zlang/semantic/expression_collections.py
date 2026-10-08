# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative collections expression semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import replace
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import functional as ir_functional
from zlang.ir import functional_regions
from zlang.ir import packing as ir_packing
from zlang.ir import types as ir_types
from zlang.common import stable_digest
from . import callables as semantic_callables
from . import compile_time_evaluation
from . import expression_ranges
from . import expression_origins
from . import expression_operators
from . import expression_packed
from . import limits as semantic_limits
from . import symbols as semantic_symbols
from .errors import SemanticError
from .expression_coercion import can_implicitly_bitcast_types, fixed_overflow_for_type, fixed_rounding, make_bitcast
from .expression_support import _expand_immutable_locals

if TYPE_CHECKING:
    from .context import ExpressionContext


def _check_index_and_slice_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if isinstance(expression, ast.IndexExpr):
        collection = context.expressions.check(expression.expression, inputs, None, context)
        packed = expression_packed.check_packed_index(
            expression, collection, inputs, expected, context
        )
        if packed is not None:
            return packed
        vector = collection
        if isinstance(vector.type, ir_types.TupleType):
            if not isinstance(expression.index, int):
                raise SemanticError(
                    "tuple projection requires a zero-based integer literal index"
                )
            index = expression.index
            if index < 0 or index >= len(vector.type.elements):
                raise SemanticError(
                    f"tuple index {index} is out of range for {vector.type}"
                )
            return ir_expr.TupleProject(
                vector, index, vector.type.elements[index]
            )
        if not isinstance(vector.type, ir_types.VecType):
            raise SemanticError(f"indexing requires a vector, got {vector.type}")
        if isinstance(expression.index, int):
            index = expression.index
        elif (
            isinstance(expression.index, ast.NameExpr)
            and expression.index.name in context.scope.index_bindings
        ):
            index = context.scope.index_bindings[expression.index.name]
        else:
            typed_index = context.expressions.check(expression.index, inputs, None, context)
            typed_index = _expand_immutable_locals(
                typed_index, inputs, work_budget=context.services
            )
            typed_index = semantic_callables._expand_analysis_calls(
                typed_index, context, purpose="runtime vector index"
            )
            if isinstance(typed_index, ir_expr.Constant):
                index = typed_index.value
            else:
                if not isinstance(typed_index.type, (ir_types.UIntType, ir_types.BitsType)):
                    raise SemanticError(
                        "runtime vector index must be an unsigned integral expression; "
                        f"got {typed_index.type}"
                    )
                value_range = expression_ranges.static_value_range(
                    typed_index, context.scope.range_refinements
                )
                if value_range is None:
                    raise SemanticError(
                        "runtime vector index has no statically provable unsigned range; "
                        f"got {typed_index.type}, required 0..{vector.type.length - 1}"
                    )
                if value_range.minimum < 0 or value_range.maximum >= vector.type.length:
                    raise SemanticError(
                        f"runtime index range {value_range.minimum}..{value_range.maximum} "
                        f"is not provably within vector length {vector.type.length} "
                        f"(required 0..{vector.type.length - 1}, type {typed_index.type})"
                    )
                return ir_expr.RuntimeIndex(
                    vector,
                    typed_index,
                    vector.type.length,
                    value_range,
                    vector.type.element_type,
                )
        if index < 0 or index >= vector.type.length:
            raise SemanticError(
                f"vector index {index} is out of range for {vector.type}"
            )
        return ir_expr.VectorIndex(vector, index, vector.type.element_type)
    if isinstance(expression, ast.VectorRangeExpr):
        vector = context.expressions.check(expression.expression, inputs, None, context)
        if not isinstance(vector.type, ir_types.VecType):
            raise SemanticError(
                f"vector range requires a vector, got {vector.type}; "
                "packed scalar values use inclusive [MSB:LSB] slicing"
            )
        start = compile_time_evaluation.resolve_range_bound(
            expression.start, context, "vector-range start", inputs
        )
        stop = compile_time_evaluation.resolve_range_bound(
            expression.stop, context, "vector-range stop", inputs
        )
        if stop <= start:
            raise SemanticError(
                f"vector range {start}..{stop} is empty; ranges are half-open"
            )
        if start < 0 or stop > vector.type.length:
            raise SemanticError(
                f"vector range {start}..{stop} is out of range for {vector.type} "
                f"(required 0..{vector.type.length})"
            )
        result_type = ir_types.VecType(stop - start, vector.type.element_type)
        if expected is not None and expected != result_type:
            raise SemanticError(
                f"vector range {start}..{stop} produces {result_type}, "
                f"expected exact {expected}"
            )
        # Use the same retained IR as an explicit vector literal containing
        # the static projections.  The concise range therefore changes only
        # spelling, not semantic/canonical/backend identity.
        return ir_expr.Generate(
            "vector_literal",
            0,
            stop - start,
            tuple(
                ir_expr.VectorIndex(
                    vector, index, vector.type.element_type
                )
                for index in range(start, stop)
            ),
            result_type,
        )
    if isinstance(expression, ast.SliceExpr):
        return expression_packed.check_static_slice(
            expression, inputs, expected, context
        )
    if isinstance(expression, ast.DynamicSliceExpr):
        return expression_packed.check_dynamic_slice(
            expression, inputs, expected, context
        )
    if isinstance(expression, ast.ConcatExpr):
        if len(expression.arguments) < 2:
            raise SemanticError("concat requires at least two operands")
        operands = tuple(
            context.expressions.check(argument, inputs, None, context)
            for argument in expression.arguments
        )
        vector_operands = tuple(
            operand for operand in operands if isinstance(operand.type, ir_types.VecType)
        )
        if vector_operands:
            if len(vector_operands) != len(operands):
                raise SemanticError(
                    "concat cannot mix vector and non-vector operands; use "
                    "bitcast<bits<W>>(vector) for explicit raw-bit assembly"
                )
            first = vector_operands[0].type
            assert isinstance(first, ir_types.VecType)
            for index, operand in enumerate(vector_operands[1:], start=2):
                assert isinstance(operand.type, ir_types.VecType)
                if operand.type.element_type != first.element_type:
                    raise SemanticError(
                        "vector concat requires exact common element type; "
                        f"operand 1 has {first.element_type}, operand {index} "
                        f"has {operand.type.element_type}"
                    )
            result_type = ir_types.VecType(
                sum(operand.type.length for operand in vector_operands),
                first.element_type,
            )
            if (
                expected is not None
                and expected != result_type
                and not can_implicitly_bitcast_types(result_type, expected)
            ):
                raise SemanticError(
                    f"vector concat produces {result_type}, expected exact {expected}"
                )
            return ir_expr.VectorConcat(operands, result_type)
        widths: list[int] = []
        for index, operand in enumerate(operands):
            try:
                widths.append(ir_packing.packed_width(operand.type))
            except ir_packing.PackingError as error:
                raise SemanticError(
                    f"concat operand {index + 1} has non-bit-packable type "
                    f"{operand.type}: {error}"
                ) from error
        result_type = ir_types.BitsType(sum(widths))
        if (
            expected is not None
            and expected != result_type
            and not can_implicitly_bitcast_types(result_type, expected)
        ):
            raise SemanticError(
                f"concat produces {result_type}, expected exact {expected}",
                code="ZL-WIDTH-CONCAT",
                primary=expression_origins.semantic_origin(expression, context),
                notes=tuple(
                    (
                        f"operand {index + 1}: exact type {operand.type}, "
                        f"packed width {width}"
                    )
                    for index, (operand, width) in enumerate(
                        zip(operands, widths, strict=True)
                    )
                )
                + (f"total packed width: {sum(widths)}",),
                fixes=(
                    "resize an operand explicitly before concat, or use an "
                    "equal-width bitcast only for a representation change",
                ),
            )
        return ir_expr.Concat(operands, result_type)
    if isinstance(expression, ast.BitcastExpr):
        if context.environment.type_resolver is None:
            raise SemanticError("bitcast target requires a type resolver")
        target_type = context.environment.type_resolver.resolve(expression.target_type)
        operand = context.expressions.check(expression.expression, inputs, None, context)
        result = make_bitcast(
            operand, target_type, description=f"bitcast<{target_type}>"
        )
        if (
            expected is not None
            and expected != target_type
            and not can_implicitly_bitcast_types(target_type, expected)
        ):
            raise SemanticError(
                f"bitcast<{target_type}> produces {target_type}, "
                f"expected exact {expected}"
            )
        return result
    return None

def _check_indexed_vector(
    index: str,
    start: int | str,
    stop: int | str,
    body: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
    constructor: type[ir_expr.Generate] | type[ir_expr.Map],
) -> ir_expr.Generate | ir_expr.Map | ir_expr.FunctionalRegion:
    start = compile_time_evaluation.resolve_range_bound(start, context, "start", inputs)
    stop = compile_time_evaluation.resolve_range_bound(stop, context, "stop", inputs)
    if stop <= start:
        raise SemanticError(
            f"functional range {start}..{stop} is empty; ranges are half-open"
        )
    length = stop - start
    if length > semantic_limits.FUNCTIONAL_RANGE:
        raise SemanticError(
            f"functional range {start}..{stop} expands to {length} elements; "
            f"the compile-time generation limit is {semantic_limits.FUNCTIONAL_RANGE}"
        )
    budget = context.services.compile_time_budget
    if budget is not None:
        budget.generated_elements += length
        if budget.generated_elements > semantic_limits.TOTAL_GENERATED:
            raise SemanticError(
                f"compile-time generation exceeds {semantic_limits.TOTAL_GENERATED} elements"
            )
    if (
        index in inputs
        or index in context.scope.index_bindings
        or index in context.environment.parameters
    ):
        raise SemanticError(f"functional range index '{index}' shadows an existing name")

    expected_element: ir_types.HardwareType | None = None
    if expected is not None:
        if not isinstance(expected, ir_types.VecType):
            raise SemanticError(
                f"generated vector cannot be context-sized as scalar {expected}"
            )
        if expected.length != length:
            raise SemanticError(
                f"functional range {start}..{stop} creates {length} elements, "
                f"expected {expected.length}"
            )
        expected_element = expected.element_type

    body_key = id(body)
    binder_ordinal = context.scope.functional_binder_ordinals.get(body_key)
    if binder_ordinal is None:
        binder_ordinal = context.scope.next_functional_binder_ordinal[0]
        context.scope.next_functional_binder_ordinal[0] += 1
        context.scope.functional_binder_ordinals[body_key] = binder_ordinal
    binder_nesting = (*context.scope.functional_binder_nesting, binder_ordinal)

    kind = (
        functional_regions.FunctionalRegionKind.GENERATE
        if constructor is ir_expr.Generate
        else functional_regions.FunctionalRegionKind.MAP
    )
    body_origin = expression_origins.semantic_origin(body, context)
    if context.scope.functional_binder_callable_identity is None:
        binder_identity_payload = {
            "schema": "zlang-functional-binder-v2",
            "resolution_stack": context.scope.resolution_stack,
            "semantic_nesting": context.scope.functional_binder_nesting,
            "declaration_ordinal": binder_ordinal,
            "kind": kind.value,
            "start": start,
            "stop": stop,
        }
    else:
        binder_identity_payload = {
            "schema": "zlang-callable-functional-binder-v1",
            "callable_identity": context.scope.functional_binder_callable_identity,
            "semantic_nesting": context.scope.functional_binder_nesting,
            "declaration_ordinal": binder_ordinal,
            "kind": kind.value,
            "start": start,
            "stop": stop,
        }
    binder = functional_regions.CompileTimeBinderRef(
        stable_digest(binder_identity_payload),
        index,
        start,
        stop,
        body_origin,
    )
    index_type = ir_types.UIntType(max(1, (stop - 1).bit_length()))

    # Admission happens before eager expansion so iterator-dependent generic
    # calls cannot create one monomorphic definition per element.  The
    # symbolic checker is deliberately fail-closed: any use that needs a
    # concrete shape, type, branch, overload, or effectful boundary falls
    # through to the established per-element elaborator below.  A current
    # register read is an invariant pure capture; transition ownership never
    # enters the region.
    if (
        length >= semantic_limits.FUNCTIONAL_REGION_THRESHOLD
        or context.scope.functional_symbolic_values
    ):
        certificate_start = len(
            context.scope.functional_specialization_certificates
        )
        attempt_state = context.services.callables.snapshot()
        binder_ordinals = dict(context.scope.functional_binder_ordinals)
        next_binder_ordinal = context.scope.next_functional_binder_ordinal[0]
        attempt_budget = (
            (
                budget.generated_elements,
                budget.operations,
                budget.call_depth,
            )
            if budget is not None
            else None
        )
        attempt_expansion_nodes = context.services.local_expansion_nodes
        observational_lengths = tuple(
            len(items) if items is not None else None
            for items in (
                context.services.tooling.definition_resolutions,
                context.services.tooling.definition_declarations,
                context.services.tooling.completion_scopes,
                context.services.tooling.signature_help_calls,
                context.services.exploration_results,
            )
        )
        symbolic_context = context.with_scope(
            allow_delay=False,
            allow_implementation_choice=False,
            functional_symbolic_values={
                **context.scope.functional_symbolic_values,
                index: functional_regions.CompileTimeExpr.ref(binder),
            },
            index_types={**context.scope.index_types, index: index_type},
            functional_binder_nesting=binder_nesting,
        )
        try:
            symbolic = context.expressions.check(
                body, inputs, expected_element, symbolic_context
            )
            if expected_element is not None and symbolic.type != expected_element:
                raise ValueError("symbolic functional element changed exact type")
            symbolic_type = ir_types.VecType(length, symbolic.type)
            symbolic = _expand_immutable_locals(
                symbolic, inputs, work_budget=context.services
            )
            pending_certificates = tuple(
                dict.fromkeys(
                    context.scope.functional_specialization_certificates[
                        certificate_start:
                    ]
                )
            )
            certificates = tuple(
                certificate
                for certificate in pending_certificates
                if certificate.owner_binder_identity == binder.identity
            )
            context.scope.functional_specialization_certificates[certificate_start:] = (
                certificate
                for certificate in pending_certificates
                if certificate.owner_binder_identity != binder.identity
            )
            symbolic, captures = (
                context.services.callable_specializer.close_nested_functional_template(
                symbolic,
                binder,
                )
            )
            region = ir_expr.FunctionalRegion(
                kind,
                binder,
                symbolic,
                (),
                captures,
                symbolic_type,
                certificates,
            )
            return region
        except (SemanticError, ValueError, ir_functional.FunctionalLoweringError):
            # The eager path remains the semantic authority for unsupported or
            # invalid constructs, including the exact public diagnostic.
            del context.scope.functional_specialization_certificates[certificate_start:]
            context.services.callables.restore(attempt_state)
            context.scope.functional_binder_ordinals.clear()
            context.scope.functional_binder_ordinals.update(binder_ordinals)
            context.scope.next_functional_binder_ordinal[0] = next_binder_ordinal
            if budget is not None and attempt_budget is not None:
                (
                    budget.generated_elements,
                    budget.operations,
                    budget.call_depth,
                ) = attempt_budget
            context.services.local_expansion_nodes = attempt_expansion_nodes
            for items, retained in zip(
                (
                    context.services.tooling.definition_resolutions,
                    context.services.tooling.definition_declarations,
                    context.services.tooling.completion_scopes,
                    context.services.tooling.signature_help_calls,
                    context.services.exploration_results,
                ),
                observational_lengths,
                strict=True,
            ):
                if items is not None and retained is not None:
                    del items[retained:]
            pass

    elements: list[ir_expr.Expression] = []
    element_type = expected_element
    for value in range(start, stop):
        body_context = context.with_scope(
            allow_delay=False,
            allow_implementation_choice=False,
            index_bindings={**context.scope.index_bindings, index: value},
            index_types={**context.scope.index_types, index: index_type},
            functional_binder_nesting=binder_nesting,
        )
        element = context.expressions.check(body, inputs, element_type, body_context)
        if element_type is None:
            element_type = element.type
        if element.type != element_type:
            raise SemanticError(
                f"functional element at {index}={value} has type {element.type}, "
                f"expected {element_type}"
            )
        elements.append(element)
    assert element_type is not None
    type_ = ir_types.VecType(length, element_type)
    if length >= semantic_limits.FUNCTIONAL_REGION_THRESHOLD:
        definitions = (
            *context.services.callables.function_definitions.values(),
            *context.services.callables.callable_definitions.values(),
        )
        local_expansion_memo: dict[int, ir_expr.Expression] = {}
        compacted = ir_functional.compact_functional_elements(
            kind,
            binder,
            # Compiler lowering erases immutable locals before either backend.
            # Expose their concrete typed expressions before anti-unification
            # so a compact region cannot retain a dangling local InputRef.
            # Stateful/protocol-derived locals then correctly fail the pure
            # region gate and remain an ordinary bounded Generate/Map.
            tuple(
                _expand_immutable_locals(
                    element,
                    inputs,
                    memo=local_expansion_memo,
                    work_budget=context.services,
                )
                for element in elements
            ),
            type_,
            definitions,
        )
        if compacted is not None:
            region, inlined_identities = compacted
            for identity in inlined_identities:
                context.services.callables.release_use(identity)
            return region
    return constructor(index, start, stop, tuple(elements), type_)

def _check_reduction(
    operator: ir_expr.ReductionOperator,
    collection: ir_expr.Expression,
    context: ExpressionContext,
) -> ir_expr.Reduce:
    if not isinstance(collection.type, ir_types.VecType):
        raise SemanticError(
            f"reduce({operator.value}, ...) requires a vector collection, "
            f"got {collection.type}"
        )
    element_type = collection.type.element_type
    if isinstance(element_type, ir_types.StructType):
        if operator is not ir_expr.ReductionOperator.ADD:
            raise SemanticError(
                "nominal aggregate reduction currently supports only additive sum"
            )
        if isinstance(collection, ir_expr.FunctionalRegion):

            def resolve_combine(
                left_type: ir_types.HardwareType,
                right_type: ir_types.HardwareType,
            ) -> functional_regions.ExactReductionCombine:
                left = ir_expr.FunctionalCaptureRef(
                    stable_digest(("exact-reduce-left", str(left_type))),
                    "exact_reduce_left",
                    left_type,
                )
                right = ir_expr.FunctionalCaptureRef(
                    stable_digest(("exact-reduce-right", str(right_type))),
                    "exact_reduce_right",
                    right_type,
                )
                try:
                    combined = expression_operators.resolve_operator("+", (left, right), context)
                except SemanticError as error:
                    raise SemanticError(
                        "nominal aggregate sum cannot resolve an exact balanced-tree "
                        f"operator '+' for {left_type} and {right_type}: {error}"
                    ) from error
                if not isinstance(combined, ir_expr.Call):
                    raise SemanticError(
                        "nominal aggregate sum requires a retained typed callable "
                        f"for operator '+' on {left_type} and {right_type}"
                    )
                if combined.callee_identity is None:
                    raise SemanticError(
                        "nominal aggregate sum callable has no semantic identity"
                    )
                return functional_regions.ExactReductionCombine(
                    combined.type,
                    combined.function,
                    combined.callee_identity,
                )

            plan = functional_regions.build_exact_reduction_plan(
                element_type,
                collection.type.length,
                resolve_combine,
            )
            return ir_expr.Reduce(
                operator,
                collection,
                plan.root_type,
                plan=plan,
            )
        expanded = _balanced_nominal_addition(
            ir_functional.collection_elements(collection),
            context,
        )
        return ir_expr.Reduce(
            operator,
            collection,
            expanded.type,
            expanded=expanded,
        )
    else:
        try:
            type_ = ir_functional.reduction_result_type(
                operator,
                element_type,
                collection.type.length,
            )
        except ir_functional.FunctionalLoweringError as error:
            if operator is ir_expr.ReductionOperator.ADD:
                raise SemanticError(
                    "addition reduction requires one integer signedness family: "
                    f"{error}"
                ) from error
            raise SemanticError(str(error)) from error
        assert isinstance(
            type_,
            (ir_types.BitType, ir_types.UIntType, ir_types.SIntType, ir_types.BitsType, ir_types.FixedType, ir_types.UFixedType),
        )
    return ir_expr.Reduce(operator, collection, type_)

def _balanced_nominal_addition(
    elements: tuple[ir_expr.Expression, ...],
    context: ExpressionContext,
) -> ir_expr.Expression:
    """Resolve the frozen source-order balanced tree through exact `operator +`."""

    if not elements:
        raise SemanticError("cannot reduce an empty nominal collection")
    if len(elements) == 1:
        return elements[0]
    middle = len(elements) // 2
    left = _balanced_nominal_addition(elements[:middle], context)
    right = _balanced_nominal_addition(elements[middle:], context)
    try:
        return expression_operators.resolve_operator("+", (left, right), context)
    except SemanticError as error:
        raise SemanticError(
            "nominal aggregate sum cannot resolve an exact balanced-tree "
            f"operator '+' for {left.type} and {right.type}: {error}"
        ) from error

def _check_dot(
    expression: ast.DotExpr,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    context: ExpressionContext,
    expected: ir_types.HardwareType | None,
) -> ir_expr.Expression:
    origin = expression_origins.semantic_origin(expression, context)
    left = context.expressions.check(expression.left, inputs, None, context)
    right = context.expressions.check(expression.right, inputs, None, context)
    if not isinstance(left.type, ir_types.VecType) or not isinstance(right.type, ir_types.VecType):
        raise SemanticError(
            f"dot requires two vectors, got {left.type} and {right.type}"
        )
    if left.type.length != right.type.length:
        raise SemanticError(
            "dot vector lengths must match, got "
            f"{left.type.length} and {right.type.length}"
        )
    products: list[ir_expr.Expression] = []
    for index in range(left.type.length):
        left_element = ir_expr.VectorIndex(
            left,
            index,
            left.type.element_type,
            origin=origin,
        )
        right_element = ir_expr.VectorIndex(
            right,
            index,
            right.type.element_type,
            origin=origin,
        )
        if isinstance(left_element.type, ir_types.StructType) or isinstance(
            right_element.type, ir_types.StructType
        ):
            product = expression_operators.resolve_operator(
                "*", (left_element, right_element), context,
                call_origin=origin,
            )
        else:
            product = expression_operators.build_binary(
                ir_expr.BinaryOperator.MULTIPLY,
                left_element,
                right_element,
            )
        products.append(replace(product, origin=origin))
    product_tuple = tuple(products)
    dot = ir_expr.Dot(
        left,
        right,
        product_tuple,
        ir_types.VecType(left.type.length, product_tuple[0].type),
        origin=origin,
    )
    reduction = _check_reduction(ir_expr.ReductionOperator.ADD, dot, context)
    if expression.rounding is None:
        return reduction
    if not isinstance(expected, (ir_types.FixedType, ir_types.UFixedType)):
        raise SemanticError(
            "dot(a,b,rounding) requires a contextual fixed-point target"
        )
    if type(reduction.type) is not type(expected):
        raise SemanticError(
            f"rounded dot cannot convert {reduction.type} to {expected}; "
            "signedness must match"
        )
    if expected.fraction >= reduction.type.fraction:
        raise SemanticError(
            "dot rounding is only valid when the contextual target discards "
            "fractional bits; use dot(a,b) for an exact result"
        )
    return ir_expr.FixedConvert(
        reduction,
        fixed_rounding(expression.rounding),
        fixed_overflow_for_type(expected),
        ir_expr.FixedConversionKind.RESCALE,
        expected,
    )
