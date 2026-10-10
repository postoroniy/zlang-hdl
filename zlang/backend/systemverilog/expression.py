# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Authoritative typed-expression SystemVerilog renderer."""

from __future__ import annotations

from contextvars import ContextVar
import re

from zlang.ir import expressions as expr
from zlang.ir import packing as ir_packing
from zlang.ir import interfaces as ir_interfaces
from zlang.ir import storage as ir_storage
from zlang.ir import types as ir_types
from zlang.backend.expression_constant_folding import BackendConstantFolder
from zlang.backend.systemverilog import context as emission_context
from zlang.backend.systemverilog.syntax import sized_decimal as _sized_decimal
from zlang.common.systemverilog import (
    render_ordered_comparison,
    render_right_shift,
    render_typed_resize,
)
from zlang.fixed_point import quantize_rational
from zlang.ir import functional as ir_functional
from zlang.ir import functional_regions as functional_regions


from zlang.backend.systemverilog.errors import SystemVerilogEmissionError
from zlang.backend.systemverilog import rendering as sv_rendering


_CURRENT_CONSTANT_FOLDER: ContextVar[BackendConstantFolder | None] = ContextVar(
    "zlang_systemverilog_constant_folder", default=None,
)


def _fold_for_render(expression: expr.Expression) -> expr.Expression:
    folder = _CURRENT_CONSTANT_FOLDER.get()
    if folder is not None:
        return folder.fold(expression)
    folder = BackendConstantFolder()
    token = _CURRENT_CONSTANT_FOLDER.set(folder)
    try:
        return folder.fold(expression)
    finally:
        _CURRENT_CONSTANT_FOLDER.reset(token)


_CURRENT_RENDER_MEMO: ContextVar[
    dict[int, tuple[expr.Expression, str]] | None
] = ContextVar("zlang_systemverilog_expression_render_memo", default=None)

def _compile_time_expression(expression: int | functional_regions.CompileTimeExpr) -> str:
    """Render exact binder arithmetic using Python-compatible floor semantics."""

    if isinstance(expression, int) and not isinstance(expression, bool):
        return str(expression)
    if not isinstance(expression, functional_regions.CompileTimeExpr):
        raise SystemVerilogEmissionError(
            "functional compile-time value is not an integer expression"
        )
    operator = expression.operator
    if operator is functional_regions.CompileTimeOperator.LITERAL:
        value = expression.operands[0]
        assert isinstance(value, int)
        return str(value)
    if operator is functional_regions.CompileTimeOperator.BINDER:
        binder = expression.operands[0]
        context = emission_context.current_functional_expression()
        rendered = None if context is None else context.binder(binder.identity)
        if rendered is None:
            raise SystemVerilogEmissionError(
                f"functional binder '{binder.display_name}' escaped its region"
            )
        return rendered
    operands = tuple(
        _compile_time_expression(
            functional_regions.CompileTimeExpr.ref(item)
            if isinstance(item, functional_regions.CompileTimeBinderRef)
            else item
        )
        for item in expression.operands
    )
    if operator is functional_regions.CompileTimeOperator.ADD:
        return f"(({operands[0]}) + ({operands[1]}))"
    if operator is functional_regions.CompileTimeOperator.SUBTRACT:
        return f"(({operands[0]}) - ({operands[1]}))"
    if operator is functional_regions.CompileTimeOperator.MULTIPLY:
        return f"(({operands[0]}) * ({operands[1]}))"
    if operator is functional_regions.CompileTimeOperator.NEGATE:
        return f"(-({operands[0]}))"
    quotient = f"(({operands[0]}) / ({operands[1]}))"
    remainder = f"(({operands[0]}) % ({operands[1]}))"
    floor = (
        f"({quotient} - ((({remainder}) != 0 && "
        f"((({operands[0]}) < 0) != (({operands[1]}) < 0))) ? 1 : 0))"
    )
    if operator is functional_regions.CompileTimeOperator.FLOOR_DIVIDE:
        return floor
    assert operator is functional_regions.CompileTimeOperator.MODULO
    return f"(({operands[0]}) - (({floor}) * ({operands[1]})))"


