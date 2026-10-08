# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative aggregates expression semantics."""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import replace
from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import functional as ir_functional
from zlang.ir import packing as ir_packing
from zlang.ir import runtime_values as runtime_values
from zlang.ir import types as ir_types
from . import expression_operators
from . import symbols as semantic_symbols
from . import type_resolution
from .errors import SemanticError

if TYPE_CHECKING:
    from .context import ExpressionContext
from .expression_coercion import can_implicitly_bitcast_types, check_integer_literal, make_bitcast
from .expression_control import _check_alternatives
from .expression_support import _resized_type

def _check_literal_and_aggregate_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if isinstance(expression, ast.PatternConstantExpr):
        if expression.kind is ast.PatternConstantKind.ZERO:
            raise SemanticError(
                "zero<x> is available only in equiv declarations; "
                "use zeros<W> for an ordinary exact-width bit constant"
            )
        if context.environment.type_resolver is None:
            raise SemanticError(
                f"{expression.kind.value}<{expression.witness}> requires a type resolver"
            )
        width = context.environment.type_resolver._eval_width(expression.witness)
        type_ = ir_types.BitsType(width)
        value = 0 if expression.kind is ast.PatternConstantKind.ZEROS else (1 << width) - 1
        if expected is not None and expected != type_ and not can_implicitly_bitcast_types(
            type_, expected
        ):
            raise SemanticError(
                f"{expression.kind.value}<{expression.witness}> produces {type_}, "
                f"expected exact {expected}"
            )
        return ir_expr.Constant(value, type_)
    if isinstance(expression, ast.NumberExpr):
        return check_integer_literal(
            expression.value,
            expected,
            signed_syntax=False,
        )
    if isinstance(expression, ast.CharLiteralExpr):
        type_ = ir_types.UIntType(8)
        if (
            expected is not None
            and expected != type_
            and not can_implicitly_bitcast_types(type_, expected)
        ):
            raise SemanticError(
                f"character literal has exact type {type_}, expected exact {expected}"
            )
        return ir_expr.Constant(expression.value, type_)
    if isinstance(expression, ast.StringLiteralExpr):
        if not expression.values:
            raise SemanticError(
                "empty string literal is not supported because vectors must have "
                "positive length"
            )
        type_ = ir_types.VecType(len(expression.values), ir_types.UIntType(8))
        if (
            expected is not None
            and expected != type_
            and not can_implicitly_bitcast_types(type_, expected)
        ):
            raise SemanticError(
                f"string literal has exact type {type_}, expected exact {expected}"
            )
        return ir_expr.Generate(
            "vector_literal",
            0,
            len(expression.values),
            tuple(
                ir_expr.Constant(value, ir_types.UIntType(8))
                for value in expression.values
            ),
            type_,
        )
    if isinstance(expression, ast.TupleLiteralExpr):
        if expected is not None and not isinstance(expected, ir_types.TupleType):
            raise SemanticError(
                f"tuple literal cannot initialize non-tuple type {expected}"
            )
        if isinstance(expected, ir_types.TupleType) and (
            len(expected.elements) != len(expression.elements)
        ):
            raise SemanticError(
                f"tuple literal has {len(expression.elements)} elements, expected "
                f"{len(expected.elements)} for {expected}"
            )
        typed_elements: list[ir_expr.Expression] = []
        for index, element in enumerate(expression.elements):
            component_expected = (
                expected.elements[index]
                if isinstance(expected, ir_types.TupleType) else None
            )
            typed_elements.append(
                context.expressions.check_typed_boundary(
                    element, inputs, component_expected, context
                )
                if component_expected is not None
                else context.expressions.check(element, inputs, None, context)
            )
        tuple_type = expected or ir_types.TupleType(
            tuple(item.type for item in typed_elements)
        )
        return ir_expr.TupleConstruct(tuple(typed_elements), tuple_type)
    if isinstance(expression, ast.RationalExpr):
        if not isinstance(expected, (ir_types.FixedType, ir_types.UFixedType)):
            raise SemanticError(
                "decimal literal requires a contextual fixed-point type or quantize(...)"
            )
        scaled = expression.numerator << expected.fraction
        raw, remainder = divmod(scaled, expression.denominator)
        if remainder:
            raise SemanticError(
                f"literal {expression.numerator}/{expression.denominator} is not exactly "
                f"representable as {expected}; use quantize(..., rounding_mode)"
            )
        if not expression_operators.constant_fits(raw, expected):
            raise SemanticError(
                f"literal {expression.numerator}/{expression.denominator} is outside "
                f"the range of {expected}; use quantize(...) for explicit overflow handling"
            )
        return ir_expr.Constant(raw, expected)
    if isinstance(expression, ast.TaggedUnionConstructExpr):
        if context.environment.type_resolver is None:
            raise SemanticError("tagged-union constructor requires a type resolver")
        union_type = context.environment.type_resolver.tagged_union_type(
            expression.union_name
        )
        if union_type is None:
            raise SemanticError(
                f"unknown tagged union '{expression.union_name}' in constructor"
            )
        variant = union_type.variant(expression.variant)
        if variant is None:
            raise SemanticError(
                f"tagged union '{union_type.name}' has no variant "
                f"'{expression.variant}'"
            )
        if expected is not None and expected != union_type:
            raise SemanticError(
                f"constructor {union_type.name}.{variant.name} has type "
                f"{union_type}, expected exact {expected}"
            )
        provided_names = tuple(item.name for item in expression.fields)
        duplicate = next(
            (name for name in provided_names if provided_names.count(name) > 1),
            None,
        )
        if duplicate is not None:
            raise SemanticError(
                f"duplicate constructor field '{duplicate}' for "
                f"{union_type.name}.{variant.name}"
            )
        expected_names = tuple(item.name for item in variant.fields)
        missing = tuple(name for name in expected_names if name not in provided_names)
        extra = tuple(name for name in provided_names if name not in expected_names)
        if missing or extra:
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unknown " + ", ".join(extra))
            raise SemanticError(
                f"constructor {union_type.name}.{variant.name} fields are invalid: "
                + "; ".join(details)
            )
        syntax_by_name = {item.name: item for item in expression.fields}
        typed_fields: list[tuple[str, ir_expr.Expression]] = []
        for field_ in variant.fields:
            source = syntax_by_name[field_.name]
            value_syntax = source.expression or ast.NameExpr(field_.name)
            value = context.expressions.check_typed_boundary(
                value_syntax, inputs, field_.type, context
            )
            typed_fields.append((field_.name, value))
        return ir_expr.UnionConstruct(
            variant.name,
            tuple(typed_fields),
            union_type,
            origin=expression.origin,
        )
    if isinstance(expression, ast.TaggedUnionMatchExpr):
        selector = context.expressions.check(expression.selector, inputs, None, context)
        if not isinstance(selector.type, ir_types.TaggedUnionType):
            raise SemanticError(
                f"match selector must be a tagged union, got {selector.type}"
            )
        union_type = selector.type
        seen: set[str] = set()
        typed_branches: list[ir_expr.Expression] = []
        keys: list[int] = []
        result_type = expected
        for arm in expression.arms:
            if arm.union_name != union_type.name:
                raise SemanticError(
                    f"match arm {arm.union_name}.{arm.variant} does not belong to "
                    f"tagged union '{union_type.name}'"
                )
            variant = union_type.variant(arm.variant)
            if variant is None:
                raise SemanticError(
                    f"tagged union '{union_type.name}' has no variant '{arm.variant}'"
                )
            if arm.variant in seen:
                raise SemanticError(
                    f"duplicate match variant {union_type.name}.{arm.variant}"
                )
            seen.add(arm.variant)
            expected_binders = tuple(field.name for field in variant.fields)
            if arm.binders != expected_binders:
                raise SemanticError(
                    f"match arm {union_type.name}.{variant.name} binders must be "
                    f"{expected_binders}, got {arm.binders}"
                )
            shadow = next(
                (
                    name
                    for name in arm.binders
                    if name in inputs or name in context.scope.union_binders
                ),
                None,
            )
            if shadow is not None:
                raise SemanticError(
                    f"tagged-union match binder '{shadow}' shadows an existing symbol"
                )
            binders = dict(context.scope.union_binders)
            for field_ in variant.fields:
                binders[field_.name] = ir_expr.UnionField(
                    selector,
                    variant.name,
                    field_.name,
                    field_.type,
                    origin=arm.origin,
                )
            arm_context = context.with_scope(union_binders=binders)
            branch = (
                context.expressions.check_typed_boundary(
                    arm.expression, inputs, result_type, arm_context
                )
                if result_type is not None
                else context.expressions.check(arm.expression, inputs, None, arm_context)
            )
            if result_type is None:
                result_type = branch.type
            elif branch.type != result_type:
                raise SemanticError(
                    f"match arm {union_type.name}.{variant.name} has type "
                    f"{branch.type}, expected exact {result_type}"
                )
            typed_branches.append(branch)
            keys.append(union_type.tag(variant.name))
        missing = tuple(
            variant.name for variant in union_type.variants
            if variant.name not in seen
        )
        if missing:
            raise SemanticError(
                f"missing tagged-union match variant(s) for '{union_type.name}': "
                + ", ".join(missing)
            )
        assert result_type is not None and typed_branches
        tag = ir_expr.UnionTag(
            selector,
            ir_types.BitsType(union_type.tag_width),
            origin=expression.origin,
        )
        return ir_expr.Switch(
            tag,
            tuple(
                ir_expr.SwitchCase(key, branch)
                for key, branch in zip(keys, typed_branches, strict=True)
            ),
            typed_branches[0],
            result_type,
            origin=expression.origin,
        )
    if isinstance(expression, ast.VectorLiteralExpr):
        if not expression.elements:
            raise SemanticError(
                "empty vector literal has no element type; use a non-empty literal"
            )
        expected_element: ir_types.HardwareType | None = None
        if expected is not None:
            if not isinstance(expected, ir_types.VecType):
                raise SemanticError(
                    f"vector literal cannot initialize non-vector {expected}"
                )
            if expected.length != len(expression.elements):
                raise SemanticError(
                    f"vector literal has {len(expression.elements)} elements, "
                    f"expected {expected.length}"
                )
            expected_element = expected.element_type
        elements: list[ir_expr.Expression] = []
        element_type = expected_element
        for index, syntax in enumerate(expression.elements):
            element = (
                context.expressions.check_typed_boundary(syntax, inputs, element_type, context)
                if element_type is not None
                else context.expressions.check(syntax, inputs, None, context)
            )
            if element_type is None:
                element_type = element.type
            if element.type != element_type:
                raise SemanticError(
                    f"vector literal element {index} has type {element.type}, "
                    f"expected exact {element_type}"
                )
            elements.append(element)
        assert element_type is not None
        type_ = ir_types.VecType(len(elements), element_type)
        return ir_expr.Generate(
            "vector_literal", 0, len(elements), tuple(elements), type_
        )
    if isinstance(expression, ast.StructUpdateExpr):
        base = context.expressions.check(expression.expression, inputs, expected, context)
        if not isinstance(base.type, ir_types.StructType):
            raise SemanticError(
                f"immutable 'with' update requires a nominal struct, got {base.type}"
            )
        if expected is not None and expected != base.type:
            raise SemanticError(
                f"struct update has type {base.type}, expected exact {expected}"
            )
        replacements: dict[str, ast.Expression] = {}
        for field in expression.fields:
            if field.name in replacements:
                raise SemanticError(
                    f"duplicate field '{field.name}' in struct update"
                )
            replacements[field.name] = (
                field.expression
                if field.expression is not None
                else ast.NameExpr(field.name, origin=expression.origin)
            )
        declared = {field.name: field for field in base.type.fields}
        unknown = sorted(set(replacements) - set(declared))
        if unknown:
            raise SemanticError(
                f"struct '{base.type.name}' has no field(s): {', '.join(unknown)}"
            )
        fields: list[tuple[str, ir_expr.Expression]] = []
        for field in base.type.fields:
            value = (
                context.expressions.check_typed_boundary(
                    replacements[field.name], inputs, field.type, context
                )
                if field.name in replacements
                else ir_expr.FieldAccess(base, field.name, field.type)
            )
            if value.type != field.type:
                raise SemanticError(
                    f"field '{field.name}' has type {value.type}, expected {field.type}"
                )
            fields.append((field.name, value))
        return ir_expr.StructConstruct(base.type.name, tuple(fields), base.type)
    return None

