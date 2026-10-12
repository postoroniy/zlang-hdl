# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Compiler-owned operator validation, resolution, typing, and folding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zlang.ast import nodes as ast
from zlang.ir import expressions as ir_expr
from zlang.ir import numeric as ir_numeric
from zlang.ir import runtime_values
from zlang.ir import types as ir_types
from zlang.source import SourceOrigin

from . import type_resolution
from .errors import SemanticError

if TYPE_CHECKING:
    from .context import ExpressionContext


def build_aggregate_equality(
    left: ir_expr.Expression,
    right: ir_expr.Expression,
) -> ir_expr.Expression:
    """Lower exact aggregate equality to ordinary typed scalar comparisons."""

    if left.type != right.type:
        raise SemanticError(
            f"aggregate equality requires one exact type, got {left.type} and {right.type}"
        )
    comparisons: list[ir_expr.Expression] = []
    if isinstance(left.type, ir_types.StructType):
        for field in left.type.fields:
            left_item = ir_expr.FieldAccess(left, field.name, field.type)
            right_item = ir_expr.FieldAccess(right, field.name, field.type)
            comparisons.append(
                build_aggregate_equality(left_item, right_item)
                if isinstance(field.type, (ir_types.StructType, ir_types.TupleType, ir_types.VecType))
                else build_binary(
                    ir_expr.BinaryOperator.EQUAL, left_item, right_item
                )
            )
    elif isinstance(left.type, ir_types.TupleType):
        for index, element_type in enumerate(left.type.elements):
            left_item = ir_expr.TupleProject(left, index, element_type)
            right_item = ir_expr.TupleProject(right, index, element_type)
            comparisons.append(
                build_aggregate_equality(left_item, right_item)
                if isinstance(element_type, (ir_types.StructType, ir_types.TupleType, ir_types.VecType))
                else build_binary(
                    ir_expr.BinaryOperator.EQUAL, left_item, right_item
                )
            )
    elif isinstance(left.type, ir_types.VecType):
        for index in range(left.type.length):
            left_item = ir_expr.VectorIndex(left, index, left.type.element_type)
            right_item = ir_expr.VectorIndex(right, index, right.type.element_type)
            comparisons.append(
                build_aggregate_equality(left_item, right_item)
                if isinstance(left.type.element_type, (ir_types.StructType, ir_types.TupleType, ir_types.VecType))
                else build_binary(
                    ir_expr.BinaryOperator.EQUAL, left_item, right_item
                )
            )
    else:
        return build_binary(ir_expr.BinaryOperator.EQUAL, left, right)
    result: ir_expr.Expression = ir_expr.Constant(1, ir_types.BitType())
    for comparison in comparisons:
        result = build_binary(
            ir_expr.BinaryOperator.BIT_AND, result, comparison
        )
    return result


def _outer_nominal_name(syntax: ast.TypeSyntax) -> str | None:
    if not isinstance(syntax, ast.TypeName):
        return None
    generic = type_resolution.TypeResolver._generic_parts(syntax.text)
    return generic[0] if generic is not None else syntax.text