def _compile_time_integer(expression: int | functional_regions.CompileTimeExpr) -> int | None:
    """Evaluate only a binder expression fixed by the current emit context."""

    if isinstance(expression, int) and not isinstance(expression, bool):
        return expression
    if not isinstance(expression, functional_regions.CompileTimeExpr):
        return None
    context = emission_context.current_functional_expression()
    if context is None:
        return None
    bindings = {
        identity: int(rendered)
        for identity, rendered in context.binders
        if re.fullmatch(r"-?[0-9]+", rendered) is not None
    }
    try:
        return functional_regions.evaluate_compile_time(expression, bindings)
    except ValueError:
        return None


def _constant_vector_projection(
    expression: expr.VectorIndex | expr.RuntimeIndex,
) -> tuple[expr.Expression, int, int] | None:
    """Collapse a compiler-fixed nested vector path to one packed slice."""

    vector = expression.expression.type
    if not isinstance(vector, ir_types.VecType):
        return None
    index = (
        _compile_time_integer(expression.index.expression)
        if isinstance(expression, expr.RuntimeIndex)
        and isinstance(expression.index, expr.FunctionalValue)
        else (
            expression.index.value
            if isinstance(expression, expr.RuntimeIndex)
            and isinstance(expression.index, expr.Constant)
            else (
                _compile_time_integer(expression.index)
                if isinstance(expression, expr.VectorIndex)
                else None
            )
        )
    )
    if index is None or not 0 <= index < vector.length:
        return None
    width = sv_rendering._width(vector.element_type)
    lsb = ir_packing.vector_element_lsb(vector, index)
    if isinstance(expression.expression, (expr.VectorIndex, expr.RuntimeIndex)):
        parent = _constant_vector_projection(expression.expression)
        if parent is not None:
            root, parent_lsb, _parent_width = parent
            return root, parent_lsb + lsb, width
    return expression.expression, lsb, width


def _runtime_struct_field_projection(expression: expr.FieldAccess) -> str:
    """Render one demanded field without selecting the complete vector element."""

    indexed = expression.expression
    assert isinstance(indexed, expr.RuntimeIndex)
    vector = indexed.expression.type
    assert isinstance(vector, ir_types.VecType)
    assert isinstance(vector.element_type, ir_types.StructType)
    rendered_vector = _expression(indexed.expression)
    rendered_index = _expression(indexed.index)
    if sv_rendering._uses_native_vector_indexing(indexed.expression):
        return sv_rendering._struct_field_expression(
            f"{rendered_vector}[{rendered_index}]",
            vector.element_type,
            expression.field,
        )
    return sv_rendering._packed_indexed_slice(
        rendered_vector,
        rendered_index,
        sv_rendering._width(vector.element_type),
        field_lsb=ir_packing.struct_field_lsb(vector.element_type, expression.field),
        width=sv_rendering._width(expression.type),
    )


def _vector_index_expression(
    expression: expr.VectorIndex | expr.RuntimeIndex,
) -> str:
    vector = expression.expression.type
    if not isinstance(vector, ir_types.VecType):
        raise SystemVerilogEmissionError("vector index requires a vector")
    rendered_vector = _expression(expression.expression)
    if isinstance(expression, expr.RuntimeIndex):
        rendered_index = _expression(expression.index)
    else:
        rendered_index = (
            str(expression.index)
            if isinstance(expression.index, int)
            else _compile_time_expression(expression.index)
        )
    if sv_rendering._uses_native_vector_indexing(expression.expression):
        return f"{rendered_vector}[{rendered_index}]"
    projection = _constant_vector_projection(expression)
    if projection is not None:
        root, lsb, width = projection
        return sv_rendering._slice(_expression(root), lsb + width - 1, lsb)
    element_width = sv_rendering._width(vector.element_type)
    if isinstance(expression, expr.VectorIndex) and isinstance(expression.index, int):
        lsb = ir_packing.vector_element_lsb(vector, expression.index)
        return sv_rendering._slice(rendered_vector, lsb + element_width - 1, lsb)
    return sv_rendering._packed_indexed_slice(
        rendered_vector, rendered_index, element_width
    )