def _check_struct_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if isinstance(expression, ast.StructConstructExpr):
        if context.environment.type_resolver is not None:
            type_resolution.record_named_type_definition(
                context,
                ast.TypeName(
                    expression.struct_name,
                    origin=expression.name_origin,
                ),
                context.environment.type_resolver,
            )
        # Resolve field punning before any generic/concrete struct path.  The
        # resulting NameExpr deliberately goes through the normal lexical
        # lookup below, so shorthand has exactly the same diagnostics and
        # typing as an explicit ``field = field`` initializer.
        if any(field.expression is None for field in expression.fields):
            expression = replace(
                expression,
                fields=tuple(
                    replace(
                        field,
                        expression=ast.NameExpr(field.name, origin=expression.origin),
                    )
                    if field.expression is None else field
                    for field in expression.fields
                ),
            )
        # A parameterized struct specialization is commonly known from the
        # destination type even though it is intentionally absent from
        # ``resolve_all()`` (there is no finite set of generic
        # specializations to enumerate).  Use that exact contextual type
        # before consulting the non-generic declaration table.  This keeps
        # construction backend-independent and, importantly, avoids picking
        # an unrelated first specialization of the same generic struct.
        struct = (
            expected
            if isinstance(expected, ir_types.StructType)
            and (
                expected.name == expression.struct_name
                or expected.name.startswith(expression.struct_name + "<")
            )
            else None
        )
        if struct is None:
            struct = next(
                (
                    item
                    for item in context.environment.structs
                    if item.name == expression.struct_name
                ),
                None,
            )
        if struct is None:
            struct = next(
                (
                    item
                    for item in context.environment.structs
                    if item.name.startswith(expression.struct_name + "<")
                ),
                None,
            )
        if struct is None:
            declaration = next(
                (
                    item for item in context.environment.struct_declarations
                    if item.name == expression.struct_name
                ),
                None,
            )
            if declaration is not None and declaration.parameters:
                provided = {field.name: field.expression for field in expression.fields}
                declared_fields = {field.name: field for field in declaration.fields}
                if set(provided) != set(declared_fields):
                    missing = sorted(set(declared_fields) - set(provided))
                    extra = sorted(set(provided) - set(declared_fields))
                    raise SemanticError(
                        f"struct '{declaration.name}' fields mismatch; missing={missing}, extra={extra}"
                    )
                raw_values = {
                    name: context.expressions.check(value, inputs, None, context)
                    for name, value in provided.items()
                }
                parameters = {item.name: item for item in declaration.parameters}
                type_bindings: dict[str, ir_types.HardwareType] = {}
                value_bindings: dict[str, int] = {}
                assert context.environment.type_resolver is not None
                context.services.callable_specializer.bind_inferred_types(
                    parameters,
                    type_bindings,
                    value_bindings,
                    context.environment.type_resolver,
                    tuple(
                        (field.type_name, raw_values[field.name].type)
                        for field in declaration.fields
                    )
                )
                unresolved = [
                    item.name for item in declaration.parameters
                    if item.name not in type_bindings and item.name not in value_bindings
                ]
                if unresolved:
                    raise SemanticError(
                        f"cannot infer struct '{declaration.name}' parameters: {', '.join(unresolved)}"
                    )
                arguments = ",".join(
                    str(type_bindings[item.name]) if item.kind == "type"
                    else str(value_bindings[item.name])
                    for item in declaration.parameters
                )
                struct = context.environment.type_resolver.resolve(
                    ast.TypeName(f"{declaration.name}<{arguments}>")
                )
        # Struct declarations are attached by analyze for this expression context.
        if struct is None:
            raise SemanticError(f"unknown struct '{expression.struct_name}'")
        provided = {field.name: field.expression for field in expression.fields}
        expected_fields = {field.name: field for field in struct.fields}
        if set(provided) != set(expected_fields):
            missing = sorted(set(expected_fields) - set(provided))
            extra = sorted(set(provided) - set(expected_fields))
            raise SemanticError(f"struct '{struct.name}' fields mismatch; missing={missing}, extra={extra}")
        values = []
        for field in struct.fields:
            value = context.expressions.check_typed_boundary(
                provided[field.name], inputs, field.type, context
            )
            if value.type != field.type:
                raise SemanticError(f"field '{field.name}' has type {value.type}, expected {field.type}")
            values.append((field.name, value))
        return ir_expr.StructConstruct(struct.name, tuple(values), struct)
    return None