def validate_operator_declarations(
    declarations: tuple[ast.OperatorDecl, ...],
    structs: tuple[ast.StructDecl, ...],
) -> None:
    struct_by_name = {declaration.name: declaration for declaration in structs}
    struct_names = set(struct_by_name)
    generic_signatures: dict[tuple[str, str, int], list[ast.OperatorDecl]] = {}

    def patterns_overlap(left: ast.OperatorDecl, right: ast.OperatorDecl) -> bool:
        left_names = {item.name for item in left.generic_parameters}
        right_names = {item.name for item in right.generic_parameters}

        def pattern(syntax: ast.TypeSyntax, names: set[str], side: str) -> object:
            if isinstance(syntax, ast.VectorTypeName):
                length: object = (
                    ("var", side, syntax.length)
                    if isinstance(syntax.length, str) and syntax.length in names
                    else ("const", str(syntax.length))
                )
                return ("vec", length, pattern(syntax.element_type, names, side))
            if isinstance(syntax, ast.TupleTypeName):
                return (
                    "tuple",
                    *(pattern(item, names, side) for item in syntax.elements),
                )
            if syntax.text in names:
                return ("var", side, syntax.text)
            generic = type_resolution.TypeResolver._generic_parts(syntax.text)
            if generic is None:
                return ("type", syntax.text)
            base, arguments = generic
            return (
                "type", base,
                *(pattern(ast.TypeName(item), names, side) for item in arguments),
            )

        substitutions: dict[object, object] = {}

        def dereference(value: object) -> object:
            while isinstance(value, tuple) and value[:1] == ("var",) and value in substitutions:
                value = substitutions[value]
            return value

        def unify(a: object, b: object) -> bool:
            a, b = dereference(a), dereference(b)
            if a == b:
                return True
            if isinstance(a, tuple) and a[:1] == ("var",):
                substitutions[a] = b
                return True
            if isinstance(b, tuple) and b[:1] == ("var",):
                substitutions[b] = a
                return True
            if not isinstance(a, tuple) or not isinstance(b, tuple):
                return False
            return len(a) == len(b) and a[0] == b[0] and all(
                unify(x, y) for x, y in zip(a[1:], b[1:], strict=True)
            )

        return all(
            unify(
                pattern(a.type_name, left_names, "left"),
                pattern(b.type_name, right_names, "right"),
            )
            for a, b in zip(left.parameters, right.parameters, strict=True)
        )
    for declaration in declarations:
        if declaration.operator not in {"+", "-", "*"}:
            raise SemanticError(f"unsupported operator overload '{declaration.operator}'")
        expected_arity = 1 if len(declaration.parameters) == 1 else 2
        if len(declaration.parameters) not in {1, 2} or (
            len(declaration.parameters) == 1 and declaration.operator != "-"
        ):
            raise SemanticError(
                f"operator '{declaration.operator}' has unsupported arity {len(declaration.parameters)}"
            )
        del expected_arity
        owners = tuple(
            _outer_nominal_name(parameter.type_name)
            for parameter in declaration.parameters
        )
        if not any(owner in struct_names for owner in owners):
            raise SemanticError(
                f"operator '{declaration.operator}' must be owned by a nominal struct operand; "
                "built-in scalar and fixed operators cannot be overloaded"
            )
        first_owner = next(owner for owner in owners if owner in struct_names)
        owner_declaration = struct_by_name[first_owner]
        if declaration.source_identity != owner_declaration.source_identity:
            raise SemanticError(
                f"operator '{declaration.operator}' violates nominal-owner coherence: "
                f"'{first_owner}' is owned by {owner_declaration.source_identity or 'this source'}"
            )
        if declaration.generic_parameters:
            key = (first_owner, declaration.operator, len(declaration.parameters))
            previous = next(
                (
                    item for item in generic_signatures.get(key, ())
                    if patterns_overlap(item, declaration)
                ),
                None,
            )
            if previous is not None:
                raise SemanticError(
                    f"overlapping generic operator '{declaration.operator}' declarations "
                    f"for owner '{first_owner}'"
                )
            generic_signatures.setdefault(key, []).append(declaration)


