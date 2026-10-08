"""Typed formal-predicate construction from backend-independent IR."""

from __future__ import annotations

from dataclasses import replace
import hashlib

from zlang.ir.interfaces import (
    CreditSignal,
    PacketSignal,
    ReadyValidSignal,
    VirtualChannelCreditSignal,
)
from zlang.ir import expressions as ir_expr
from zlang.ir import packing as ir_packing
from zlang.ir.formal_models import (
    FormalError,
    FormalProperty,
    FormalPropertyClassification,
    Ownership,
    PropertyKind,
    TemporalForm,
)
from zlang.ir.formal_observations import (
    fifo_observation_id,
    port_observation_id,
    register_observation_id,
)
from zlang.ir.formal_predicates import (
    Binary as PredicateBinary,
    Constant as PredicateConstant,
    FormalBinaryOperator,
    FormalPredicate,
    FormalSignedness,
    FormalUnaryOperator,
    Mux as PredicateMux,
    ObservationCycle,
    ObservationRef,
    Unary as PredicateUnary,
)
from zlang.ir.module import Module
from zlang.ir.storage import FifoSignal
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    SIntType,
    StructType,
    TupleType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.source import SourceOrigin


def _stable_id(family: str, name: str, detail: str) -> str:
    digest = hashlib.sha256(f"{family}|{name}|{detail}".encode()).hexdigest()[:12]
    return f"safety_verification.{family}.{name}.{digest}"


def _clock(module: Module) -> str:
    if module.clock is not None:
        return module.clock
    if len(module.clock_domains) == 1:
        return module.clock_domains[0].name
    raise FormalError("automatic formal properties require an explicit clock domain")


def _reset(module: Module) -> str | None:
    return module.reset


_MODULE_RESET = object()


def _property(
    family: str,
    name: str,
    expression: str,
    module: Module,
    *,
    ownership: Ownership,
    generated_from: str,
    predicate: FormalPredicate,
    kind: PropertyKind = PropertyKind.ASSERTION,
    temporal: TemporalForm = TemporalForm.SAME_CYCLE,
    antecedent: str | None = None,
    consequent: str | None = None,
    origin: SourceOrigin | None = None,
    reset_condition: str | None | object = _MODULE_RESET,
    non_executable_reason: str | None = None,
    classification: FormalPropertyClassification = (
        FormalPropertyClassification.BEHAVIORAL
    ),
) -> FormalProperty:
    return FormalProperty(
        id=_stable_id(family, module.name, name), kind=kind, clock=_clock(module),
        reset_condition=(
            _reset(module) if reset_condition is _MODULE_RESET else reset_condition
        ),
        expression=expression, temporal_form=temporal,
        ownership=ownership, source_origin=origin, generated_from=generated_from,
        relevant_signals=predicate.observation_ids(), antecedent=antecedent,
        consequent=consequent, predicate=predicate,
        non_executable_reason=non_executable_reason,
        classification=classification,
    )


def _formal_signedness(type_: object) -> FormalSignedness:
    if isinstance(type_, BitType):
        return FormalSignedness.BIT
    if isinstance(type_, (SIntType, FixedType)):
        return FormalSignedness.SIGNED
    if isinstance(type_, (UIntType, UFixedType, EnumType)):
        return FormalSignedness.UNSIGNED
    return FormalSignedness.BITS


def _observation(
    semantic_id: str,
    type_: object,
    cycle: ObservationCycle = ObservationCycle.CURRENT,
) -> ObservationRef:
    return ObservationRef(
        semantic_id,
        getattr(type_, "width", 1),
        _formal_signedness(type_),
        cycle,
    )


def _bit_observation(
    semantic_id: str,
    cycle: ObservationCycle = ObservationCycle.CURRENT,
) -> ObservationRef:
    return ObservationRef(semantic_id, 1, FormalSignedness.BIT, cycle)


def _previous(value: ObservationRef) -> ObservationRef:
    return replace(value, cycle=ObservationCycle.PREVIOUS)


def _constant(value: int, width: int, signedness: FormalSignedness) -> PredicateConstant:
    return PredicateConstant(value, width, signedness)