def _check_representation_expression(
    expression: ast.Expression,
    inputs: dict[str, semantic_symbols.ValueSymbol],
    expected: ir_types.HardwareType | None,
    context: ExpressionContext,
) -> ir_expr.Expression | None:
    if isinstance(expression, ast.ReshapeExpr):
        operand = context.expressions.check(expression.expression, inputs, None, context)
        if expression.target_type is None:
            target_type = expected
            if not isinstance(target_type, ir_types.VecType):
                raise SemanticError(
                    "contextual reshape requires an unambiguous vector target; "
                    "use reshape<vec<...>>(value)"
                )
        else:
            if context.environment.type_resolver is None:
                raise SemanticError("reshape target requires a type resolver")
            target_type = context.environment.type_resolver.resolve(
                expression.target_type
            )
        if not isinstance(operand.type, ir_types.VecType):
            raise SemanticError(
                f"reshape requires a vector source, got {operand.type}"
            )
        if not isinstance(target_type, ir_types.VecType):
            raise SemanticError(
                f"reshape target must be a vector, got {target_type}"
            )
        source_count, source_leaf = ir_functional.vector_leaf_shape(operand.type)
        target_count, target_leaf = ir_functional.vector_leaf_shape(target_type)
        if source_count != target_count:
            raise SemanticError(
                "reshape requires equal leaf count, got "
                f"{source_count} in {operand.type} and {target_count} in {target_type}"
            )
        if source_leaf != target_leaf:
            raise SemanticError(
                "reshape requires exact common leaf type, got "
                f"{source_leaf} and {target_leaf}"
            )
        if (
            expected is not None
            and expected != target_type
            and not can_implicitly_bitcast_types(target_type, expected)
        ):
            raise SemanticError(
                f"reshape produces {target_type}, expected exact {expected}"
            )
        if operand.type == target_type:
            return operand
        return ir_expr.Reshape(operand, target_type)
    if isinstance(expression, ast.PackExpr):
        operand = context.expressions.check(expression.expression, inputs, None, context)
        try:
            width = ir_packing.packed_width(operand.type)
        except ir_packing.PackingError as error:
            raise SemanticError(
                f"pack requires a recursively bit-packable non-enum value, got "
                f"{operand.type}: {error}"
            ) from error
        result_type = ir_types.BitsType(width)
        if (
            expected is not None
            and expected != result_type
            and not can_implicitly_bitcast_types(result_type, expected)
        ):
            raise SemanticError(
                f"pack produces {result_type}, expected exact {expected}"
            )
        return make_bitcast(operand, result_type, description="pack")
    if isinstance(expression, ast.UnpackExpr):
        if context.environment.type_resolver is None:
            raise SemanticError("unpack target requires a type resolver")
        target_type = context.environment.type_resolver.resolve(expression.target_type)
        try:
            width = ir_packing.packed_width(target_type)
        except ir_packing.PackingError as error:
            raise SemanticError(
                f"unpack target must be recursively bit-packable and non-enum, "
                f"got {target_type}: {error}"
            ) from error
        source_type = ir_types.BitsType(width)
        operand = context.expressions.check(
            expression.expression, inputs, source_type, context
        )
        if operand.type != source_type:
            raise SemanticError(
                f"unpack<{target_type}> requires exact {source_type} source, "
                f"got {operand.type}"
            )
        if (
            expected is not None
            and expected != target_type
            and not can_implicitly_bitcast_types(target_type, expected)
        ):
            raise SemanticError(
                f"unpack<{target_type}> produces {target_type}, expected exact {expected}"
            )
        return make_bitcast(
            operand, target_type, description=f"unpack<{target_type}>"
        )
    if isinstance(expression, ast.ResizeExpr):
        operand = context.expressions.check(expression.expression, inputs, None, context)
        width = expression.width
        contextual = width is None
        if contextual:
            if expected is None:
                raise SemanticError(
                    f"contextual {expression.kind.value}(...) requires an explicit "
                    "typed assignment, storage, argument, or return boundary"
                )
            if isinstance(expected, (ir_types.EnumType, ir_types.StructType, ir_types.TupleType, ir_types.VecType)):
                raise SemanticError(
                    f"contextual {expression.kind.value}(...) requires a scalar "
                    f"integer/fixed/raw target, got {expected}"
                )
            width = expected.width
        if isinstance(width, str):
            if width not in context.environment.parameters:
                raise SemanticError(
                    f"resize width '{width}' is not a constant module parameter"
                )
            width = context.environment.parameters[width]
        if isinstance(operand.type, ir_types.EnumType):
            raise SemanticError(
                f"cannot resize enum '{operand.type.name}'; implicit enum "
                "resizing is forbidden"
            )
        if isinstance(operand.type, ir_types.BitType):
            raise SemanticError(
                f"{expression.kind.value} is not defined for bit; use bits<1> "
                "for a resizable vector"
            )
        if expression.kind is ast.ResizeKind.EXTEND:
            if width < operand.type.width:
                raise SemanticError(
                    f"cannot extend {operand.type} to width {width}; "
                    "use truncate for a narrower result"
                )
            result_type = _resized_type(operand.type, width)
            if contextual and result_type != expected:
                raise SemanticError(
                    f"contextual extend of {operand.type} produces {result_type}, "
                    f"but the typed boundary requires exact {expected}"
                )
            if isinstance(operand, ir_expr.Constant):
                return ir_expr.Constant(operand.value, result_type)
            return ir_expr.Extend(operand, result_type)
        if width > operand.type.width:
            raise SemanticError(
                f"cannot truncate {operand.type} to width {width}; "
                "use extend for a wider result"
            )
        result_type = _resized_type(operand.type, width)
        if contextual and result_type != expected:
            raise SemanticError(
                f"contextual truncate of {operand.type} produces {result_type}, "
                f"but the typed boundary requires exact {expected}"
            )
        if isinstance(operand, ir_expr.Constant):
            return ir_expr.Constant(runtime_values.normalize_scalar(operand.value, result_type), result_type)
        return ir_expr.Truncate(operand, result_type)
    if isinstance(expression, ast.MuxExpr):
        condition = context.expressions.check(
            expression.condition, inputs, ir_types.BitType(), context
        )
        if not isinstance(condition.type, ir_types.BitType):
            raise SemanticError(f"mux condition must be bit, got {condition.type}")
        branches, result_type = _check_alternatives(
            (expression.when_true, expression.when_false),
            inputs,
            expected,
            "mux branch",
            context,
        )
        if isinstance(condition, ir_expr.Constant):
            return branches[0] if condition.value else branches[1]
        if context.scope.functional_symbolic_values and branches[0] == branches[1]:
            return branches[0]
        return ir_expr.Mux(condition, branches[0], branches[1], result_type)
    return None