def resolve_operator(
    symbol: str,
    arguments: tuple[ir_expr.Expression, ...],
    context: ExpressionContext,
    *,
    call_origin: SourceOrigin | None = None,
) -> ir_expr.Expression:
    struct_names = {
        declaration.name
        for declaration in context.environment.struct_declarations
    }
    candidates: list[tuple[int, ast.OperatorDecl]] = []
    diagnostics: list[str] = []
    rejected: list[ast.OperatorDecl] = []
    for declaration in context.environment.operator_declarations:
        if declaration.operator != symbol or len(declaration.parameters) != len(arguments):
            continue
        owners = [
            _outer_nominal_name(parameter.type_name)
            for parameter in declaration.parameters
        ]
        if not any(owner in struct_names for owner in owners):
            raise SemanticError(
                f"operator '{symbol}' must be owned by at least one nominal operand type"
            )
        try:
            context.services.callable_specializer.bindings(
                declaration,
                (),
                arguments,
                context,
            )
        except SemanticError as error:
            diagnostics.append(str(error))
            rejected.append(declaration)
            continue
        rank = 1 if declaration.generic_parameters else 2
        candidates.append((rank, declaration))
    if not candidates:
        detail = f"; candidates rejected: {diagnostics[0]}" if diagnostics else ""
        rendered = ", ".join(str(argument.type) for argument in arguments)
        error = SemanticError(
            f"no exact overload for operator '{symbol}' with ({rendered}){detail}"
        )
        if rejected:
            raise context.services.callable_specializer.annotate_error(
                error,
                rejected[0],
                context,
                call_origin,
            )
        raise error
    best_rank = max(rank for rank, _ in candidates)
    best = [declaration for rank, declaration in candidates if rank == best_rank]
    if len(best) != 1:
        raise SemanticError(
            f"ambiguous operator '{symbol}' overload for "
            f"({', '.join(str(argument.type) for argument in arguments)})"
        )
    return context.services.callable_specializer.specialize(
        best[0],
        arguments,
        (),
        context,
        call_origin=call_origin,
    )


def build_binary(
    operator: ir_expr.BinaryOperator,
    left: ir_expr.Expression,
    right: ir_expr.Expression,
) -> ir_expr.Expression:
    if isinstance(left.type, ir_types.EnumType) or isinstance(right.type, ir_types.EnumType):
        if operator in {
            ir_expr.BinaryOperator.LESS,
            ir_expr.BinaryOperator.LESS_EQUAL,
            ir_expr.BinaryOperator.GREATER,
            ir_expr.BinaryOperator.GREATER_EQUAL,
        }:
            raise SemanticError(
                "ordered comparison is not defined for enum values"
            )
        if operator not in {
            ir_expr.BinaryOperator.EQUAL,
            ir_expr.BinaryOperator.NOT_EQUAL,
        }:
            raise SemanticError("arithmetic is not defined for enum values")
    if operator is ir_expr.BinaryOperator.SUBTRACT:
        try:
            rule = ir_numeric.subtraction_rule(left.type, right.type)
        except ir_numeric.NumericTypeError as error:
            if error.reason is ir_numeric.NumericTypeErrorReason.FRACTION_MISMATCH:
                raise SemanticError(
                    "fixed-point subtraction requires identical fractional widths; "
                    "use explicit quantize/rescale before the operator"
                ) from error
            raise SemanticError(
                f"subtraction requires matching integer families, got "
                f"{left.type} and {right.type}"
            ) from error
        operand_type = rule.operand_type
        result_type = rule.result_type
    elif operator is ir_expr.BinaryOperator.MULTIPLY:
        try:
            rule = ir_numeric.multiplication_rule(left.type, right.type)
        except ir_numeric.NumericTypeError as error:
            raise SemanticError(
                f"multiplication requires matching integer families, got "
                f"{left.type} and {right.type}"
            ) from error
        operand_type = rule.operand_type
        result_type = rule.result_type
    elif operator in {
        ir_expr.BinaryOperator.BIT_AND,
        ir_expr.BinaryOperator.BIT_OR,
        ir_expr.BinaryOperator.BIT_XOR,
    }:
        try:
            rule = ir_numeric.bitwise_rule(left.type, right.type)
        except ir_numeric.NumericTypeError as error:
            if error.reason is ir_numeric.NumericTypeErrorReason.FAMILY_MISMATCH:
                raise SemanticError(
                    f"bitwise operands require matching type families, got "
                    f"{left.type} and {right.type}"
                ) from error
            raise SemanticError(
                f"bitwise operation is not defined for {left.type}"
            ) from error
        operand_type = rule.operand_type
        result_type = rule.result_type
    elif operator in {
        ir_expr.BinaryOperator.SHIFT_LEFT,
        ir_expr.BinaryOperator.SHIFT_RIGHT,
    }:
        if not isinstance(left.type, (ir_types.UIntType, ir_types.SIntType, ir_types.BitsType)):
            raise SemanticError(f"shift is not defined for {left.type}")
        if not isinstance(right.type, ir_types.UIntType):
            raise SemanticError(f"shift amount must be unsigned, got {right.type}")
        operand_type = left.type
        result_type = left.type
    else:
        equality = operator in {
            ir_expr.BinaryOperator.EQUAL,
            ir_expr.BinaryOperator.NOT_EQUAL,
        }
        try:
            rule = ir_numeric.comparison_rule(
                left.type,
                right.type,
                equality=equality,
            )
        except ir_numeric.NumericTypeError as error:
            if error.reason is ir_numeric.NumericTypeErrorReason.FAMILY_MISMATCH:
                message = (
                    f"comparison requires matching type families, got "
                    f"{left.type} and {right.type}"
                )
            elif error.reason is ir_numeric.NumericTypeErrorReason.NOMINAL_ENUM_MISMATCH:
                message = (
                    f"enum comparison requires matching nominal enum types, got "
                    f"{left.type} and {right.type}"
                )
            elif error.reason is ir_numeric.NumericTypeErrorReason.ORDERED_ENUM:
                assert isinstance(left.type, ir_types.EnumType)
                message = (
                    f"ordered comparison is not defined for enum "
                    f"'{left.type.name}'"
                )
            elif error.reason is ir_numeric.NumericTypeErrorReason.ORDERED_BIT:
                message = "ordered comparison is not defined for bit"
            elif error.reason is ir_numeric.NumericTypeErrorReason.ORDERED_BITS:
                message = "ordered comparison is not defined for bit vectors"
            elif error.reason is ir_numeric.NumericTypeErrorReason.FRACTION_MISMATCH:
                message = (
                    "fixed-point comparison requires identical fractional widths"
                )
            else:
                message = f"comparison is not defined for {left.type}"
            raise SemanticError(message) from error
        operand_type = rule.operand_type
        result_type = rule.result_type

    if isinstance(left, ir_expr.Constant) and isinstance(right, ir_expr.Constant):
        value = _evaluate_constant_binary(operator, left.value, right.value, result_type)
        return ir_expr.Constant(value, result_type)
    return ir_expr.Binary(operator, left, right, operand_type, result_type)