def _bit(value: bool) -> PredicateConstant:
    return PredicateConstant(int(value), 1, FormalSignedness.BIT)


def _not(value: FormalPredicate) -> FormalPredicate:
    return PredicateUnary(
        FormalUnaryOperator.LOGICAL_NOT, value, 1, FormalSignedness.BIT
    )


def _resize(
    value: FormalPredicate, width: int, signedness: FormalSignedness
) -> FormalPredicate:
    if (value.width, value.signedness) == (width, signedness):
        return value
    return PredicateUnary(FormalUnaryOperator.RESIZE, value, width, signedness)


def _binary(
    operator: FormalBinaryOperator,
    left: FormalPredicate,
    right: FormalPredicate,
    width: int,
    signedness: FormalSignedness,
) -> FormalPredicate:
    return PredicateBinary(operator, left, right, width, signedness)


def _and(left: FormalPredicate, right: FormalPredicate) -> FormalPredicate:
    return _binary(
        FormalBinaryOperator.LOGICAL_AND, left, right, 1, FormalSignedness.BIT
    )


def _or(left: FormalPredicate, right: FormalPredicate) -> FormalPredicate:
    return _binary(
        FormalBinaryOperator.LOGICAL_OR, left, right, 1, FormalSignedness.BIT
    )


def _implies(left: FormalPredicate, right: FormalPredicate) -> FormalPredicate:
    return _binary(
        FormalBinaryOperator.IMPLIES, left, right, 1, FormalSignedness.BIT
    )


def _equal(left: FormalPredicate, right: FormalPredicate) -> FormalPredicate:
    return _binary(
        FormalBinaryOperator.EQUAL, left, right, 1, FormalSignedness.BIT
    )


def _ordered(
    operator: FormalBinaryOperator,
    left: FormalPredicate,
    right: FormalPredicate,
) -> FormalPredicate:
    return _binary(operator, left, right, 1, FormalSignedness.BIT)


def _without_previous_reset(module: Module, predicate: FormalPredicate) -> FormalPredicate:
    if module.reset is None:
        return predicate
    return _implies(
        _not(_bit_observation("reset", ObservationCycle.PREVIOUS)), predicate
    )


def _packed_predicate_concat(
    operands: tuple[ir_expr.Expression, ...],
    *,
    expected_width: int,
) -> FormalPredicate:
    """Pack typed operands in the language's frozen MSB-first order.

    FormalPredicate deliberately has no second aggregate/layout IR.  Exact
    concatenation is therefore expressed using its existing typed resize,
    shift, and bitwise-or nodes.  Each source operand is first reinterpreted at
    *its own* packed width, so a signed operand is never accidentally
    sign-extended into a neighbouring field.
    """

    if not operands:
        raise FormalError("formal packed construction requires at least one operand")
    try:
        widths = tuple(ir_packing.packed_width(item.type) for item in operands)
    except ir_packing.PackingError as error:
        raise FormalError("formal concatenation requires bit-packable operands") from error
    if sum(widths) != expected_width:
        raise FormalError(
            "formal concatenation packed width does not match its typed result"
        )

    result = _resize(
        _semantic_predicate(operands[0]), expected_width, FormalSignedness.BITS
    )
    for operand, width in zip(operands[1:], widths[1:], strict=True):
        amount_width = max(1, width.bit_length())
        result = _binary(
            FormalBinaryOperator.SHIFT_LEFT,
            result,
            _constant(width, amount_width, FormalSignedness.UNSIGNED),
            expected_width,
            FormalSignedness.BITS,
        )
        result = _binary(
            FormalBinaryOperator.BIT_OR,
            result,
            _resize(
                _semantic_predicate(operand),
                expected_width,
                FormalSignedness.BITS,
            ),
            expected_width,
            FormalSignedness.BITS,
        )
    return result