def _fixed_convert(expression: expr.FixedConvert) -> str:
    rendered = _expression(expression.expression)
    if expression.kind in {expr.FixedConversionKind.FROM_RAW, expr.FixedConversionKind.TO_RAW}:
        return rendered
    target_signed = isinstance(expression.type, ir_types.FixedType)
    if expression.rational_denominator is not None:
        assert isinstance(expression.expression, expr.Constant)
        value = quantize_rational(
            expression.expression.value,
            expression.rational_denominator,
            fraction=expression.type.fraction,
            width=expression.type.width,
            signed=target_signed,
            rounding=expression.rounding,
            overflow=expression.overflow,
        )
        return _sized_decimal(
            expression.type.width,
            value,
            signed=target_signed,
        )
    source_fraction = getattr(expression.expression.type, "fraction", 0)
    delta = expression.type.fraction - source_fraction
    source_signed = isinstance(expression.expression.type, (ir_types.SIntType, ir_types.FixedType))
    source_width = sv_rendering._width(expression.expression.type)
    work_width = max(
        source_width + max(delta, 0) + 1,
        expression.type.width + 1,
    )
    value = (
        f"$signed({work_width}'($signed({rendered})))"
        if source_signed
        else f"$unsigned({work_width}'($unsigned({rendered})))"
    )
    if delta > 0:
        converted = f"({value} <<< {delta})"
    elif delta == 0:
        converted = value
    else:
        shift = -delta
        def literal(value: int) -> str:
            return f"{work_width}'d{value}"

        magnitude = (f"(({value}) < 0 ? -({value}) : ({value}))"
                     if source_signed else f"({value})")
        quotient = f"({magnitude} >> {shift})"
        discarded = (
            f"(({magnitude} & {literal((1 << shift) - 1)}) "
            f"!= {literal(0)})"
        )
        discarded_value = f"{work_width}'({discarded})"
        if expression.rounding is expr.FixedRounding.NEAREST_EVEN:
            rounded = (
                f"(({magnitude} + {literal((1 << (shift - 1)) - 1)} + "
                f"(({magnitude} >> {shift}) & {literal(1)})) >> {shift})"
            )
            converted = (
                f"(({value}) < 0 ? -({rounded}) : ({rounded}))"
                if source_signed else rounded
            )
        elif expression.rounding is expr.FixedRounding.AWAY_ZERO:
            rounded = f"({quotient} + {discarded_value})"
            converted = (
                f"(({value}) < 0 ? -({rounded}) : ({rounded}))"
                if source_signed else rounded
            )
        elif expression.rounding is expr.FixedRounding.FLOOR and source_signed:
            converted = (
                f"(({value}) < 0 ? "
                f"-({quotient} + {discarded_value}) : {quotient})"
            )
        else:
            converted = (
                f"(({value}) < 0 ? -({quotient}) : ({quotient}))"
                if source_signed else quotient
            )
    if expression.overflow is expr.FixedOverflow.SATURATE:
        minimum = -(1 << (expression.type.width - 1)) if target_signed else 0
        maximum = ((1 << (expression.type.width - 1)) - 1 if target_signed
                   else (1 << expression.type.width) - 1)
        converted = (
            f"$signed({work_width}'({converted}))"
            if target_signed
            else f"$unsigned({work_width}'({converted}))"
        )
        minimum_literal = (
            f"-{work_width}'sd{-minimum}"
            if minimum < 0 else f"{work_width}'d{minimum}"
        )
        maximum_literal = (
            f"{work_width}'sd{maximum}"
            if target_signed else f"{work_width}'d{maximum}"
        )
        converted = (
            f"(({converted}) < {minimum_literal} ? {minimum_literal} : "
            f"(({converted}) > {maximum_literal} ? {maximum_literal} : "
            f"({converted})))"
        )
    sized = f"{expression.type.width}'({converted})"
    return f"$signed({sized})" if target_signed else sized