def _evaluate_constant_binary(
    operator: ir_expr.BinaryOperator,
    left: int,
    right: int,
    result_type: ir_types.HardwareType,
) -> int:
    if operator is ir_expr.BinaryOperator.SUBTRACT:
        value = left - right
    elif operator is ir_expr.BinaryOperator.MULTIPLY:
        value = left * right
    elif operator is ir_expr.BinaryOperator.BIT_AND:
        value = left & right
    elif operator is ir_expr.BinaryOperator.BIT_OR:
        value = left | right
    elif operator is ir_expr.BinaryOperator.BIT_XOR:
        value = left ^ right
    elif operator is ir_expr.BinaryOperator.SHIFT_LEFT:
        value = left << right
    elif operator is ir_expr.BinaryOperator.SHIFT_RIGHT:
        value = left >> right
    elif operator is ir_expr.BinaryOperator.EQUAL:
        return int(left == right)
    elif operator is ir_expr.BinaryOperator.NOT_EQUAL:
        return int(left != right)
    elif operator is ir_expr.BinaryOperator.LESS:
        return int(left < right)
    elif operator is ir_expr.BinaryOperator.LESS_EQUAL:
        return int(left <= right)
    elif operator is ir_expr.BinaryOperator.GREATER:
        return int(left > right)
    elif operator is ir_expr.BinaryOperator.GREATER_EQUAL:
        return int(left >= right)
    else:  # Defensive guard for future operators.
        raise SemanticError(f"cannot fold operator {operator.value}")
    return runtime_values.normalize_scalar(value, result_type)


def constant_fits(value: int, type_: ir_types.HardwareType) -> bool:
    # Enum constants have their own nominal-member/decode paths.  Preserve the
    # existing literal boundary, which accepted only numeric scalar families.
    return not isinstance(type_, ir_types.EnumType) and runtime_values.scalar_fits(value, type_)