def _semantic_reference_predicate(
    expression: ir_expr.Expression,
) -> FormalPredicate | None:
    if isinstance(expression, ir_expr.InputRef):
        return _observation(port_observation_id(expression.name), expression.type)
    if isinstance(expression, ir_expr.RegisterRef):
        return _observation(register_observation_id(expression.name), expression.type)
    if isinstance(expression, ir_expr.ParameterRef):
        raise FormalError(
            f"compile-time parameter '{expression.name}' was not resolved before "
            "formal predicate lowering"
        )
    if isinstance(expression, ir_expr.Constant):
        return _constant(
            expression.value,
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, ir_expr.ReadyValidRef):
        if expression.signal is ReadyValidSignal.TRANSFER:
            return _and(
                _bit_observation(port_observation_id(expression.interface, "valid")),
                _bit_observation(port_observation_id(expression.interface, "ready")),
            )
        return _observation(
            port_observation_id(expression.interface, expression.signal.value),
            expression.type,
        )
    if isinstance(expression, ir_expr.CreditRef):
        signal = (
            CreditSignal.SEND
            if expression.signal is CreditSignal.TRANSFER
            else expression.signal
        )
        return _observation(
            port_observation_id(expression.interface, signal.value),
            expression.type,
        )
    if isinstance(expression, ir_expr.PacketRef):
        if expression.signal is PacketSignal.TRANSFER:
            return _and(
                _bit_observation(port_observation_id(expression.interface, "valid")),
                _bit_observation(port_observation_id(expression.interface, "ready")),
            )
        return _observation(
            port_observation_id(expression.interface, expression.signal.value),
            expression.type,
        )
    if isinstance(expression, ir_expr.VirtualChannelCreditRef):
        signal = (
            VirtualChannelCreditSignal.SEND
            if expression.signal is VirtualChannelCreditSignal.TRANSFER
            else expression.signal
        )
        return _observation(
            port_observation_id(expression.interface, signal.value),
            expression.type,
        )
    if isinstance(expression, ir_expr.RequestResponseRef):
        if expression.signal is ReadyValidSignal.TRANSFER:
            prefix = f"{expression.channel.value}."
            return _and(
                _bit_observation(port_observation_id(
                    expression.interface, prefix + "valid"
                )),
                _bit_observation(port_observation_id(
                    expression.interface, prefix + "ready"
                )),
            )
        return _observation(
            port_observation_id(
                expression.interface,
                f"{expression.channel.value}.{expression.signal.value}",
            ),
            expression.type,
        )
    if isinstance(expression, ir_expr.FifoRef):
        if expression.signal is FifoSignal.VALID:
            return _not(_bit_observation(
                fifo_observation_id(expression.fifo, FifoSignal.EMPTY.value)
            ))
        if expression.signal is FifoSignal.READY:
            return _not(_bit_observation(
                fifo_observation_id(expression.fifo, FifoSignal.FULL.value)
            ))
        return _observation(
            fifo_observation_id(expression.fifo, expression.signal.value),
            expression.type,
        )
    return None