def _expression(expression: expr.Expression) -> str:
    folded = _fold_for_render(expression)
    expression = folded
    memo = _CURRENT_RENDER_MEMO.get()
    if memo is not None:
        cached = memo.get(id(expression))
        if cached is not None and cached[0] is expression:
            return cached[1]
        rendered = _render_expression(expression)
        memo[id(expression)] = (expression, rendered)
        return rendered

    memo = {}
    token = _CURRENT_RENDER_MEMO.set(memo)
    try:
        rendered = _render_expression(expression)
        memo[id(expression)] = (expression, rendered)
        return rendered
    finally:
        _CURRENT_RENDER_MEMO.reset(token)


def render_expression(expression: expr.Expression) -> str:
    """Render one already-typed expression through the production SV spelling.

    Formal harnesses use this narrow entry point so arithmetic widths and
    signedness are never reconstructed independently from the backend.  Module
    emission still owns names, materialization, and statement placement.
    """

    return _expression(expression)


def _render_expression(expression: expr.Expression) -> str:
    if isinstance(expression, (expr.InputRef, expr.ParameterRef, expr.RegisterRef)):
        return sv_rendering._identifier(expression.name)
    if isinstance(expression, expr.InstanceOutputRef):
        raise SystemVerilogEmissionError(
            "instance output requires its containing module naming plan",
            code="ZL-BACKEND-SYSTEMVERILOG-NAMING-CONTEXT",
            semantic_path=(expression.instance, expression.port),
            primary=expression.origin,
        )
    if isinstance(expression, expr.ReadyValidRef):
        prefix = sv_rendering._identifier(expression.interface)
        if expression.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            return f"({prefix}_valid && {prefix}_ready)"
        return f"{prefix}_{expression.signal.value}"
    if isinstance(expression, expr.CreditRef):
        signal = (
            ir_interfaces.CreditSignal.SEND
            if expression.signal is ir_interfaces.CreditSignal.TRANSFER
            else expression.signal
        )
        return f"{sv_rendering._identifier(expression.interface)}_{signal.value}"
    if isinstance(expression, expr.RequestResponseRef):
        prefix = f"{sv_rendering._identifier(expression.interface)}_{expression.channel.value}"
        if expression.signal is ir_interfaces.ReadyValidSignal.TRANSFER:
            return f"({prefix}_valid && {prefix}_ready)"
        return (
            f"{prefix}_"
            f"{expression.signal.value}"
        )
    if isinstance(expression, expr.MemoryRef):
        if expression.port is not None:
            return (
                f"{sv_rendering._identifier(expression.memory)}_"
                f"{sv_rendering._identifier(expression.port)}_{expression.signal.value}"
            )
        return f"{sv_rendering._identifier(expression.memory)}_{expression.signal.value}"
    if isinstance(expression, expr.RomRef):
        if expression.signal is not ir_storage.RomSignal.READ_DATA:
            raise SystemVerilogEmissionError(
                "ROM read_address is a driven storage input, not a readable value"
            )
        return f"{sv_rendering._identifier(expression.rom)}_read_data"
    if isinstance(expression, expr.FifoRef):
        return f"{sv_rendering._identifier(expression.fifo)}_{expression.signal.value}"
    if isinstance(expression, expr.Constant):
        return _sized_decimal(
            sv_rendering._width(expression.type),
            expression.value,
            signed=isinstance(expression.type, (ir_types.SIntType, ir_types.FixedType)),
        )
    if isinstance(expression, expr.FunctionalCaptureRef):
        context = emission_context.current_functional_expression()
        captured = None if context is None else context.capture(expression.identity)
        if captured is None:
            raise SystemVerilogEmissionError(
                f"functional capture '{expression.display_name}' escaped its region"
            )
        return _expression(captured)
    if isinstance(expression, expr.FunctionalValue):
        value = _compile_time_expression(expression.expression)
        return f"{sv_rendering._width(expression.type)}'($unsigned({value}))"
    if isinstance(expression, expr.FunctionalTableLookup):
        context = emission_context.current_functional_expression()
        temporary = None if context is None else context.table_temporary(expression)
        if temporary is None:
            raise SystemVerilogEmissionError(
                f"functional table lookup '{expression.table_name}' escaped its region"
            )
        return temporary
    if isinstance(expression, expr.EnumEncode):
        width = expression.type.width
        return f"{width}'($unsigned({_expression(expression.expression)}))"
    if isinstance(expression, expr.EnumValid):
        value = _expression(expression.expression)
        width = expression.expression.type.width
        return "(" + " || ".join(
            f"(({value}) == {width}'d{code})"
            for code in expression.enum_type.codes
        ) + ")"
    if isinstance(expression, expr.EnumDecode):
        value = _expression(expression.expression)
        width = expression.type.width
        valid = " || ".join(
            f"(({value}) == {width}'d{code})"
            for code in expression.type.codes
        )
        fallback = _expression(expression.fallback)
        return f"(({valid}) ? {width}'($unsigned({value})) : ({fallback}))"
    if isinstance(expression, expr.UnionConstruct):
        union_type = expression.type
        tag = _sized_decimal(
            union_type.tag_width,
            union_type.tag(expression.variant),
            signed=False,
        )
        parts = [tag, *(_expression(value) for _, value in expression.fields)]
        variant = union_type.variant(expression.variant)
        assert variant is not None
        padding = union_type.payload_width - variant.payload_width
        if padding:
            parts.append(_sized_decimal(padding, 0, signed=False))
        return "{" + ", ".join(parts) + "}"
    if isinstance(expression, expr.UnionTag):
        union_type = expression.expression.type
        assert isinstance(union_type, ir_types.TaggedUnionType)
        return sv_rendering._slice(
            _expression(expression.expression),
            union_type.width - 1,
            union_type.payload_width,
        )
    if isinstance(expression, expr.UnionField):
        union_type = expression.expression.type
        assert isinstance(union_type, ir_types.TaggedUnionType)
        msb, lsb = ir_packing.tagged_union_field_slice(
            union_type,
            expression.variant,
            expression.field,
        )
        return sv_rendering._slice(_expression(expression.expression), msb, lsb)
    if isinstance(expression, expr.Add):
        width = sv_rendering._width(expression.type)
        return (
            f"({_resize(expression.left, width)} + "
            f"{_resize(expression.right, width)})"
        )
    if isinstance(expression, expr.Binary):
        width = sv_rendering._width(expression.operand_type)
        left = _resize(expression.left, width)
        right = _resize(expression.right, width)
        if expression.operator is expr.BinaryOperator.SHIFT_RIGHT:
            return render_right_shift(
                left,
                right,
                signed=isinstance(expression.operand_type, ir_types.SIntType),
            )
        operator = {
            expr.BinaryOperator.SUBTRACT: "-",
            expr.BinaryOperator.MULTIPLY: "*",
            expr.BinaryOperator.BIT_AND: "&",
            expr.BinaryOperator.BIT_OR: "|",
            expr.BinaryOperator.BIT_XOR: "^",
            expr.BinaryOperator.SHIFT_LEFT: "<<",
            expr.BinaryOperator.EQUAL: "==",
            expr.BinaryOperator.NOT_EQUAL: "!=",
            expr.BinaryOperator.LESS: "<",
            expr.BinaryOperator.LESS_EQUAL: "<=",
            expr.BinaryOperator.GREATER: ">",
            expr.BinaryOperator.GREATER_EQUAL: ">=",
        }[expression.operator]
        if expression.operator in {
            expr.BinaryOperator.LESS,
            expr.BinaryOperator.LESS_EQUAL,
            expr.BinaryOperator.GREATER,
            expr.BinaryOperator.GREATER_EQUAL,
        }:
            return render_ordered_comparison(
                left,
                operator,
                right,
                signed=isinstance(expression.operand_type, (ir_types.SIntType, ir_types.FixedType)),
            )
        return f"({left} {operator} {right})"
    if isinstance(expression, expr.Extend):
        return _resize(expression.expression, sv_rendering._width(expression.type))
    if isinstance(expression, expr.Truncate):
        width = sv_rendering._width(expression.type)
        return f"{width}'({_expression(expression.expression)})"
    if isinstance(expression, expr.FixedConvert):
        return _fixed_convert(expression)
    if isinstance(expression, expr.Mux):
        return (
            f"({_expression(expression.condition)} ? "
            f"{_expression(expression.when_true)} : "
            f"{_expression(expression.when_false)})"
        )
    if isinstance(expression, expr.Switch):
        rendered = _expression(expression.default)
        selector = _expression(expression.selector)
        selector_width = sv_rendering._width(expression.selector.type)
        for case in reversed(expression.cases):
            rendered = (
                f"(({selector}) == {selector_width}'d{case.key} ? "
                f"{_expression(case.expression)} : {rendered})"
            )
        return rendered
    if isinstance(expression, expr.FieldAccess):
        if not isinstance(expression.expression.type, ir_types.StructType):
            raise SystemVerilogEmissionError("field access requires a struct")
        if isinstance(expression.expression, expr.RuntimeIndex):
            return _runtime_struct_field_projection(expression)
        return sv_rendering._struct_field_expression(
            _expression(expression.expression),
            expression.expression.type,
            expression.field,
        )
    if isinstance(expression, expr.StructConstruct):
        return "{" + sv_rendering._comma_join(
            tuple(_expression(value) for _, value in expression.fields)
        ) + "}"
    if isinstance(expression, expr.TupleConstruct):
        return "{" + sv_rendering._comma_join(tuple(
            f"{sv_rendering._width(value.type)}'({_expression(value)})"
            for value in reversed(expression.elements)
        )) + "}"
    if isinstance(expression, expr.TupleProject):
        tuple_type = expression.expression.type
        if not isinstance(tuple_type, ir_types.TupleType):
            raise SystemVerilogEmissionError(
                "tuple projection requires a structural tuple"
            )
        lsb = ir_packing.tuple_element_lsb(tuple_type, expression.index)
        msb = lsb + sv_rendering._width(tuple_type.elements[expression.index]) - 1
        projected = sv_rendering._slice(_expression(expression.expression), msb, lsb)
        if isinstance(expression.type, (ir_types.SIntType, ir_types.FixedType)):
            return f"$signed({projected})"
        return projected
    if isinstance(expression, (expr.VectorIndex, expr.RuntimeIndex)):
        return _vector_index_expression(expression)
    if isinstance(expression, expr.VectorUpdate):
        vector = expression.expression.type
        if not isinstance(vector, ir_types.VecType):
            raise SystemVerilogEmissionError("vector update requires a vector")
        total_width = sv_rendering._width(vector)
        element_width = sv_rendering._width(vector.element_type)
        rendered_vector = _expression(expression.expression)
        rendered_index = _expression(expression.index)
        rendered_value = _expression(expression.value)
        base = sv_rendering._scaled_packed_index(rendered_index, element_width)
        element_mask = f"{total_width}'h{((1 << element_width) - 1):x}"
        cleared = (
            f"({total_width}'($unsigned({rendered_vector})) & "
            f"~({element_mask} << ({base})))"
        )
        inserted = (
            f"(({total_width}'($unsigned({rendered_value})) & {element_mask}) "
            f"<< ({base}))"
        )
        return f"{total_width}'(({cleared}) | ({inserted}))"
    if isinstance(expression, expr.Slice):
        width = sv_rendering._width(expression.type)
        value = _expression(expression.expression)
        return f"{width}'(($unsigned({value})) >> {expression.lsb})"
    if isinstance(expression, expr.Concat):
        if len(expression.operands) < 2:
            raise SystemVerilogEmissionError(
                "typed concat requires at least two operands"
            )
        return "{" + sv_rendering._comma_join(tuple(
            f"{sv_rendering._width(operand.type)}'({_expression(operand)})"
            for operand in expression.operands
        )) + "}"
    if isinstance(expression, expr.VectorConcat):
        if len(expression.operands) < 2:
            raise SystemVerilogEmissionError(
                "typed vector concat requires at least two operands"
            )
        return "{" + sv_rendering._comma_join(tuple(
            f"{sv_rendering._width(operand.type)}'({_expression(operand)})"
            for operand in reversed(expression.operands)
        )) + "}"
    if isinstance(expression, expr.Reshape):
        width = sv_rendering._width(expression.type)
        return f"{width}'($unsigned({_expression(expression.expression)}))"
    if isinstance(expression, expr.Bitcast):
        width = sv_rendering._width(expression.type)
        raw = f"{width}'($unsigned({_expression(expression.expression)}))"
        if isinstance(expression.type, (ir_types.SIntType, ir_types.FixedType)):
            return f"$signed({raw})"
        return raw
    if isinstance(expression, expr.Pack):
        width = sv_rendering._width(expression.type)
        return f"{width}'($unsigned({_expression(expression.expression)}))"
    if isinstance(expression, expr.Unpack):
        width = sv_rendering._width(expression.type)
        raw = f"{width}'($unsigned({_expression(expression.expression)}))"
        if isinstance(expression.type, (ir_types.SIntType, ir_types.FixedType)):
            return f"$signed({raw})"
        return raw
    if isinstance(expression, expr.Dot):
        return _balanced_expression(expression.products)
    if isinstance(expression, expr.Reduce):
        return _expression(ir_functional.lower_reduction(expression))
    if isinstance(expression, expr.FunctionalRegion):
        raise SystemVerilogEmissionError(
            "functional region requires statement-based emission"
        )
    if isinstance(expression, (expr.Generate, expr.Map)):
        if isinstance(expression.type, ir_types.VecType):
            # Generate/map are semantically unrolled vectors by this stage.
            # SystemVerilog concatenations list the MSB first, whereas ZLang
            # indexed aggregates place element zero at the LSB.
            return "{" + sv_rendering._comma_join(tuple(
                _expression(element) for element in reversed(expression.elements)
            )) + "}"
        return _balanced_expression(expression.elements)
    if isinstance(expression, expr.Call):
        arguments = sv_rendering._comma_join(tuple(
            _expression(item) for item in expression.arguments
        ))
        return f"{sv_rendering._identifier(expression.function)}({arguments})"
    if isinstance(expression, expr.ImplementationChoice):
        return _expression(expression.selected_alternative.expression)
    if isinstance(expression, (expr.Delay, expr.Pipeline)):
        raise SystemVerilogEmissionError(
            "nested delay/pipeline expressions are outside the direct experiment"
        )
    raise SystemVerilogEmissionError(
        f"unsupported direct SystemVerilog expression {type(expression).__name__}"
    )


def _balanced_expression(elements: tuple[expr.Expression, ...]) -> str:
    if not elements:
        raise SystemVerilogEmissionError("direct reduction cannot be empty")
    if len(elements) == 1:
        return _expression(elements[0])
    middle = len(elements) // 2
    return (
        f"({_balanced_expression(elements[:middle])} + "
        f"{_balanced_expression(elements[middle:])})"
    )


def _resize(expression: expr.Expression, width: int) -> str:
    return render_typed_resize(
        _expression(expression),
        source_width=sv_rendering._width(expression.type),
        target_width=width,
        signed=isinstance(expression.type, (ir_types.SIntType, ir_types.FixedType)),
    )
