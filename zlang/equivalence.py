"""M36 selected-architecture equivalence construction and deterministic emitters."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import shutil
from pathlib import Path
from typing import Iterable

from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.backend.systemverilog.syntax import sized_decimal
from zlang.common.tool_inventory import discover_tool_inventory
from zlang.formal_domain import (
    FormalDomainRendering,
    FormalDomainRenderingError,
    render_formal_domain,
)
from zlang.formal_trace import TraceBinding, originating_sample_cycle
from zlang.ir import expressions as expr
from zlang.ir.comparison_window import ComparisonWindow
from zlang.ir.cdc import ClockDomain, PowerUpPolicy
from zlang.ir.equivalence import (
    BindingMap, BindingSide, EquivalenceBinding, EquivalenceError,
    EquivalenceMode, EquivalenceProperty, EquivalenceRelation,
    EquivalenceResult, EquivalenceCounterexample, EquivalenceStatus, SignalRole, classify_equivalence,
    signedness, stable_equivalence_id,
)
from zlang.ir.types import (
    BitType, BitsType, EnumType, FixedType, HardwareType, SIntType, UFixedType, UIntType,
    StructType, TupleType, VecType,
)
from zlang.ir.module import Module, PortDirection
from zlang.ir.callables import CallableExpansionError, expand_callable_calls
from zlang.ir.functional import (
    FunctionalLoweringError,
    lower_reduction,
    materialize_functional_region,
)
from zlang.ir.interfaces import InterfaceProtocol
from zlang.fixed_point import quantize_rational
from zlang.timing import TimingInfo, timing_info


_SUPPORTED_CLASSES = {"value", "m27", "m29", "m32", "m31", "pipeline"}


@dataclass(frozen=True)
class MiterTraceMetadata:
    """Exact checker-local signals published by one M36 miter emission.

    These are physical names allocated beside the actual binding map.  They
    deliberately live outside :class:`EquivalenceProperty`: checker spelling
    is an emitted-artifact concern and must not affect semantic identity.
    """

    reference_output: str
    implementation_output: str
    reset: str | None = None
    comparison_valid: str | None = None

    def __post_init__(self) -> None:
        required = (self.reference_output, self.implementation_output)
        if any(not isinstance(item, str) or not item for item in required):
            raise EquivalenceError("M36 trace output names must be non-empty")
        optional = (self.reset, self.comparison_valid)
        if any(item is not None and (not isinstance(item, str) or not item)
               for item in optional):
            raise EquivalenceError("M36 optional trace names must be non-empty")
        if (self.reset is None) != (self.comparison_valid is None):
            raise EquivalenceError(
                "M36 timed trace metadata requires reset and comparison-valid together"
            )


@dataclass(frozen=True)
class MiterEmission:
    """SystemVerilog miter plus its authoritative physical trace metadata."""

    source: str
    trace_metadata: MiterTraceMetadata


def _type(type_: HardwareType) -> str:
    if isinstance(type_, BitType):
        return "logic"
    if isinstance(type_, (UIntType, BitsType, UFixedType, EnumType)):
        return f"logic [{type_.width - 1}:0]"
    if isinstance(type_, (SIntType, FixedType)):
        return f"logic signed [{type_.width - 1}:0]"
    if isinstance(type_, (StructType, TupleType, VecType)):
        return f"logic [{type_.width - 1}:0]"
    raise EquivalenceError(f"reference model does not support aggregate type {type_}")


def _expr(value: expr.Expression) -> str:
    if isinstance(value, expr.InputRef):
        return value.name
    if isinstance(value, expr.Constant):
        if isinstance(value.type, BitType):
            return "1'b1" if value.value else "1'b0"
        return sized_decimal(
            value.type.width,
            value.value,
            signed=isinstance(value.type, (SIntType, FixedType)),
        )
    if isinstance(value, expr.EnumEncode):
        return f"{value.type.width}'($unsigned({_expr(value.expression)}))"
    if isinstance(value, expr.EnumValid):
        raw = _expr(value.expression)
        width = value.expression.type.width
        return "(" + " || ".join(
            f"(({raw}) == {width}'d{code})"
            for code in value.enum_type.codes
        ) + ")"
    if isinstance(value, expr.EnumDecode):
        raw = _expr(value.expression)
        width = value.type.width
        valid = " || ".join(
            f"(({raw}) == {width}'d{code})"
            for code in value.type.codes
        )
        return (
            f"(({valid}) ? {width}'($unsigned({raw})) : "
            f"({_expr(value.fallback)}))"
        )
    if isinstance(value, expr.Add):
        return f"({_expr(value.left)} + {_expr(value.right)})"
    if isinstance(value, expr.Binary):
        return f"({_expr(value.left)} {value.operator.value} {_expr(value.right)})"
    if isinstance(value, (expr.Extend, expr.Truncate)):
        # Resize nodes are semantic bit-vector boundaries.  Relying on a later
        # assignment context is incorrect when the resized value feeds another
        # expression, such as a runtime vector selector.
        return f"{value.type.width}'({_expr(value.expression)})"
    if isinstance(value, expr.FixedConvert):
        return _fixed_convert_expr(value)
    if isinstance(value, expr.Mux):
        return f"({_expr(value.condition)} ? {_expr(value.when_true)} : {_expr(value.when_false)})"
    if isinstance(value, expr.Switch):
        rendered = _expr(value.default)
        for case in reversed(value.cases):
            rendered = f"({_expr(value.selector)} == {case.key} ? {_expr(case.expression)} : {rendered})"
        return rendered
    if isinstance(value, expr.Slice):
        return (
            f"{value.type.width}'(($unsigned({_expr(value.expression)})) >> "
            f"{value.lsb})"
        )
    if isinstance(value, expr.Concat):
        if len(value.operands) < 2:
            raise EquivalenceError("typed concat requires at least two operands")
        return "{" + ", ".join(
            f"{operand.type.width}'({_expr(operand)})"
            for operand in value.operands
        ) + "}"
    if isinstance(value, expr.VectorConcat):
        if len(value.operands) < 2:
            raise EquivalenceError("typed vector concat requires at least two operands")
        return "{" + ", ".join(
            f"{operand.type.width}'({_expr(operand)})"
            for operand in value.operands
        ) + "}"
    if isinstance(value, expr.Reshape):
        return f"{value.type.width}'($unsigned({_expr(value.expression)}))"
    if isinstance(value, expr.Bitcast):
        raw = f"{value.type.width}'($unsigned({_expr(value.expression)}))"
        if isinstance(value.type, (SIntType, FixedType)):
            return f"$signed({raw})"
        return raw
    if isinstance(value, expr.Pack):
        return (
            f"{value.type.width}'($unsigned({_expr(value.expression)}))"
        )
    if isinstance(value, expr.Unpack):
        raw = f"{value.type.width}'($unsigned({_expr(value.expression)}))"
        if isinstance(value.type, (SIntType, FixedType)):
            return f"$signed({raw})"
        return raw
    if isinstance(value, expr.StructConstruct):
        return "{" + ", ".join(
            f"{item.type.width}'({_expr(item)})"
            for _, item in value.fields
        ) + "}"
    if isinstance(value, expr.TupleConstruct):
        return "{" + ", ".join(
            f"{item.type.width}'({_expr(item)})"
            for item in value.elements
        ) + "}"
    if isinstance(value, expr.TupleProject):
        aggregate_type = value.expression.type
        if not isinstance(aggregate_type, TupleType):
            raise EquivalenceError("tuple projection base is not a tuple")
        lsb = sum(item.width for item in aggregate_type.elements[value.index + 1:])
        projected = (
            f"{value.type.width}'(($unsigned({_expr(value.expression)})) >> {lsb})"
        )
        if isinstance(value.type, (SIntType, FixedType)):
            return f"$signed({projected})"
        return projected
    if isinstance(value, expr.FieldAccess):
        aggregate_type = value.expression.type
        if not isinstance(aggregate_type, StructType):
            raise EquivalenceError("field access base is not a struct")
        offset = aggregate_type.width
        for field in aggregate_type.fields:
            offset -= field.type.width
            if field.name == value.field:
                return (
                    f"{field.type.width}'(($unsigned({_expr(value.expression)})) "
                    f">> {offset})"
                )
        raise EquivalenceError(
            f"struct '{aggregate_type.name}' has no field '{value.field}'"
        )
    if isinstance(value, expr.VectorIndex):
        vector = value.expression.type
        if not isinstance(vector, VecType):
            raise EquivalenceError("vector index base is not a vector")
        lsb = (vector.length - value.index - 1) * vector.element_type.width
        return (
            f"{value.type.width}'(($unsigned({_expr(value.expression)})) >> {lsb})"
        )
    if isinstance(value, expr.RuntimeIndex):
        if not isinstance(value.expression.type, VecType):
            raise EquivalenceError("runtime index base is not a vector")
        element_width = value.type.width
        return (
            f"{_expr(value.expression)}[((32'd{value.vector_length - 1} - "
            f"32'({_expr(value.index)})) * 32'd{element_width}) +: "
            f"{element_width}]"
        )
    if isinstance(value, (expr.Generate, expr.Map)):
        return "{" + ", ".join(
            f"{item.type.width}'({_expr(item)})" for item in value.elements
        ) + "}"
    if isinstance(value, expr.Reduce):
        # Scalar representation reductions are deliberately expressed as an
        # explicit Bitcast-to-vec followed by the ordinary typed reduction.
        # Lower that same tree in the independent M36 reference emitter rather
        # than introducing a second parity interpretation here.
        return _expr(lower_reduction(value))
    if isinstance(value, (expr.Delay, expr.Pipeline)):
        return _expr(value.expression)
    raise EquivalenceError(f"unsupported reference expression: {type(value).__name__}")


def _materialize_reference_expression(
    expression: expr.Expression,
    definitions: tuple[object, ...],
) -> expr.Expression:
    """Boundedly expose retained calls and functional regions for M36 RTL.

    The semantic relation is unchanged: nominal reductions replay their frozen
    source-order plan, calls use their exact monomorphic definitions, and a
    compact functional region is expanded only at this final reference-emission
    boundary.  No optimizer or equality relation observes the materialized
    implementation tree.
    """

    def map_value(value: object) -> object:
        if isinstance(value, expr.Expression):
            return visit(value)
        if isinstance(value, tuple):
            return tuple(map_value(item) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            updates = {
                item.name: map_value(getattr(value, item.name))
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            return replace(value, **updates) if updates else value
        return value

    def visit(value: expr.Expression) -> expr.Expression:
        if isinstance(value, expr.Reduce):
            return visit(lower_reduction(value))
        if isinstance(value, expr.FunctionalRegion):
            elements = tuple(
                visit(item) for item in materialize_functional_region(value)
            )
            if value.kind.value == "map":
                return expr.Map(
                    value.binder.display_name,
                    value.binder.start,
                    value.binder.stop,
                    elements,
                    value.type,
                    origin=value.origin,
                )
            return expr.Generate(
                value.binder.display_name,
                value.binder.start,
                value.binder.stop,
                elements,
                value.type,
                origin=value.origin,
            )
        if isinstance(value, expr.Call):
            expanded = expand_callable_calls(value, definitions)
            return visit(expanded)
        updates = {
            item.name: map_value(getattr(value, item.name))
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        return replace(value, **updates) if updates else value

    try:
        return visit(expression)
    except (CallableExpansionError, FunctionalLoweringError, ValueError) as error:
        raise EquivalenceError(
            f"cannot materialize M36 reference expression: {error}"
        ) from error


def _fixed_convert_expr(value: expr.FixedConvert) -> str:
    operand = _expr(value.expression)
    if value.kind in {
        expr.FixedConversionKind.FROM_RAW,
        expr.FixedConversionKind.TO_RAW,
    }:
        return operand
    target_signed = isinstance(value.type, FixedType)
    if value.rational_denominator is not None:
        if not isinstance(value.expression, expr.Constant):
            raise EquivalenceError("rational fixed conversion requires a constant numerator")
        result = quantize_rational(
            value.expression.value,
            value.rational_denominator,
            fraction=value.type.fraction,
            width=value.type.width,
            signed=target_signed,
            rounding=value.rounding,
            overflow=value.overflow,
        )
        return sized_decimal(
            value.type.width,
            result,
            signed=target_signed,
        )

    source_fraction = getattr(value.expression.type, "fraction", 0)
    delta = value.type.fraction - source_fraction
    source_signed = isinstance(value.expression.type, (SIntType, FixedType))
    source = f"$signed({operand})" if source_signed else f"$unsigned({operand})"
    if delta >= 0:
        converted = source if delta == 0 else f"({source} <<< {delta})"
    else:
        shift = -delta
        magnitude = (
            f"(({source}) < 0 ? -({source}) : ({source}))"
            if source_signed else f"({source})"
        )
        quotient = f"({magnitude} >> {shift})"
        discarded = f"(({magnitude} & {(1 << shift) - 1}) != 0)"
        if value.rounding is expr.FixedRounding.NEAREST_EVEN:
            rounded = (
                f"(({magnitude} + {(1 << (shift - 1)) - 1} + "
                f"(({magnitude} >> {shift}) & 1)) >> {shift})"
            )
            converted = (
                f"(({source}) < 0 ? -({rounded}) : ({rounded}))"
                if source_signed else rounded
            )
        elif value.rounding is expr.FixedRounding.AWAY_ZERO:
            rounded = f"({quotient} + {discarded})"
            converted = (
                f"(({source}) < 0 ? -({rounded}) : ({rounded}))"
                if source_signed else rounded
            )
        elif value.rounding is expr.FixedRounding.FLOOR and source_signed:
            converted = f"(({source}) < 0 ? -({quotient} + {discarded}) : {quotient})"
        else:
            converted = (
                f"(({source}) < 0 ? -({quotient}) : ({quotient}))"
                if source_signed else quotient
            )
    if value.overflow is expr.FixedOverflow.SATURATE:
        minimum = -(1 << (value.type.width - 1)) if target_signed else 0
        maximum = (
            (1 << (value.type.width - 1)) - 1
            if target_signed else (1 << value.type.width) - 1
        )
        converted = (
            f"(({converted}) < {minimum} ? {minimum} : "
            f"(({converted}) > {maximum} ? {maximum} : ({converted})))"
        )
    sized = f"{value.type.width}'({converted})"
    return f"$signed({sized})" if target_signed else sized


def emit_reference_model(module_name: str, output_name: str, output_type: HardwareType,
                         inputs: tuple[tuple[str, HardwareType], ...],
                         expression: expr.Expression, *,
                         callable_definitions: Iterable[object] = (),
                         clock_name: str | None = None,
                         reset_name: str | None = None) -> str:
    """Emit an independent combinational Verilog reference model from typed IR."""
    if not module_name or not output_name:
        raise EquivalenceError("reference model requires module and output names")
    names = [name for name, _ in inputs]
    if len(names) != len(set(names)):
        raise EquivalenceError("reference model input names must be unique")
    if (clock_name is None) != (reset_name is None):
        raise EquivalenceError(
            "reference model clock and reset must be supplied together"
        )
    ports = []
    if clock_name is not None and reset_name is not None:
        if clock_name in names or reset_name in names or clock_name == reset_name:
            raise EquivalenceError("reference model clock/reset names must be unique")
        ports.extend((f"input logic {clock_name}", f"input logic {reset_name}"))
    ports.extend(f"input {_type(type_)} {name}" for name, type_ in inputs)
    ports.append(f"output {_type(output_type)} {output_name}")
    expression = _materialize_reference_expression(
        expression,
        tuple(callable_definitions),
    )
    return "\n".join((
        "`default_nettype none", f"module {module_name}(", ",\n".join(f"  {port}" for port in ports),
        ");", f"  assign {output_name} = {_expr(expression)};", "endmodule", "`default_nettype wire", "",
    ))


def make_equivalence_property(reference: expr.Expression, implementation: expr.Expression,
                              *, candidate_class: str, reference_root: str,
                              implementation_root: str, reference_timing: TimingInfo | None = None,
                              implementation_timing: TimingInfo | None = None,
                              inputs: tuple[str, ...] = (), reference_output: str = "ref_y",
                              implementation_output: str = "impl_y", source_origin=None,
                              selected_origin=None,
                              clock_domain_contract: ClockDomain | None = None,
                              ) -> EquivalenceProperty:
    """Validate a frozen candidate class and construct the separate M36 IR."""
    if candidate_class not in _SUPPORTED_CLASSES:
        raise EquivalenceError(f"unsupported M36 candidate class: {candidate_class}")
    if reference.type != implementation.type:
        raise EquivalenceError("equivalence canonical types differ")
    left = reference_timing or timing_info(reference)
    right = implementation_timing or timing_info(implementation)
    if left.ii != 1 or right.ii != 1:
        raise EquivalenceError("M36 equivalence requires II=1")
    delta = right.latency - left.latency
    if delta < 0:
        raise EquivalenceError("implementation latency cannot precede reference")
    relation = (EquivalenceRelation.SAME_CYCLE_VALUE if delta == 0
                else EquivalenceRelation.FIXED_LATENCY_VALUE)
    if relation is EquivalenceRelation.FIXED_LATENCY_VALUE and (left.clock_domain != right.clock_domain or left.reset_domain != right.reset_domain):
        raise EquivalenceError("fixed-latency candidates require matching clock/reset domains")
    if relation is EquivalenceRelation.FIXED_LATENCY_VALUE and (left.clock_domain is None or left.reset_domain is None):
        raise EquivalenceError("fixed-latency candidates require clock and reset domains")
    if clock_domain_contract is not None:
        try:
            clock_domain_contract.validate()
        except ValueError as error:
            raise EquivalenceError(str(error)) from error
        if clock_domain_contract.power_up is not PowerUpPolicy.UNSPECIFIED:
            raise EquivalenceError(
                "M36 executable equivalence does not support power_up reset"
            )
        if relation is EquivalenceRelation.FIXED_LATENCY_VALUE and (
            clock_domain_contract.clock != left.clock_domain
            or clock_domain_contract.reset != left.reset_domain
        ):
            raise EquivalenceError(
                "M36 physical clock/reset contract does not match candidate timing"
            )
    effective_contract = clock_domain_contract
    if (
        effective_contract is None
        and relation is EquivalenceRelation.FIXED_LATENCY_VALUE
    ):
        # Backward-compatible M36 callers predate physical-domain metadata.
        # Their timed relation is exactly the legacy rising/synchronous/high
        # contract, so seal that contract explicitly rather than guessing at
        # miter emission time.
        assert left.clock_domain is not None and left.reset_domain is not None
        effective_contract = ClockDomain(left.clock_domain, left.reset_domain)
    release_cycles = (
        0
        if effective_contract is None
        else effective_contract.reset_release_cycles
    )
    return EquivalenceProperty(
        stable_equivalence_id(reference_root, implementation_root, candidate_class), relation,
        reference_root, implementation_root, reference.type, inputs, reference_output,
        implementation_output, left.latency, right.latency, left.ii, right.ii,
        left.clock_domain, right.clock_domain, left.reset_domain, right.reset_domain,
        delta, (
            ComparisonWindow.same_cycle()
            if delta == 0
            else ComparisonWindow.reset_fill(
                delta,
                reset_release_cycles=release_cycles,
            )
        ),
        source_origin, selected_origin, candidate_class, effective_contract,
    )


def _timed_formal_domain(
    property_: EquivalenceProperty,
    *,
    clock_name: str,
    reset_name: str,
    used_names: set[str],
) -> FormalDomainRendering:
    if property_.relation_kind is not EquivalenceRelation.FIXED_LATENCY_VALUE:
        raise EquivalenceError("same-cycle equivalence has no reset/fill domain")
    contract = property_.clock_domain_contract
    if contract is None:
        if not property_.reference_clock or not property_.reference_reset:
            raise EquivalenceError("timed miter requires clock and reset")
        contract = ClockDomain(
            property_.reference_clock,
            property_.reference_reset,
        )
    try:
        return render_formal_domain(
            contract,
            clock_name=clock_name,
            reset_name=reset_name,
            used_names=used_names,
        )
    except FormalDomainRenderingError as error:
        raise EquivalenceError(str(error)) from error


def _timed_trace_reset_name(property_: EquivalenceProperty) -> str:
    """Compatibility reset spelling for historical source-only runners.

    This cannot see physical binding names and therefore is not authoritative
    when a user port collides with a checker-private identifier.  Compiler-owned
    routes instead retain :class:`MiterTraceMetadata` from the actual emission.
    """

    rendering = _timed_formal_domain(
        property_,
        clock_name="clock",
        reset_name="reset",
        used_names={
            "clock",
            "reset",
            "reference_value",
            "implementation_value",
            "reference_history",
            "sample_valid",
            *property_.inputs,
        },
    )
    return rendering.reset_active


def emit_miter_with_metadata(
    property_: EquivalenceProperty,
    bindings: BindingMap,
    *,
    reference_module: str,
    implementation_module: str,
    clock_name: str = "clock",
    reset_name: str = "reset",
) -> MiterEmission:
    """Emit a deterministic miter and authoritative checker trace names."""
    required = [(side, item) for side in (BindingSide.REFERENCE, BindingSide.IMPLEMENTATION)
                for item in property_.inputs]
    required += [(BindingSide.REFERENCE, property_.reference_output),
                 (BindingSide.IMPLEMENTATION, property_.implementation_output)]
    if property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE:
        required += [(side, "clock") for side in (BindingSide.REFERENCE, BindingSide.IMPLEMENTATION)]
        required += [(side, "reset") for side in (BindingSide.REFERENCE, BindingSide.IMPLEMENTATION)]
    bindings.validate(required=tuple(required))
    ref = {(item.side, item.semantic_signal_id): item for item in bindings.entries}
    ref_out = ref[(BindingSide.REFERENCE, property_.reference_output)]
    impl_out = ref[(BindingSide.IMPLEMENTATION, property_.implementation_output)]
    if property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE:
        if not property_.reference_clock or not property_.reference_reset:
            raise EquivalenceError("timed miter requires clock and reset")
    used_names = {
        clock_name,
        reset_name,
        *(item.rtl_path for item in bindings.entries),
    }
    reference_value_name = allocate_private_rtl_identifier(
        "reference_value",
        semantic_identity=f"{property_.id}|m36|reference-output",
        used=used_names,
    )
    implementation_value_name = allocate_private_rtl_identifier(
        "implementation_value",
        semantic_identity=f"{property_.id}|m36|implementation-output",
        used=used_names,
    )
    reference_history_name: str | None = None
    sample_valid_name: str | None = None
    if property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE:
        reference_history_name = allocate_private_rtl_identifier(
            "reference_history",
            semantic_identity=f"{property_.id}|m36|reference-history",
            used=used_names,
        )
        sample_valid_name = allocate_private_rtl_identifier(
            "sample_valid",
            semantic_identity=f"{property_.id}|m36|comparison-valid",
            used=used_names,
        )
    lines = ["`default_nettype none", f"module m36_{property_.id.replace('.', '_')}("]
    ports = [f"  input logic {clock_name}", f"  input logic {reset_name}"]
    for semantic in property_.inputs:
        r = ref[(BindingSide.REFERENCE, semantic)]
        ports.append(f"  input {_type_from_binding(r)} {r.rtl_path}")
    lines.append(",\n".join(ports) + ");")
    lines.append(f"  logic [{ref_out.width - 1}:0] {reference_value_name};")
    lines.append(f"  logic [{impl_out.width - 1}:0] {implementation_value_name};")
    domain_rendering: FormalDomainRendering | None = None
    if property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE:
        domain_rendering = _timed_formal_domain(
            property_,
            clock_name=clock_name,
            reset_name=reset_name,
            used_names=used_names,
        )
        if not domain_rendering.domain.is_legacy_default:
            assert reference_history_name is not None
            assert sample_valid_name is not None
            lines.append(f"  logic [{ref_out.width - 1}:0] {reference_history_name} [0:{property_.latency_delta - 1}];")
            lines.append(f"  logic [{property_.latency_delta}:0] {sample_valid_name};")
            lines.extend(f"  {line}" for line in domain_rendering.support_lines)
    ref_connections = [f".{ref_out.rtl_path}({reference_value_name})"]
    impl_connections = [f".{impl_out.rtl_path}({implementation_value_name})"]
    for semantic in property_.inputs:
        ref_connections.append(f".{ref[(BindingSide.REFERENCE, semantic)].rtl_path}({ref[(BindingSide.REFERENCE, semantic)].rtl_path})")
        impl_connections.append(f".{ref[(BindingSide.IMPLEMENTATION, semantic)].rtl_path}({ref[(BindingSide.REFERENCE, semantic)].rtl_path})")
    if property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE:
        ref_connections.append(f".{ref[(BindingSide.REFERENCE, 'clock')].rtl_path}({clock_name})")
        ref_connections.append(f".{ref[(BindingSide.REFERENCE, 'reset')].rtl_path}({reset_name})")
        impl_connections.append(f".{ref[(BindingSide.IMPLEMENTATION, 'clock')].rtl_path}({clock_name})")
        impl_connections.append(f".{ref[(BindingSide.IMPLEMENTATION, 'reset')].rtl_path}({reset_name})")
    lines.append(f"  {reference_module} reference_i({', '.join(ref_connections)});")
    lines.append(f"  {implementation_module} implementation_i({', '.join(impl_connections)});")
    if property_.relation_kind is EquivalenceRelation.SAME_CYCLE_VALUE:
        lines.append(
            f"  always_comb assert ({reference_value_name} == "
            f"{implementation_value_name});"
        )
    else:
        assert domain_rendering is not None
        assert reference_history_name is not None
        assert sample_valid_name is not None
        if domain_rendering.domain.is_legacy_default:
            lines.append(f"  logic [{ref_out.width - 1}:0] {reference_history_name} [0:{property_.latency_delta - 1}];")
            lines.append(f"  logic [{property_.latency_delta}:0] {sample_valid_name};")
        lines.append("  initial begin")
        lines.append(f"    {sample_valid_name} = '0;")
        for index in range(property_.latency_delta):
            lines.append(f"    {reference_history_name}[{index}] = '0;")
        lines.append(f"    assume({domain_rendering.external_reset_asserted});")
        lines.append("  end")
        lines.append(f"  always_ff @({domain_rendering.history_event}) begin")
        if domain_rendering.asynchronous_assertion_event is not None:
            # Keep the physical reset expression as the first branch so Yosys
            # recognizes the asynchronous control instead of treating the
            # normalized checker reset as a second clock edge.
            lines.append(
                f"    if ({domain_rendering.external_reset_asserted}) begin "
                f"{sample_valid_name} <= '0;"
            )
        else:
            lines.append(
                f"    if ({domain_rendering.reset_active}) begin "
                f"{sample_valid_name} <= '0;"
            )
        for index in range(property_.latency_delta):
            lines.append(f"      {reference_history_name}[{index}] <= '0;")
        if domain_rendering.release_tracker_name is not None:
            lines.append(
                f"    end else if ({domain_rendering.reset_active}) begin "
                f"{sample_valid_name} <= '0;"
            )
            for index in range(property_.latency_delta):
                lines.append(f"      {reference_history_name}[{index}] <= '0;")
        lines.append("    end else begin")
        lines.append(
            f"      {sample_valid_name} <= {{{sample_valid_name}["
            f"{property_.latency_delta - 1}:0], 1'b1}};"
        )
        lines.append(
            f"      {reference_history_name}[0] <= {reference_value_name};"
        )
        for index in range(1, property_.latency_delta):
            lines.append(
                f"      {reference_history_name}[{index}] <= "
                f"{reference_history_name}[{index - 1}];"
            )
        lines.append("    end")
        lines.append("  end")
        lines.append(
            f"  always_ff @({domain_rendering.sample_event}) if "
            f"(!{domain_rendering.reset_active} && "
            f"{sample_valid_name}[{property_.latency_delta}]) assert "
            f"({reference_history_name}[{property_.latency_delta - 1}] == "
            f"{implementation_value_name});"
        )
    lines += ["endmodule", "`default_nettype wire", ""]
    trace_metadata = MiterTraceMetadata(
        reference_value_name,
        implementation_value_name,
        None if domain_rendering is None else domain_rendering.reset_active,
        sample_valid_name,
    )
    return MiterEmission("\n".join(lines), trace_metadata)


def emit_miter(property_: EquivalenceProperty, bindings: BindingMap, *,
              reference_module: str, implementation_module: str,
              clock_name: str = "clock", reset_name: str = "reset") -> str:
    """Emit a deterministic two-sided miter from explicit binding metadata."""

    return emit_miter_with_metadata(
        property_,
        bindings,
        reference_module=reference_module,
        implementation_module=implementation_module,
        clock_name=clock_name,
        reset_name=reset_name,
    ).source


def _type_from_binding(binding: EquivalenceBinding) -> str:
    if binding.signedness == "signed":
        return f"logic signed [{binding.width - 1}:0]"
    return "logic" if binding.width == 1 else f"logic [{binding.width - 1}:0]"


def artifact_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def publish_bindings(module: Module, *, side: BindingSide, selected_ir_identity: str,
                     backend: str, artifact_hash_value: str,
                     rtl_names: dict[str, str], map_version: int = 2) -> tuple[EquivalenceBinding, ...]:
    """Create backend-published bindings from explicit semantic-to-RTL names."""
    if len(module.clock_domains) > 1:
        raise EquivalenceError(
            "M36 executable equivalence supports exactly one clock/reset domain"
        )
    if any(
        domain.power_up is not PowerUpPolicy.UNSPECIFIED
        for domain in module.clock_domains
    ):
        raise EquivalenceError(
            "M36 executable equivalence does not support power_up reset"
        )
    if module.elastic_pipeline_regions:
        raise EquivalenceError(
            "M36 fixed-latency equivalence does not support variable-latency "
            "elastic pipeline(auto) regions"
        )
    protocol_ports = tuple(
        port
        for port in module.ports
        if port.protocol is not InterfaceProtocol.WIRE
    )
    if protocol_ports:
        rendered = ", ".join(
            f"{port.name} ({port.protocol.value})" for port in protocol_ports
        )
        raise EquivalenceError(
            "M36 executable value equivalence does not support protocol-valued "
            f"top ports: {rendered}"
        )
    if module.aggregate_protocol_endpoints or module.aggregate_protocol_connections:
        raise EquivalenceError(
            "M36 executable value equivalence does not support aggregate "
            "protocol endpoints or connections, including scalar-only members"
        )
    if not rtl_names:
        raise EquivalenceError("backend must publish explicit RTL names for M36 bindings")
    result: list[EquivalenceBinding] = []
    for port in module.ports:
        semantic = f"port:{port.name}"
        if semantic not in rtl_names:
            raise EquivalenceError(f"missing backend RTL name for {semantic}")
        result.append(EquivalenceBinding(
            map_version, side, semantic, selected_ir_identity, module.name,
            rtl_names[semantic], port.type.width, signedness(port.type),
            SignalRole.INPUT if port.direction is PortDirection.INPUT else SignalRole.OUTPUT,
            port.domain or module.clock, module.reset, backend, artifact_hash_value,
        ))
    if module.clock:
        semantic = "clock"
        if semantic not in rtl_names:
            raise EquivalenceError("missing backend RTL name for clock")
        result.append(EquivalenceBinding(map_version, side, semantic, selected_ir_identity,
                                         module.name, rtl_names[semantic], 1, "bit",
                                         SignalRole.CLOCK, module.clock, module.reset,
                                         backend, artifact_hash_value))
    if module.reset:
        semantic = "reset"
        if semantic not in rtl_names:
            raise EquivalenceError("missing backend RTL name for reset")
        result.append(EquivalenceBinding(map_version, side, semantic, selected_ir_identity,
                                         module.name, rtl_names[semantic], 1, "bit",
                                         SignalRole.RESET, module.clock, module.reset,
                                         backend, artifact_hash_value))
    return tuple(result)


def unavailable_result(property_: EquivalenceProperty, *, backend: str, reason: str,
                      mode: EquivalenceMode = EquivalenceMode.BMC, depth: int | None = None) -> EquivalenceResult:
    return EquivalenceResult(property_.id, EquivalenceStatus.SKIPPED, mode, None, None, depth,
                             property_.relation_kind, property_.latency_delta, backend, "", "",
                             2, property_.implementation_root, property_.source_origin,
                             property_.selected_origin, reason=reason)


def formal_tools_available() -> tuple[str, ...]:
    return discover_tool_inventory(
        ("yosys", "sby", "yosys-smtbmc"),
        which=shutil.which,
        require_truthy_path=True,
    ).available


def run_equivalence_formal(property_: EquivalenceProperty, source: str, *, top: str,
                           backend: str = "direct_sv", mode: EquivalenceMode = EquivalenceMode.BMC,
                           depth: int = 20, solver: str = "z3",
                           reference_hash: str | None = None,
                           implementation_hash: str | None = None,
                           timeout_seconds: int = 120,
                           work_directory: Path | None = None,
                           trace_metadata: MiterTraceMetadata | None = None,
                           ) -> EquivalenceResult:
    """Execute a published equivalence miter while preserving M36 statuses."""
    if (
        mode is EquivalenceMode.BMC
        and not property_.comparison_window.bmc_depth_reaches_comparison(depth)
    ):
        return EquivalenceResult(
            property_.id,
            EquivalenceStatus.UNKNOWN,
            mode,
            None,
            solver,
            depth,
            property_.relation_kind,
            property_.latency_delta,
            backend,
            reference_hash or artifact_hash(source + "|reference"),
            implementation_hash or artifact_hash(source + "|implementation"),
            2,
            property_.implementation_root,
            property_.source_origin,
            property_.selected_origin,
            reason=property_.comparison_window.bmc_unreached_reason(depth),
        )
    from zlang.formal import run_verilog_formal
    from zlang.ir.formal import ProofMode
    if trace_metadata is None:
        # Compatibility for historical low-level callers which provide an
        # already assembled source string.  Compiler-owned preparation always
        # supplies emission metadata; only that path is collision-proof.
        trace_metadata = MiterTraceMetadata(
            "reference_value",
            "implementation_value",
            (
                _timed_trace_reset_name(property_)
                if property_.relation_kind
                is EquivalenceRelation.FIXED_LATENCY_VALUE
                else None
            ),
            (
                "sample_valid"
                if property_.relation_kind
                is EquivalenceRelation.FIXED_LATENCY_VALUE
                else None
            ),
        )
    elif (
        property_.relation_kind is EquivalenceRelation.SAME_CYCLE_VALUE
        and (
            trace_metadata.reset is not None
            or trace_metadata.comparison_valid is not None
        )
    ):
        raise EquivalenceError(
            "same-cycle M36 trace metadata cannot publish timed reset/history signals"
        )
    elif (
        property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE
        and (
            trace_metadata.reset is None
            or trace_metadata.comparison_valid is None
        )
    ):
        raise EquivalenceError(
            "fixed-latency M36 trace metadata requires reset/history signals"
        )
    proof_mode = ProofMode.BMC if mode is EquivalenceMode.BMC else ProofMode.PROVE
    trace_bindings = [
        TraceBinding(
            "reference_output",
            trace_metadata.reference_output,
            property_.canonical_type.width,
            property_.canonical_type,
            signedness(property_.canonical_type),
        ),
        TraceBinding(
            "implementation_output",
            trace_metadata.implementation_output,
            property_.canonical_type.width,
            property_.canonical_type,
            signedness(property_.canonical_type),
        ),
    ]
    if property_.relation_kind is EquivalenceRelation.FIXED_LATENCY_VALUE:
        assert trace_metadata.reset is not None
        assert trace_metadata.comparison_valid is not None
        trace_bindings.extend((
            TraceBinding(
                "reset",
                trace_metadata.reset,
                1,
                BitType(),
                "bit",
            ),
            TraceBinding(
                "comparison_valid",
                trace_metadata.comparison_valid,
                property_.latency_delta + 1,
                BitsType(property_.latency_delta + 1),
                "bits",
            ),
        ))
    result = run_verilog_formal(source, top=top, property_id=property_.id,
                                mode=proof_mode, depth=depth, solver=solver,
                                source_origin=property_.source_origin,
                                systemverilog=True,
                                timeout_seconds=timeout_seconds,
                                work_directory=work_directory,
                                trace_bindings=tuple(trace_bindings),
                                comparison_window=property_.comparison_window)
    mapped = {
        "proven": EquivalenceStatus.PROVEN,
        "failed": EquivalenceStatus.FAILED,
        "bounded_pass": EquivalenceStatus.BOUNDED_PASS,
        "unknown": EquivalenceStatus.UNKNOWN,
        "skipped": EquivalenceStatus.SKIPPED,
    }[result.status.value]
    counterexample = None
    if result.counterexample is not None:
        failure_cycle = result.counterexample.cycle
        sample_cycle = originating_sample_cycle(
            failure_cycle, property_.comparison_window
        )
        counterexample = EquivalenceCounterexample(
            property_.id,
            failure_cycle=failure_cycle,
            sample_cycle=sample_cycle,
            raw_trace=result.counterexample.raw_trace,
            values=result.counterexample.values)
    return EquivalenceResult(
        property_.id, mapped, mode, result.engine, result.solver, result.depth,
        property_.relation_kind, property_.latency_delta, backend,
        reference_hash or artifact_hash(source + "|reference"),
        implementation_hash or artifact_hash(source + "|implementation"), 2,
        property_.implementation_root, property_.source_origin,
        property_.selected_origin, counterexample, result.reason)


__all__ = [
    "MiterEmission",
    "MiterTraceMetadata",
    "artifact_hash",
    "emit_miter",
    "emit_miter_with_metadata",
    "emit_reference_model",
    "formal_tools_available",
    "make_equivalence_property",
    "publish_bindings",
    "run_equivalence_formal",
    "unavailable_result",
]