def _semantic_scalar_predicate(
    expression: ir_expr.Expression,
) -> FormalPredicate | None:
    if isinstance(expression, ir_expr.EnumEncode):
        return _resize(
            _semantic_predicate(expression.expression),
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, ir_expr.EnumValid):
        raw = _resize(
            _semantic_predicate(expression.expression),
            expression.expression.type.width,
            _formal_signedness(expression.expression.type),
        )
        comparisons = tuple(
            _equal(
                raw,
                _constant(code, raw.width, raw.signedness),
            )
            for code in expression.enum_type.codes
        )
        result = comparisons[0]
        for comparison in comparisons[1:]:
            result = _or(result, comparison)
        return result
    if isinstance(expression, ir_expr.EnumDecode):
        raw = _resize(
            _semantic_predicate(expression.expression),
            expression.expression.type.width,
            _formal_signedness(expression.expression.type),
        )
        result = _resize(
            _semantic_predicate(expression.fallback),
            expression.type.width,
            _formal_signedness(expression.type),
        )
        for code in reversed(expression.type.codes):
            result = PredicateMux(
                _equal(raw, _constant(code, raw.width, raw.signedness)),
                _constant(code, expression.type.width, _formal_signedness(expression.type)),
                result,
                expression.type.width,
                _formal_signedness(expression.type),
            )
        return result
    if isinstance(expression, ir_expr.Add):
        signedness = _formal_signedness(expression.type)
        width = expression.type.width
        return _binary(
            FormalBinaryOperator.ADD,
            _resize(_semantic_predicate(expression.left), width, signedness),
            _resize(_semantic_predicate(expression.right), width, signedness),
            width,
            signedness,
        )
    if isinstance(expression, ir_expr.Binary):
        mapping = {
            ir_expr.BinaryOperator.SUBTRACT: FormalBinaryOperator.SUBTRACT,
            ir_expr.BinaryOperator.MULTIPLY: FormalBinaryOperator.MULTIPLY,
            ir_expr.BinaryOperator.BIT_AND: FormalBinaryOperator.BIT_AND,
            ir_expr.BinaryOperator.BIT_OR: FormalBinaryOperator.BIT_OR,
            ir_expr.BinaryOperator.BIT_XOR: FormalBinaryOperator.BIT_XOR,
            ir_expr.BinaryOperator.SHIFT_LEFT: FormalBinaryOperator.SHIFT_LEFT,
            ir_expr.BinaryOperator.SHIFT_RIGHT: FormalBinaryOperator.SHIFT_RIGHT,
            ir_expr.BinaryOperator.EQUAL: FormalBinaryOperator.EQUAL,
            ir_expr.BinaryOperator.NOT_EQUAL: FormalBinaryOperator.NOT_EQUAL,
            ir_expr.BinaryOperator.LESS: FormalBinaryOperator.LESS,
            ir_expr.BinaryOperator.LESS_EQUAL: FormalBinaryOperator.LESS_EQUAL,
            ir_expr.BinaryOperator.GREATER: FormalBinaryOperator.GREATER,
            ir_expr.BinaryOperator.GREATER_EQUAL: FormalBinaryOperator.GREATER_EQUAL,
        }
        operand_signedness = _formal_signedness(expression.operand_type)
        operand_width = expression.operand_type.width
        left = _resize(
            _semantic_predicate(expression.left), operand_width, operand_signedness
        )
        right = _resize(
            _semantic_predicate(expression.right), operand_width, operand_signedness
        )
        result_signedness = _formal_signedness(expression.type)
        return _binary(
            mapping[expression.operator], left, right,
            expression.type.width, result_signedness,
        )
    if isinstance(expression, ir_expr.Mux):
        signedness = _formal_signedness(expression.type)
        width = expression.type.width
        return PredicateMux(
            _semantic_predicate(expression.condition),
            _resize(_semantic_predicate(expression.when_true), width, signedness),
            _resize(_semantic_predicate(expression.when_false), width, signedness),
            width,
            signedness,
        )
    if isinstance(expression, ir_expr.Switch):
        # Preserve the already-typed switch exactly as a priority-free chain
        # of equality-selected muxes.  Semantic analysis has already proved
        # keys unique and the default/result types exact; this lowering adds
        # no temporal or width behavior.
        selector = _semantic_predicate(expression.selector)
        selector_width = expression.selector.type.width
        selector_signedness = _formal_signedness(expression.selector.type)
        selector = _resize(selector, selector_width, selector_signedness)
        result_width = expression.type.width
        result_signedness = _formal_signedness(expression.type)
        result = _resize(
            _semantic_predicate(expression.default),
            result_width,
            result_signedness,
        )
        for case in reversed(expression.cases):
            result = PredicateMux(
                _equal(
                    selector,
                    _constant(case.key, selector_width, selector_signedness),
                ),
                _resize(
                    _semantic_predicate(case.expression),
                    result_width,
                    result_signedness,
                ),
                result,
                result_width,
                result_signedness,
            )
        return result
    if isinstance(expression, ir_expr.FixedConvert):
        if (
            expression.rational_denominator is not None
            or expression.kind is ir_expr.FixedConversionKind.RESCALE
            and getattr(expression.expression.type, "fraction", 0)
            != getattr(expression.type, "fraction", 0)
        ):
            raise FormalError(
                "quantized fixed-point contract expressions require semantic-reference equivalence reference equivalence"
            )
        return _resize(
            _semantic_predicate(expression.expression),
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, (ir_expr.Extend, ir_expr.Truncate)):
        return _resize(
            _semantic_predicate(expression.expression),
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, (ir_expr.Bitcast, ir_expr.Pack, ir_expr.Unpack)):
        return _resize(
            _semantic_predicate(expression.expression),
            expression.type.width,
            _formal_signedness(expression.type),
        )
    return None


def _semantic_aggregate_predicate(
    expression: ir_expr.Expression,
) -> FormalPredicate | None:
    if isinstance(expression, ir_expr.Concat):
        if len(expression.operands) < 2:
            raise FormalError("formal concatenation requires at least two operands")
        return _packed_predicate_concat(
            expression.operands,
            expected_width=expression.type.width,
        )
    if isinstance(expression, ir_expr.StructConstruct):
        aggregate = expression.type
        if not isinstance(aggregate, StructType):
            raise FormalError("formal struct construction requires a struct type")
        expected_fields = tuple(field.name for field in aggregate.fields)
        actual_fields = tuple(name for name, _ in expression.fields)
        if actual_fields != expected_fields:
            raise FormalError(
                "formal struct construction does not match its typed fields"
            )
        operands = tuple(value for _, value in expression.fields)
        return _packed_predicate_concat(
            operands,
            expected_width=ir_packing.packed_width(aggregate),
        )
    if isinstance(expression, ir_expr.TupleConstruct):
        return _packed_predicate_concat(
            tuple(reversed(expression.elements)),
            expected_width=ir_packing.packed_width(expression.type),
        )
    if isinstance(expression, ir_expr.Slice):
        source = _semantic_predicate(expression.expression)
        raw = _resize(source, source.width, FormalSignedness.BITS)
        if expression.lsb:
            amount_width = max(1, expression.lsb.bit_length())
            raw = _binary(
                FormalBinaryOperator.SHIFT_RIGHT,
                raw,
                _constant(
                    expression.lsb,
                    amount_width,
                    FormalSignedness.UNSIGNED,
                ),
                raw.width,
                raw.signedness,
            )
        return _resize(raw, expression.type.width, _formal_signedness(expression.type))
    if isinstance(expression, ir_expr.FieldAccess):
        aggregate = expression.expression.type
        if not isinstance(aggregate, StructType):
            raise FormalError("formal field projection requires a struct value")
        if isinstance(expression.expression, ir_expr.StructConstruct):
            try:
                field_value = next(
                    value
                    for name, value in expression.expression.fields
                    if name == expression.field
                )
            except StopIteration as error:
                raise FormalError(
                    f"formal struct has no field '{expression.field}'"
                ) from error
            return _resize(
                _semantic_predicate(field_value),
                expression.type.width,
                _formal_signedness(expression.type),
            )
        try:
            selected = next(
                index
                for index, field in enumerate(aggregate.fields)
                if field.name == expression.field
            )
            widths = tuple(ir_packing.packed_width(field.type) for field in aggregate.fields)
        except (StopIteration, ir_packing.PackingError) as error:
            raise FormalError(
                f"formal field projection '{expression.field}' is not bit-packable"
            ) from error
        lsb = sum(widths[selected + 1 :])
        projected = _semantic_predicate(ir_expr.Slice(
            expression.expression,
            lsb + widths[selected] - 1,
            lsb,
            BitsType(widths[selected]),
            origin=expression.origin,
        ))
        return _resize(
            projected,
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, ir_expr.TupleProject):
        aggregate = expression.expression.type
        if not isinstance(aggregate, TupleType):
            raise FormalError("formal tuple projection requires a tuple value")
        if isinstance(expression.expression, ir_expr.TupleConstruct):
            return _resize(
                _semantic_predicate(expression.expression.elements[expression.index]),
                expression.type.width,
                _formal_signedness(expression.type),
            )
        try:
            widths = tuple(ir_packing.packed_width(item) for item in aggregate.elements)
        except ir_packing.PackingError as error:
            raise FormalError("formal tuple projection is not bit-packable") from error
        lsb = ir_packing.tuple_element_lsb(aggregate, expression.index)
        projected = _semantic_predicate(ir_expr.Slice(
            expression.expression,
            lsb + widths[expression.index] - 1,
            lsb,
            BitsType(widths[expression.index]),
            origin=expression.origin,
        ))
        return _resize(
            projected,
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, ir_expr.VectorIndex):
        aggregate = expression.expression.type
        if not isinstance(aggregate, VecType) or not isinstance(expression.index, int):
            raise FormalError("formal vector projection requires a constant index")
        try:
            element_width = ir_packing.packed_width(aggregate.element_type)
        except ir_packing.PackingError as error:
            raise FormalError("formal vector projection is not bit-packable") from error
        lsb = ir_packing.vector_element_lsb(aggregate, expression.index)
        projected = _semantic_predicate(ir_expr.Slice(
            expression.expression,
            lsb + element_width - 1,
            lsb,
            BitsType(element_width),
            origin=expression.origin,
        ))
        return _resize(
            projected,
            expression.type.width,
            _formal_signedness(expression.type),
        )
    if isinstance(expression, ir_expr.RuntimeIndex):
        aggregate = expression.expression.type
        if not isinstance(aggregate, VecType):
            raise FormalError("formal runtime projection requires a vector value")
        try:
            element_width = ir_packing.packed_width(aggregate.element_type)
        except ir_packing.PackingError as error:
            raise FormalError(
                "formal runtime vector projection is not bit-packable"
            ) from error

        # RuntimeIndex is admitted by semantic analysis only after its complete
        # selector range is proven inside the vector.  Preserve the language's
        # LSB-first indexed layout and lower the selection to the existing
        # same-cycle mux/equality predicate vocabulary.  No assertion is used
        # to justify the range proof and no default/clamp behavior is added.
        packed = _resize(
            _semantic_predicate(expression.expression),
            ir_packing.packed_width(aggregate),
            FormalSignedness.BITS,
        )
        selector = _semantic_predicate(expression.index)

        def element(index: int) -> FormalPredicate:
            raw = packed
            lsb = ir_packing.vector_element_lsb(aggregate, index)
            if lsb:
                raw = _binary(
                    FormalBinaryOperator.SHIFT_RIGHT,
                    raw,
                    _constant(
                        lsb,
                        max(1, lsb.bit_length()),
                        FormalSignedness.UNSIGNED,
                    ),
                    raw.width,
                    raw.signedness,
                )
            return _resize(
                raw,
                expression.type.width,
                _formal_signedness(expression.type),
            )

        result = element(aggregate.length - 1)
        for index in reversed(range(aggregate.length - 1)):
            result = PredicateMux(
                _equal(
                    selector,
                    _constant(index, selector.width, selector.signedness),
                ),
                element(index),
                result,
                expression.type.width,
                _formal_signedness(expression.type),
            )
        return result
    return None


def _semantic_predicate(expression: ir_expr.Expression) -> FormalPredicate:
    """Lower the frozen typed contract subset without executable strings."""

    for lower in (
        _semantic_reference_predicate,
        _semantic_scalar_predicate,
        _semantic_aggregate_predicate,
    ):
        predicate = lower(expression)
        if predicate is not None:
            return predicate
    raise FormalError(
        f"unsupported contract expression for formal lowering: {expression!r}"
    )


def _type_range(type_: object) -> str:
    width = getattr(type_, "width", 1)
    if isinstance(type_, (SIntType, FixedType)):
        return f"-({1 << (width - 1)}) <= state && state < {1 << (width - 1)}"
    if isinstance(type_, (UIntType, UFixedType, BitType)):
        return f"state >= 0 && state < {1 << width}"
    if isinstance(type_, EnumType):
        if type_.explicit_codes is not None:
            return " || ".join(f"state == {code}" for code in type_.codes)
        return f"state >= 0 && state < {len(type_.members)}"
    return "state == state"
