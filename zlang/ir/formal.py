"""Backend-independent formal verification IR and safety-property generation.

The objects in this module deliberately contain semantic names, not RTL names.
Backends publish :class:`SignalBinding` records separately; the harness emitter
is the only layer that resolves those records to implementation signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import re
from typing import Iterable

from zlang.ir.interfaces import (
    CreditSignal,
    InterfaceProtocol,
    PacketSignal,
    ReadyValidSignal,
    VirtualChannelCreditSignal,
)
from zlang.ir.cdc import (
    ClockDomain,
    ClockEdge,
    PowerUpPolicy,
    ResetMode,
    ResetPolarity,
    ResetReleaseMode,
)
from zlang.ir.csr import csr_internal_port_names, derived_state_bindings
import zlang.ir.expressions as ir_expr
from zlang.ir import packing as ir_packing
from zlang.ir.module import Module, PortDirection
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
from zlang.ir.verification import VerificationGoalKind
from zlang.ir.formal_observations import (
    RequestResponseObservationSignal,
    fifo_observation_id,
    port_observation_id,
    register_observation_id,
    request_response_observation_id,
    rule_fire_observation_id,
)
from zlang.ir.formal_predicates import (
    Binary as PredicateBinary,
    Constant as PredicateConstant,
    FormalBinaryOperator,
    FormalPredicate,
    FormalPredicateError,
    FormalSignedness,
    FormalUnaryOperator,
    Mux as PredicateMux,
    ObservationCycle,
    ObservationRef,
    Unary as PredicateUnary,
    require_predicate,
)
from zlang.source import SourceOrigin
from zlang.common.systemverilog import (
    render_ordered_comparison,
    render_right_shift,
)
from zlang.backend.identifiers import allocate_private_rtl_identifier
from zlang.formal_domain import (
    FormalDomainRendering,
    FormalDomainRenderingError,
    POWER_UP_FORMAL_DOMAIN_REASON,
    formal_domain_applicability_reason,
    render_formal_domain,
)


class FormalError(ValueError):
    """A property or binding cannot be represented safely."""


class PropertyKind(str, Enum):
    ASSUMPTION = "assumption"
    ASSERTION = "assertion"


class TemporalForm(str, Enum):
    SAME_CYCLE = "same_cycle"
    NEXT_CYCLE = "next_cycle"
    STABLE_WHILE = "stable_while"
    BOUNDED_IMPLICATION = "bounded_implication"


class FormalPropertyClassification(str, Enum):
    """Truthful strength of one generated safety property.

    A range assertion over the complete representation of a fixed-width scalar
    is useful as a binding/width smoke check, but it cannot establish a
    behavioral invariant beyond the RTL representation itself.  Keeping that
    distinction in typed IR prevents reports from overstating the result while
    preserving the historical executable property and its stable identity.
    """

    BEHAVIORAL = "behavioral"
    REPRESENTATION_INVARIANT = "representation_invariant"


class Ownership(str, Enum):
    ENVIRONMENT = "environment"
    IMPLEMENTATION = "implementation"
    SOURCE_ENDPOINT = "source_endpoint"
    SINK_ENDPOINT = "sink_endpoint"
    SCHEDULER = "scheduler"


class FormalStatus(str, Enum):
    PROVEN = "proven"
    FAILED = "failed"
    BOUNDED_PASS = "bounded_pass"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class ProofMode(str, Enum):
    PROVE = "prove"
    BMC = "bmc"


class CoverStatus(str, Enum):
    """Outcome of one bounded reachability query.

    Cover results deliberately use a status vocabulary separate from safety
    proofs.  In particular, exhausting a bounded search without a witness is
    not a proof that the cover is unreachable.
    """

    WITNESSED = "witnessed"
    BOUNDED_UNREACHED = "bounded_unreached"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SignalBinding:
    """Stable semantic-to-RTL binding published by either backend."""

    semantic_signal_id: str
    rtl_module: str
    rtl_name: str
    width: int
    direction: str
    clock_domain: str | None = None
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.semantic_signal_id or not self.rtl_module or not self.rtl_name:
            raise FormalError("signal bindings require semantic ID, module, and RTL name")
        if self.width < 1:
            raise FormalError("signal binding width must be positive")
        if self.direction not in {"input", "output", "internal"}:
            raise FormalError(f"unsupported binding direction: {self.direction}")


@dataclass(frozen=True)
class FormalProperty:
    id: str
    kind: PropertyKind
    clock: str
    reset_condition: str | None
    expression: str
    temporal_form: TemporalForm
    ownership: Ownership
    source_origin: SourceOrigin | None = None
    generated_from: str | None = None
    relevant_signals: tuple[str, ...] = ()
    antecedent: str | None = None
    consequent: str | None = None
    min_delay: int | None = None
    max_delay: int | None = None
    # ``expression`` and the optional antecedent/consequent strings are stable
    # report spellings retained for compatibility.  Executable meaning lives
    # exclusively in this structured predicate.
    predicate: FormalPredicate | None = None
    non_executable_reason: str | None = None
    classification: FormalPropertyClassification = (
        FormalPropertyClassification.BEHAVIORAL
    )

    def __post_init__(self) -> None:
        if not self.id or not self.clock or not self.expression:
            raise FormalError("formal properties require an id, clock, and expression")
        if not isinstance(self.classification, FormalPropertyClassification):
            raise FormalError(
                "formal property classification must be a "
                "FormalPropertyClassification"
            )
        if self.predicate is not None:
            try:
                require_predicate(self.predicate)
            except FormalPredicateError as error:
                raise FormalError(str(error)) from error
            observed = self.predicate.observation_ids()
            if self.relevant_signals and tuple(self.relevant_signals) != observed:
                raise FormalError(
                    f"property '{self.id}' relevant_signals do not match its "
                    "structured predicate observations"
                )
            if not self.relevant_signals:
                object.__setattr__(self, "relevant_signals", observed)
        if (
            self.temporal_form is TemporalForm.NEXT_CYCLE
            and not self.consequent
            and self.predicate is None
        ):
            raise FormalError("next_cycle properties require a consequent")
        if self.temporal_form is TemporalForm.BOUNDED_IMPLICATION:
            if self.antecedent is None or self.consequent is None:
                raise FormalError("bounded implication requires antecedent and consequent")
            if self.min_delay is None or self.max_delay is None or self.max_delay < self.min_delay:
                raise FormalError("bounded implication requires a valid delay range")


@dataclass(frozen=True)
class CoverProperty:
    """One backend-independent, bounded reachability goal.

    ``expression`` is a stable report spelling only.  As with
    :class:`FormalProperty`, executable meaning lives exclusively in the
    structured predicate and all observations are resolved through explicit
    backend-published bindings.
    """

    id: str
    clock: str
    reset_condition: str | None
    expression: str
    predicate: FormalPredicate | None
    source_origin: SourceOrigin | None = None
    generated_from: str | None = None
    relevant_signals: tuple[str, ...] = ()
    non_executable_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.clock or not self.expression:
            raise FormalError("cover properties require an id, clock, and expression")
        if self.predicate is None:
            if self.non_executable_reason is None:
                raise FormalError(
                    f"cover property '{self.id}' requires a structured predicate"
                )
            return
        try:
            require_predicate(self.predicate)
        except FormalPredicateError as error:
            raise FormalError(str(error)) from error
        observed = self.predicate.observation_ids()
        if self.relevant_signals and tuple(self.relevant_signals) != observed:
            raise FormalError(
                f"cover property '{self.id}' relevant_signals do not match its "
                "structured predicate observations"
            )
        if not self.relevant_signals:
            object.__setattr__(self, "relevant_signals", observed)


@dataclass(frozen=True)
class FormalDesign:
    module_name: str
    properties: tuple[FormalProperty, ...]
    bindings: tuple[SignalBinding, ...]
    covers: tuple[CoverProperty, ...] = ()
    connected_backend: str | None = None
    connected_artifact_hash: str | None = None
    connected_module: str | None = None
    implementation_text: str | None = None
    dut_ports: tuple[SignalBinding, ...] = ()
    non_executable_reason: str | None = None
    # Exact source-side physical contracts used to interpret clock/reset
    # observations.  Kept after connection so harness rendering never has to
    # infer reset behavior from an RTL token or generated name.
    clock_domains: tuple[ClockDomain, ...] = ()

    def __post_init__(self) -> None:
        semantic_ids = tuple(item.semantic_signal_id for item in self.bindings)
        if len(semantic_ids) != len(set(semantic_ids)):
            duplicate = next(item for item in semantic_ids if semantic_ids.count(item) > 1)
            raise FormalError(f"duplicate semantic signal binding: {duplicate}")
        if (self.connected_backend is None) != (self.connected_artifact_hash is None):
            raise FormalError("connected formal design requires backend and artifact hash together")
        if self.connected_artifact_hash is not None:
            if not self.connected_module or self.implementation_text is None:
                raise FormalError(
                    "connected formal design requires an implementation module and RTL text"
                )
        domain_keys = tuple((item.clock, item.reset) for item in self.clock_domains)
        if len(domain_keys) != len(set(domain_keys)):
            raise FormalError("formal design contains duplicate clock/reset domains")


@dataclass(frozen=True)
class Counterexample:
    property_id: str
    cycle: int | None = None
    values: tuple[tuple[str, str], ...] = ()
    raw_trace: str | None = None


@dataclass(frozen=True)
class FormalResult:
    property_id: str
    status: FormalStatus
    mode: ProofMode
    engine: str | None
    solver: str | None
    depth: int | None
    counterexample: Counterexample | None = None
    source_origin: SourceOrigin | None = None
    tool_versions: tuple[tuple[str, str], ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status is FormalStatus.BOUNDED_PASS and self.mode is not ProofMode.BMC:
            raise FormalError("bounded_pass is only valid for BMC mode")
        if self.status is FormalStatus.PROVEN and self.mode is not ProofMode.PROVE:
            raise FormalError("proven is only valid for prove mode")
        if (self.counterexample is not None) != (
            self.status is FormalStatus.FAILED
        ):
            raise FormalError(
                "failed formal results require exactly one counterexample"
            )


@dataclass(frozen=True)
class CoverWitness:
    property_id: str
    cycle: int
    values: tuple[tuple[str, str], ...] = ()
    raw_trace: str | None = None

    def __post_init__(self) -> None:
        if not self.property_id:
            raise FormalError("cover witness requires a property id")
        if self.cycle < 0:
            raise FormalError("cover witness cycle must be non-negative")


@dataclass(frozen=True)
class CoverResult:
    property_id: str
    status: CoverStatus
    engine: str | None
    solver: str | None
    depth: int | None
    witness: CoverWitness | None = None
    source_origin: SourceOrigin | None = None
    tool_versions: tuple[tuple[str, str], ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.property_id:
            raise FormalError("cover result requires a property id")
        if not isinstance(self.status, CoverStatus):
            raise FormalError("cover result status must be a CoverStatus")
        if self.depth is not None and self.depth < 1:
            raise FormalError("cover result depth must be positive")
        if self.status is CoverStatus.WITNESSED:
            if self.witness is None:
                raise FormalError("witnessed cover result requires witness metadata")
            if self.witness.property_id != self.property_id:
                raise FormalError("cover witness property id does not match its result")
            if self.depth is not None and self.witness.cycle > self.depth:
                raise FormalError("cover witness cycle exceeds the executed depth")
        elif self.witness is not None:
            raise FormalError("only witnessed cover results may carry witness metadata")
        if self.status is CoverStatus.BOUNDED_UNREACHED and self.depth is None:
            raise FormalError("bounded_unreached cover result requires a depth")


def _stable_id(family: str, name: str, detail: str) -> str:
    digest = hashlib.sha256(f"{family}|{name}|{detail}".encode()).hexdigest()[:12]
    return f"m35.{family}.{name}.{digest}"


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


def _semantic_predicate(expression: object) -> FormalPredicate:
    """Lower the frozen typed contract subset without executable strings."""
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
                "quantized fixed-point contract expressions require M36 reference equivalence"
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
            expression.elements,
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
        lsb = sum(widths[expression.index + 1 :])
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
        lsb = (aggregate.length - expression.index - 1) * element_width
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
        # MSB-first vector layout and lower the selection to the existing
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
            lsb = (aggregate.length - index - 1) * element_width
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
    raise FormalError(f"unsupported contract expression for formal lowering: {expression!r}")


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


def generate_properties(module: Module) -> FormalDesign:
    """Generate the frozen M35 safety families from typed semantic IR."""
    # Multi-domain automatic state-property families remain outside the
    # frozen M35 subset, but source goals in one exact supported domain may
    # still observe that state.  Preserve the complete module for semantic
    # bindings while filtering only the automatic-property generation view.
    binding_module = module
    # Purely combinational modules have no sampling domain. They still expose
    # stable public bindings, but automatic sequential/protocol properties are
    # omitted until a clocked selected IR exists; source contracts retain their
    # own explicit clock and continue to lower normally.
    if module.clock is None and len(module.clock_domains) != 1:
        module = replace(
            module,
            registers=(), fifos=(), csr_blocks=(), rules=(), rule_priorities=(),
            ports=tuple(port for port in module.ports if port.protocol is InterfaceProtocol.WIRE),
        )
    properties: list[FormalProperty] = []
    covers: list[CoverProperty] = []
    for register in module.registers:
        state = _observation(register_observation_id(register.name), register.type)
        signedness = _formal_signedness(register.type)
        if signedness is FormalSignedness.BIT:
            minimum = 0
            maximum = 1
            bounds = _or(
                _equal(state, _constant(0, 1, FormalSignedness.BIT)),
                _equal(state, _constant(1, 1, FormalSignedness.BIT)),
            )
        elif signedness is FormalSignedness.BITS:
            minimum = 0
            maximum = (1 << register.type.width) - 1
            bounds = _equal(state, state)
        elif signedness is FormalSignedness.SIGNED:
            minimum = -(1 << (register.type.width - 1))
            maximum = (1 << (register.type.width - 1)) - 1
        elif isinstance(register.type, EnumType):
            if register.type.explicit_codes is not None:
                minimum = min(register.type.codes)
                maximum = max(register.type.codes)
                comparisons = tuple(
                    _equal(
                        state,
                        _constant(code, register.type.width, signedness),
                    )
                    for code in register.type.codes
                )
                bounds = comparisons[0]
                for comparison in comparisons[1:]:
                    bounds = _or(bounds, comparison)
            else:
                minimum = 0
                maximum = len(register.type.members) - 1
        else:
            minimum = 0
            maximum = (1 << register.type.width) - 1
        if (
            signedness not in {FormalSignedness.BIT, FormalSignedness.BITS}
            and not (
                isinstance(register.type, EnumType)
                and register.type.explicit_codes is not None
            )
        ):
            bounds = _and(
                _ordered(
                    FormalBinaryOperator.GREATER_EQUAL,
                    state,
                    _constant(minimum, register.type.width, signedness),
                ),
                _ordered(
                    FormalBinaryOperator.LESS_EQUAL,
                    state,
                    _constant(maximum, register.type.width, signedness),
                ),
            )
        properties.append(_property(
            "register", register.name + ".range",
            _type_range(register.type).replace("state", register.name), module,
            ownership=Ownership.IMPLEMENTATION,
            generated_from=f"register:{register.name}", predicate=bounds,
            origin=register.initial.origin,
            classification=(
                FormalPropertyClassification.BEHAVIORAL
                if isinstance(register.type, EnumType)
                else FormalPropertyClassification.REPRESENTATION_INVARIANT
            ),
        ))
        if module.reset is not None:
            initial_value = getattr(register.initial, "value", 0)
            reset_predicate = _implies(
                _bit_observation("reset", ObservationCycle.PREVIOUS),
                _equal(
                    state,
                    _constant(initial_value, register.type.width, signedness),
                ),
            )
            properties.append(_property(
                "register", register.name + ".reset",
                f"previous({module.reset}) -> {register.name} == {initial_value}",
                module, ownership=Ownership.IMPLEMENTATION,
                generated_from=f"register:{register.name}:reset",
                predicate=reset_predicate, temporal=TemporalForm.NEXT_CYCLE,
                antecedent=module.reset,
                consequent=f"{register.name} == {initial_value}",
                origin=register.initial.origin, reset_condition=None,
            ))
    for fifo in module.fifos:
        n, d = fifo.name, fifo.depth
        origin = fifo.data.origin if fifo.data is not None else fifo.source_origin
        count_type = UIntType(fifo.count_width)
        count = _observation(fifo_observation_id(n, "count"), count_type)
        push = _bit_observation(fifo_observation_id(n, "push"))
        pop = _bit_observation(fifo_observation_id(n, "pop"))
        empty = _bit_observation(fifo_observation_id(n, "empty"))
        full = _bit_observation(fifo_observation_id(n, "full"))
        front = _observation(
            fifo_observation_id(n, "front"), fifo.element_type
        )
        bounds = _ordered(
            FormalBinaryOperator.LESS_EQUAL,
            count,
            _constant(d, fifo.count_width, FormalSignedness.UNSIGNED),
        )
        no_pop = _or(_not(empty), _not(pop))
        no_push = _or(_or(_not(full), _not(push)), pop)
        work_width = fifo.count_width + 2
        previous_count = _resize(
            _previous(count), work_width, FormalSignedness.UNSIGNED
        )
        previous_push = _resize(
            _previous(push), work_width, FormalSignedness.UNSIGNED
        )
        previous_pop = _resize(
            _previous(pop), work_width, FormalSignedness.UNSIGNED
        )
        conservation_value = _binary(
            FormalBinaryOperator.SUBTRACT,
            _binary(
                FormalBinaryOperator.ADD,
                previous_count,
                previous_push,
                work_width,
                FormalSignedness.UNSIGNED,
            ),
            previous_pop,
            work_width,
            FormalSignedness.UNSIGNED,
        )
        conservation = _without_previous_reset(
            module,
            _equal(
                _resize(count, work_width, FormalSignedness.UNSIGNED),
                conservation_value,
            ),
        )
        front_antecedent = _and(
            _ordered(
                FormalBinaryOperator.GREATER,
                _previous(count),
                _constant(0, fifo.count_width, FormalSignedness.UNSIGNED),
            ),
            _not(_previous(pop)),
        )
        if module.reset is not None:
            front_antecedent = _and(
                front_antecedent,
                _not(_bit_observation("reset", ObservationCycle.PREVIOUS)),
            )
        front_stable = _implies(
            front_antecedent,
            _equal(front, _previous(front)),
        )
        for suffix, display, predicate, temporal in (
            ("bounds", f"{n}.count >= 0 && {n}.count <= {d}", bounds, TemporalForm.SAME_CYCLE),
            ("no_pop_empty", f"{n}.empty == 0 || {n}.pop == 0", no_pop, TemporalForm.SAME_CYCLE),
            ("no_push_full", f"{n}.full == 0 || {n}.push == 0 || {n}.pop == 1", no_push, TemporalForm.SAME_CYCLE),
            ("conservation", f"{n}.count == previous({n}.count + {n}.push - {n}.pop)", conservation, TemporalForm.NEXT_CYCLE),
            (
                "front_stable",
                f"previous({n}.count > 0 && !{n}.pop) -> "
                f"{n}.front == previous({n}.front)",
                front_stable,
                TemporalForm.NEXT_CYCLE,
            ),
        ):
            properties.append(_property(
                "fifo", f"{n}.{suffix}", display, module,
                ownership=Ownership.IMPLEMENTATION, generated_from=f"fifo:{n}",
                predicate=predicate, temporal=temporal, origin=origin,
            ))
    for port in module.ports:
        if port.protocol is InterfaceProtocol.READY_VALID:
            prefix = port.name
            ownership = (Ownership.ENVIRONMENT if port.direction is PortDirection.INPUT
                         else Ownership.SOURCE_ENDPOINT)
            kind = PropertyKind.ASSUMPTION if ownership is Ownership.ENVIRONMENT else PropertyKind.ASSERTION
            valid = _bit_observation(port_observation_id(prefix, "valid"))
            ready = _bit_observation(port_observation_id(prefix, "ready"))
            payload = _observation(
                port_observation_id(prefix, "payload"), port.type
            )
            antecedent = _and(_previous(valid), _not(_previous(ready)))
            if module.reset is not None:
                antecedent = _and(
                    antecedent,
                    _not(_bit_observation("reset", ObservationCycle.PREVIOUS)),
                )
            stable = _and(valid, _equal(payload, _previous(payload)))
            properties.append(_property(
                "ready_valid", f"{prefix}.stall",
                f"previous({prefix}.valid && !{prefix}.ready) -> "
                f"{prefix}.valid && stable({prefix}.payload)",
                module, ownership=ownership,
                generated_from=f"ready_valid:{prefix}",
                predicate=_implies(antecedent, stable), kind=kind,
                temporal=TemporalForm.NEXT_CYCLE,
                antecedent=f"{prefix}.valid && !{prefix}.ready",
                consequent=f"{prefix}.valid && stable({prefix}.payload)",
            ))
        if port.protocol is InterfaceProtocol.CREDIT:
            prefix = port.name
            capacity = port.capacity or 0
            credit_width = max(1, capacity.bit_length())
            send = _bit_observation(port_observation_id(prefix, "send"))
            returned = _bit_observation(port_observation_id(prefix, "return"))
            sender = port.direction is PortDirection.OUTPUT
            state_name = "credits" if sender else "occupancy"
            state = _observation(
                port_observation_id(prefix, state_name), UIntType(credit_width)
            )
            bounds = _ordered(
                FormalBinaryOperator.LESS_EQUAL, state,
                _constant(capacity, credit_width, FormalSignedness.UNSIGNED),
            )
            work_width = credit_width + 2
            previous_state = _resize(
                _previous(state), work_width, FormalSignedness.UNSIGNED
            )
            previous_send = _resize(
                _previous(send), work_width, FormalSignedness.UNSIGNED
            )
            previous_return = _resize(
                _previous(returned), work_width, FormalSignedness.UNSIGNED
            )
            if sender:
                # A sender consumes one available credit when it sends and
                # replenishes one when the environment returns a credit.
                previous_value = _binary(
                    FormalBinaryOperator.ADD,
                    _binary(
                        FormalBinaryOperator.SUBTRACT,
                        previous_state,
                        previous_send,
                        work_width,
                        FormalSignedness.UNSIGNED,
                    ),
                    previous_return,
                    work_width, FormalSignedness.UNSIGNED,
                )
            else:
                # A receiver records accepted incoming sends until its local
                # implementation returns the corresponding credits.
                previous_value = _binary(
                    FormalBinaryOperator.SUBTRACT,
                    _binary(
                        FormalBinaryOperator.ADD,
                        previous_state,
                        previous_send,
                        work_width,
                        FormalSignedness.UNSIGNED,
                    ),
                    previous_return,
                    work_width,
                    FormalSignedness.UNSIGNED,
                )
            conservation = _without_previous_reset(
                module,
                _equal(
                    _resize(state, work_width, FormalSignedness.UNSIGNED),
                    previous_value,
                ),
            )

            zero = _constant(0, credit_width, FormalSignedness.UNSIGNED)
            below_capacity = _ordered(
                FormalBinaryOperator.LESS, state,
                _constant(capacity, credit_width, FormalSignedness.UNSIGNED),
            )
            above_zero = _ordered(FormalBinaryOperator.GREATER, state, zero)
            if sender:
                implementation_legality = _or(_not(send), above_zero)
                environment_legality = _or(
                    _or(_not(returned), send), below_capacity
                )
                implementation_suffix = "transfer"
                implementation_display = (
                    f"{prefix}.send == 0 || {prefix}.credits > 0"
                )
                environment_suffix = "return_capacity"
                environment_display = (
                    f"{prefix}.return == 0 || {prefix}.send == 1 || "
                    f"{prefix}.credits < {capacity}"
                )
                implementation_ownership = Ownership.SOURCE_ENDPOINT
                conservation_display = (
                    f"{prefix}.credits == previous({prefix}.credits - "
                    f"{prefix}.send + {prefix}.return)"
                )
                reset_value = capacity
                unavailable_reason = None
            else:
                implementation_legality = _or(
                    _or(_not(returned), send), above_zero
                )
                environment_legality = _or(
                    _or(_not(send), returned), below_capacity
                )
                implementation_suffix = "return"
                implementation_display = (
                    f"{prefix}.return == 0 || {prefix}.send == 1 || "
                    f"{prefix}.occupancy > 0"
                )
                environment_suffix = "send_capacity"
                environment_display = (
                    f"{prefix}.send == 0 || {prefix}.return == 1 || "
                    f"{prefix}.occupancy < {capacity}"
                )
                implementation_ownership = Ownership.SINK_ENDPOINT
                conservation_display = (
                    f"{prefix}.occupancy == previous({prefix}.occupancy + "
                    f"{prefix}.send - {prefix}.return)"
                )
                reset_value = 0
                # Applicability is a property of one concrete backend route,
                # not of the backend-independent M35 property.  Receiver
                # occupancy is real implementation state and is already part
                # of the frozen credit predicate.  Publish its semantic
                # binding below and let each backend either connect it or
                # report the exact missing observation.
                unavailable_reason = None

            for suffix, display, predicate, temporal, ownership, kind in (
                (
                    "bounds",
                    f"{prefix}.{state_name} >= 0 && "
                    f"{prefix}.{state_name} <= {capacity}",
                    bounds,
                    TemporalForm.SAME_CYCLE,
                    Ownership.IMPLEMENTATION,
                    PropertyKind.ASSERTION,
                ),
                (
                    implementation_suffix,
                    implementation_display,
                    implementation_legality,
                    TemporalForm.SAME_CYCLE,
                    implementation_ownership,
                    PropertyKind.ASSERTION,
                ),
                (
                    environment_suffix,
                    environment_display,
                    environment_legality,
                    TemporalForm.SAME_CYCLE,
                    Ownership.ENVIRONMENT,
                    PropertyKind.ASSUMPTION,
                ),
                (
                    "conservation",
                    conservation_display,
                    conservation,
                    TemporalForm.NEXT_CYCLE,
                    Ownership.IMPLEMENTATION,
                    PropertyKind.ASSERTION,
                ),
            ):
                properties.append(_property(
                    "credit", f"{prefix}.{suffix}", display, module,
                    ownership=ownership, kind=kind,
                    generated_from=f"credit:{prefix}", predicate=predicate,
                    temporal=temporal,
                    non_executable_reason=unavailable_reason,
                ))
            if module.reset is not None:
                properties.append(_property(
                    "credit", f"{prefix}.reset",
                    f"previous({module.reset}) -> {prefix}.{state_name} == "
                    f"{reset_value}",
                    module, ownership=Ownership.IMPLEMENTATION,
                    generated_from=f"credit:{prefix}:reset",
                    predicate=_implies(
                        _bit_observation("reset", ObservationCycle.PREVIOUS),
                        _equal(
                            state,
                            _constant(
                                reset_value,
                                credit_width,
                                FormalSignedness.UNSIGNED,
                            ),
                        ),
                    ),
                    temporal=TemporalForm.NEXT_CYCLE,
                    antecedent=module.reset,
                    consequent=f"{prefix}.{state_name} == {reset_value}",
                    reset_condition=None,
                    non_executable_reason=unavailable_reason,
                ))
    # The parent connection owns one accepted-request ledger for both physical
    # ready/valid channels.  Keep these accounting properties separate from
    # the endpoint properties above so buffered and accepted work cannot be
    # conflated by a backend.
    for connection in module.request_response_connections:
        ids = {
            signal.value: request_response_observation_id(
                connection.semantic_id, signal
            )
            for signal in RequestResponseObservationSignal
        }
        tracker_width = max(1, connection.max_outstanding.bit_length())
        request_width = max(
            1,
            connection.request.request_buffer_depth.bit_length()
            if connection.request.request_buffer_depth
            else tracker_width,
        )
        response_width = max(
            1,
            connection.response.response_buffer_depth.bit_length()
            if connection.response.response_buffer_depth
            else tracker_width,
        )
        outstanding = _observation(ids["outstanding"], UIntType(tracker_width))
        request_accept = _bit_observation(ids["request_accept"])
        response_consume = _bit_observation(ids["response_consume"])
        request_occupancy = _observation(
            ids["request_occupancy"], UIntType(request_width)
        )
        response_occupancy = _observation(
            ids["response_occupancy"], UIntType(response_width)
        )
        generated = f"request_response:{connection.semantic_id}"
        bounds = _ordered(
            FormalBinaryOperator.LESS_EQUAL, outstanding,
            _constant(connection.max_outstanding, tracker_width, FormalSignedness.UNSIGNED),
        )
        no_response = _or(
            _not(response_consume),
            _or(
                request_accept,
                _ordered(
                    FormalBinaryOperator.GREATER, outstanding,
                    _constant(0, tracker_width, FormalSignedness.UNSIGNED),
                ),
            ),
        )
        work_width = tracker_width + 2
        next_outstanding = _binary(
            FormalBinaryOperator.SUBTRACT,
            _binary(
                FormalBinaryOperator.ADD,
                _resize(_previous(outstanding), work_width, FormalSignedness.UNSIGNED),
                _resize(_previous(request_accept), work_width, FormalSignedness.UNSIGNED),
                work_width, FormalSignedness.UNSIGNED,
            ),
            _resize(_previous(response_consume), work_width, FormalSignedness.UNSIGNED),
            work_width, FormalSignedness.UNSIGNED,
        )
        conservation = _without_previous_reset(
            module,
            _equal(
                _resize(outstanding, work_width, FormalSignedness.UNSIGNED),
                next_outstanding,
            ),
        )
        response_count = _ordered(
            FormalBinaryOperator.LESS_EQUAL,
            _resize(response_occupancy, work_width, FormalSignedness.UNSIGNED),
            _resize(outstanding, work_width, FormalSignedness.UNSIGNED),
        )
        properties.extend((
            _property(
                "request_response", f"{connection.semantic_id}.bounds",
                f"{ids['outstanding']} >= 0 && {ids['outstanding']} <= "
                f"{connection.max_outstanding}", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=bounds, origin=connection.source_origin,
            ),
            _property(
                "request_response", f"{connection.semantic_id}.no_response_without_request",
                f"{ids['response_consume']} == 0 || {ids['request_accept']} == 1 || "
                f"{ids['outstanding']} > 0", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=no_response, origin=connection.source_origin,
            ),
            _property(
                "request_response", f"{connection.semantic_id}.response_count",
                f"{ids['response_occupancy']} <= {ids['outstanding']}", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=response_count, origin=connection.source_origin,
            ),
            _property(
                "request_response", f"{connection.semantic_id}.conservation",
                f"{ids['outstanding']} == previous({ids['outstanding']} + "
                f"{ids['request_accept']} - {ids['response_consume']})", module,
                ownership=Ownership.IMPLEMENTATION, generated_from=generated,
                predicate=conservation, temporal=TemporalForm.NEXT_CYCLE,
                origin=connection.source_origin,
            ),
        ))
        if module.reset is not None:
            reset_clear = _and(
                _equal(outstanding, _constant(0, tracker_width, FormalSignedness.UNSIGNED)),
                _and(
                    _equal(request_occupancy, _constant(0, request_width, FormalSignedness.UNSIGNED)),
                    _equal(response_occupancy, _constant(0, response_width, FormalSignedness.UNSIGNED)),
                ),
            )
            properties.append(_property(
                "request_response", f"{connection.semantic_id}.reset_epoch",
                f"previous({module.reset}) -> {ids['outstanding']} == 0 && "
                f"{ids['request_occupancy']} == 0 && {ids['response_occupancy']} == 0",
                module, ownership=Ownership.IMPLEMENTATION,
                generated_from=generated, predicate=_implies(
                    _bit_observation("reset", ObservationCycle.PREVIOUS), reset_clear
                ),
                temporal=TemporalForm.NEXT_CYCLE, reset_condition=None,
                origin=connection.source_origin,
            ))
    for block_ordinal, block in enumerate(module.csr_blocks):
        effective_bindings = derived_state_bindings(
            block, module_identity=module.source_hash or module.name,
            block_ordinal=block_ordinal, clock_domain=module.clock,
            reset_domain=module.reset,
        )
        state_by_field = {
            item.csr_field_id: item for item in effective_bindings
        }
        for register_ordinal, register in enumerate(block.registers):
            for field_ordinal, field in enumerate(register.fields):
                prefix = f"{block.name}.{register.name}.{field.name}"
                state_binding = state_by_field.get(field.identity)
                if state_binding is None and field.identity is None:
                    state_binding = next(
                        (item for item in effective_bindings
                         if item.csr_field_id.register.declaration_ordinal == register_ordinal
                         and item.csr_field_id.declaration_ordinal == field_ordinal),
                        None,
                    )
                if state_binding is None:
                    continue
                state = state_binding.semantic_state_id
                write_hit = state_binding.write_hit_id
                write_value = state_binding.write_value_id
                state_ref = _observation(state, state_binding.canonical_type)
                hit_ref = _bit_observation(write_hit)
                value_ref = _observation(write_value, state_binding.canonical_type)
                width = state_binding.field_width
                signedness = _formal_signedness(state_binding.canonical_type)
                if module.reset is not None:
                    properties.append(_property(
                        "csr", prefix + ".reset",
                        f"previous({module.reset}) -> {prefix} == {field.reset}",
                        module, ownership=Ownership.IMPLEMENTATION,
                        generated_from=f"csr-field:{state_binding.csr_field_id.render()}:reset",
                        predicate=_implies(
                            _bit_observation("reset", ObservationCycle.PREVIOUS),
                            _equal(state_ref, _constant(field.reset, width, signedness)),
                        ),
                        temporal=TemporalForm.NEXT_CYCLE, reset_condition=None,
                        origin=field.source_origin,
                    ))
                previous_state = _previous(state_ref)
                previous_hit = _previous(hit_ref)
                previous_value = _previous(value_ref)
                if field.access.value == "rw":
                    update = PredicateMux(
                        previous_hit, previous_value, previous_state,
                        width, signedness,
                    )
                    suffix = "rw"
                if field.access.value == "w1c":
                    cleared = _binary(
                        FormalBinaryOperator.BIT_AND,
                        previous_state,
                        PredicateUnary(
                            FormalUnaryOperator.BITWISE_NOT,
                            previous_value,
                            width,
                            signedness,
                        ),
                        width,
                        signedness,
                    )
                    update = PredicateMux(
                        previous_hit, cleared, previous_state,
                        width, signedness,
                    )
                    suffix = "w1c"
                if field.access.value == "pulse":
                    update = PredicateMux(
                        previous_hit,
                        previous_value,
                        _constant(0, width, signedness),
                        width,
                        signedness,
                    )
                    suffix = "pulse"
                if field.access.value in {"rw", "w1c", "pulse"}:
                    predicate = _without_previous_reset(
                        module, _equal(state_ref, update)
                    )
                    properties.append(_property(
                        "csr", prefix + f".{suffix}",
                        f"{prefix} == previous({suffix} update)", module,
                        ownership=Ownership.IMPLEMENTATION,
                        generated_from=f"csr-field:{state_binding.csr_field_id.render()}:{suffix}",
                        predicate=predicate, temporal=TemporalForm.NEXT_CYCLE,
                        origin=field.source_origin,
                        non_executable_reason=(
                            "hardware-connected CSR priority requires its existing "
                            "implementation observation"
                            if field.binding is not None else None
                        ),
                    ))
    if module.resolved_transition is not None:
        from zlang.ir.state import actions_conflict, groups_conflict
        groups = module.resolved_transition.action_groups
        by_name = {group.rule_name: group for group in groups}

        def active_conflict_predicate(left, right):
            """Return the exact same-cycle conflict selected by activations.

            ``groups_conflict`` without runtime activation values is the
            conservative semantic legality relation.  It is insufficient as
            an executable property once nested ``when`` makes an individual
            effect conditional: two outer groups may legally co-fire when the
            potentially conflicting effects are inactive.  Build the claim
            from the already-typed per-effect activation predicates instead;
            no new observation family is required.
            """

            pairs = tuple(
                (left_action, right_action)
                for left_action in left.actions
                for right_action in right.actions
                if actions_conflict(left_action, right_action)
            )
            if not pairs:
                return None, False, None
            # One unconditional conflicting pair makes the group conflict
            # unconditional and preserves the historical property spelling.
            if any(
                left_action.activation is None
                and right_action.activation is None
                for left_action, right_action in pairs
            ):
                return _bit(True), False, None
            terms: list[FormalPredicate] = []
            try:
                for left_action, right_action in pairs:
                    predicates = tuple(
                        _semantic_predicate(action.activation)
                        for action in (left_action, right_action)
                        if action.activation is not None
                    )
                    term = predicates[0]
                    for predicate in predicates[1:]:
                        term = _and(term, predicate)
                    terms.append(term)
            except FormalError as error:
                return None, True, str(error)
            result = terms[0]
            for term in terms[1:]:
                result = _or(result, term)
            return result, True, None

        conflicts = {
            tuple(sorted((left.rule_name, right.rule_name))):
                active_conflict_predicate(left, right)
            for index, left in enumerate(groups)
            for right in groups[index + 1:]
            if groups_conflict(left, right)
        }
        for (higher, lower), (
            active_conflict, conditional, lowering_error,
        ) in sorted(conflicts.items()):
            left = _bit_observation(rule_fire_observation_id(higher))
            right = _bit_observation(rule_fire_observation_id(lower))
            simultaneous = _and(left, right)
            predicate = (
                _not(_and(simultaneous, active_conflict))
                if conditional and active_conflict is not None
                else _not(simultaneous)
            )
            properties.append(_property(
                "rules", f"{higher}.exclusive.{lower}",
                (
                    f"!({higher}_fire && {lower}_fire && active_conflict)"
                    if conditional else
                    f"!({higher}_fire && {lower}_fire)"
                ), module,
                ownership=Ownership.SCHEDULER,
                generated_from=f"rules:{higher},{lower}",
                predicate=predicate,
                origin=by_name[higher].source_origin,
                non_executable_reason=(
                    "conditional rule-conflict activation is outside the "
                    f"structured formal predicate subset: {lowering_error}"
                    if lowering_error is not None else None
                ),
            ))
        for priority in module.resolved_transition.priorities:
            if tuple(sorted(priority)) not in conflicts:
                continue
            higher, lower = priority
            active_conflict, conditional, lowering_error = conflicts[
                tuple(sorted(priority))
            ]
            higher_fire = _bit_observation(rule_fire_observation_id(higher))
            antecedent = (
                _and(higher_fire, active_conflict)
                if conditional and active_conflict is not None
                else higher_fire
            )
            properties.append(_property(
                "rules", f"{higher}.priority.{lower}",
                (
                    f"({higher}_fire && active_conflict) -> !{lower}_fire"
                    if conditional else
                    f"{higher}_fire -> !{lower}_fire"
                ), module,
                ownership=Ownership.SCHEDULER,
                generated_from=f"priority:{higher}>{lower}",
                predicate=_implies(
                    antecedent,
                    _not(_bit_observation(rule_fire_observation_id(lower))),
                ),
                origin=by_name[higher].source_origin,
                non_executable_reason=(
                    "conditional rule-conflict activation is outside the "
                    f"structured formal predicate subset: {lowering_error}"
                    if lowering_error is not None else None
                ),
            ))
    if module.verification_scopes:
        legacy_contract_kinds = {
            item.name: item.kind.value for item in module.contracts
        }
        for scope in module.verification_scopes:
            requirement_predicates = tuple(
                _semantic_predicate(item.expression)
                for item in scope.requirements
            )
            conjunction: FormalPredicate | None = None
            for requirement_predicate in requirement_predicates:
                conjunction = (
                    requirement_predicate
                    if conjunction is None
                    else _and(conjunction, requirement_predicate)
                )
            if conjunction is not None:
                covers.append(CoverProperty(
                    id=f"{scope.semantic_id}.requirements_feasible",
                    clock=scope.clock,
                    reset_condition=scope.reset,
                    expression=conjunction.render(),
                    predicate=conjunction,
                    source_origin=scope.source_origin,
                    generated_from=f"verification-feasibility:{scope.name}",
                ))
            if scope.name == "$module":
                for requirement, predicate in zip(
                    scope.requirements, requirement_predicates, strict=True
                ):
                    properties.append(FormalProperty(
                        id=requirement.semantic_id,
                        kind=PropertyKind.ASSUMPTION,
                        clock=scope.clock,
                        reset_condition=scope.reset,
                        expression=predicate.render(),
                        temporal_form=TemporalForm.SAME_CYCLE,
                        ownership=Ownership.ENVIRONMENT,
                        source_origin=requirement.source_origin,
                        generated_from=(
                            f"contract:{requirement.name}"
                            if legacy_contract_kinds.get(requirement.name) == "assume"
                            else f"verification-requirement:{scope.name}:{requirement.name}"
                        ),
                        relevant_signals=predicate.observation_ids(),
                        predicate=predicate,
                    ))
            for goal in scope.goals:
                predicate = _semantic_predicate(goal.expression)
                if scope.name != "$module" and conjunction is not None:
                    executable = (
                        _and(conjunction, predicate)
                        if goal.kind is VerificationGoalKind.COVER
                        else _implies(conjunction, predicate)
                    )
                else:
                    executable = predicate
                if goal.kind is VerificationGoalKind.COVER:
                    covers.append(CoverProperty(
                        id=goal.semantic_id,
                        clock=scope.clock,
                        reset_condition=scope.reset,
                        expression=executable.render(),
                        predicate=executable,
                        source_origin=goal.source_origin,
                        generated_from=f"verification-cover:{scope.name}:{goal.name}",
                    ))
                else:
                    properties.append(FormalProperty(
                        id=goal.semantic_id,
                        kind=PropertyKind.ASSERTION,
                        clock=scope.clock,
                        reset_condition=scope.reset,
                        expression=executable.render(),
                        temporal_form=TemporalForm.SAME_CYCLE,
                        ownership=Ownership.IMPLEMENTATION,
                        source_origin=goal.source_origin,
                        generated_from=(
                            f"contract:{goal.name}"
                            if legacy_contract_kinds.get(goal.name) == "guarantee"
                            else (
                                f"verification-{goal.kind.value}:"
                                f"{scope.name}:{goal.name}"
                            )
                        ),
                        relevant_signals=executable.observation_ids(),
                        predicate=executable,
                    ))
    else:
        # Compatibility for hand-constructed semantic modules predating the
        # overlay normalization performed by semantic analysis.
        for contract in module.contracts:
            predicate = _semantic_predicate(contract.expression)
            properties.append(FormalProperty(
                id=_stable_id("contract", module.name, contract.name),
                kind=(PropertyKind.ASSUMPTION if contract.kind.value == "assume" else PropertyKind.ASSERTION),
                clock=contract.clock, reset_condition=contract.reset,
                expression=predicate.render(),
                temporal_form=TemporalForm.SAME_CYCLE, ownership=(Ownership.ENVIRONMENT if contract.kind.value == "assume" else Ownership.IMPLEMENTATION),
                source_origin=contract.expression.origin,
                generated_from=f"contract:{contract.name}",
                relevant_signals=predicate.observation_ids(), predicate=predicate,
            ))
    return FormalDesign(
        module.name,
        tuple(properties),
        signal_bindings(binding_module, include_rule_fire=True),
        covers=tuple(covers),
        clock_domains=tuple(binding_module.clock_domains),
    )


def top_aggregate_ownership(module: Module) -> dict[str, Ownership]:
    """Return M35 ownership for each projected top aggregate leaf."""
    from zlang.ir.top_abi import build_top_aggregate_abi
    return {
        leaf.leaf_semantic_id: (
            Ownership.ENVIRONMENT
            if leaf.direction is PortDirection.INPUT
            else Ownership.IMPLEMENTATION
        )
        for leaf in build_top_aggregate_abi(module).leaves
    }


def signal_bindings(module: Module, *, rtl_module: str | None = None,
                    overrides: dict[str, str] | None = None,
                    include_rule_fire: bool = False) -> tuple[SignalBinding, ...]:
    """Publish explicit bindings for public ports and supported semantic state."""
    rtl_module = rtl_module or module.name
    overrides = overrides or {}
    result: list[SignalBinding] = []
    csr_internal = csr_internal_port_names(module.csr_access, module.csr_blocks)
    if module.clock is not None:
        result.append(SignalBinding(
            "clock", rtl_module, overrides.get("clock", module.clock), 1,
            "input", module.clock,
        ))
    if module.reset is not None:
        result.append(SignalBinding(
            "reset", rtl_module, overrides.get("reset", module.reset), 1,
            "input", module.clock,
        ))
    for port in module.ports:
        if port.name in csr_internal:
            continue
        port_id = port_observation_id(port.name)
        result.append(SignalBinding(port_id, rtl_module, overrides.get(port_id, port.name),
                                    port.type.width, port.direction.value, port.domain or module.clock))
    for register in module.registers:
        register_id = register_observation_id(register.name)
        result.append(SignalBinding(register_id, rtl_module,
                                    overrides.get(register_id, register.name), register.type.width,
                                    "internal", register.domain or module.clock, register.initial.origin))
    if include_rule_fire and module.resolved_transition is not None:
        for group in module.resolved_transition.action_groups:
            key = rule_fire_observation_id(group.rule_name)
            result.append(SignalBinding(
                key, rtl_module,
                overrides.get(key, f"rule_{group.rule_name}_fire"),
                1, "internal", module.clock, group.source_origin,
            ))
    for fifo in module.fifos:
        for signal, width in (("count", fifo.count_width), ("push", 1), ("pop", 1), ("empty", 1), ("full", 1), ("front", fifo.element_type.width)):
            key = fifo_observation_id(fifo.name, signal)
            result.append(SignalBinding(
                key, rtl_module, overrides.get(key, f"{fifo.name}_{signal}"),
                width, "internal", module.clock,
                fifo.data.origin if fifo.data is not None else fifo.source_origin,
            ))
    for connection in module.request_response_connections:
        tracker = (f"rr_{connection.request.source.owner}_{connection.request.source.name}_"
                   f"{connection.response.destination.owner}_outstanding")
        tracker_width = max(1, connection.max_outstanding.bit_length())
        request_width = (
            max(1, connection.request.request_buffer_depth.bit_length())
            if connection.request.request_buffer_depth else tracker_width
        )
        response_width = (
            max(1, connection.response.response_buffer_depth.bit_length())
            if connection.response.response_buffer_depth else tracker_width
        )
        for signal, width, rtl_name in (
            ("outstanding", tracker_width, tracker),
            ("request_accept", 1, tracker + "_request_accept"),
            ("response_consume", 1, tracker + "_response_consume"),
            ("request_occupancy", request_width, tracker + "_request_occupancy"),
            ("response_occupancy", response_width, tracker + "_response_occupancy"),
        ):
            key = request_response_observation_id(connection.semantic_id, signal)
            result.append(SignalBinding(key, rtl_module, overrides.get(key, rtl_name), width,
                                        "internal", module.clock, connection.source_origin))
    for block_ordinal, block in enumerate(module.csr_blocks):
        fields = {}
        block_id = block.identity
        for register_ordinal, register in enumerate(block.registers):
            register_id = register.identity
            for field_ordinal, field in enumerate(register.fields):
                field_id = field.identity
                if field_id is None:
                    from zlang.ir.csr import (
                        CsrBlockIdentity, CsrFieldIdentity, CsrRegisterIdentity,
                    )
                    effective_block = block_id or CsrBlockIdentity(
                        module.source_hash or module.name, block_ordinal
                    )
                    effective_register = register_id or CsrRegisterIdentity(
                        effective_block, register_ordinal
                    )
                    field_id = CsrFieldIdentity(effective_register, field_ordinal)
                fields[field_id] = (register, field)
        for state in derived_state_bindings(
            block, module_identity=module.source_hash or module.name,
            block_ordinal=block_ordinal, clock_domain=module.clock,
            reset_domain=module.reset,
        ):
            register, field = fields[state.csr_field_id]
            physical = f"csr_{block.name}_{register.name.lower()}_{field.name}"
            hit = f"csr_{block.name}_{register.name.lower()}_write_hit"
            value = physical + "_write_value"
            for key, width, rtl_name in (
                (state.semantic_state_id, state.field_width, physical),
                (state.write_hit_id, 1, hit),
                (state.write_value_id, state.field_width, value),
            ):
                result.append(SignalBinding(
                    key, rtl_module, overrides.get(key, rtl_name), width,
                    "internal", state.clock_domain, state.source_origin,
                ))
    for port in module.ports:
        if port.name in csr_internal:
            continue
        if port.protocol is InterfaceProtocol.READY_VALID:
            forward = port.direction.value
            reverse = (
                PortDirection.OUTPUT.value
                if port.direction is PortDirection.INPUT
                else PortDirection.INPUT.value
            )
            for signal, width, direction in (
                ("valid", 1, forward),
                ("ready", 1, reverse),
                ("payload", port.type.width, forward),
            ):
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
        elif port.protocol is InterfaceProtocol.CREDIT:
            sender = port.direction is PortDirection.OUTPUT
            credit_fields = [
                ("payload", port.type.width, port.direction.value),
                ("send", 1, port.direction.value),
                (
                    "return", 1,
                    PortDirection.INPUT.value if sender
                    else PortDirection.OUTPUT.value,
                ),
            ]
            if sender:
                credit_fields.append((
                    "credits", max(1, (port.capacity or 1).bit_length()),
                    "internal",
                ))
            else:
                credit_fields.append((
                    "occupancy", max(1, (port.capacity or 1).bit_length()),
                    "internal",
                ))
            for signal, width, direction in credit_fields:
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
        elif port.protocol is InterfaceProtocol.PACKET:
            forward = port.direction.value
            reverse = (
                PortDirection.OUTPUT.value
                if port.direction is PortDirection.INPUT
                else PortDirection.INPUT.value
            )
            for signal, width, direction in (
                ("payload", port.type.width, forward),
                ("valid", 1, forward),
                ("last", 1, forward),
                ("ready", 1, reverse),
            ):
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
        elif port.protocol is InterfaceProtocol.VC_CREDIT:
            forward = port.direction.value
            reverse = (
                PortDirection.OUTPUT.value
                if port.direction is PortDirection.INPUT
                else PortDirection.INPUT.value
            )
            vc_width = max(1, ((port.virtual_channels or 1) - 1).bit_length())
            for signal, width, direction in (
                ("payload", port.type.width, forward),
                ("vc", vc_width, forward),
                ("send", 1, forward),
                ("return", 1, reverse),
                ("return_vc", vc_width, reverse),
            ):
                key = port_observation_id(port.name, signal)
                result.append(SignalBinding(
                    key, rtl_module,
                    overrides.get(key, f"{port.name}_{signal}"),
                    width, direction, port.domain or module.clock,
                ))
    for interface in module.request_responses:
        requester = interface.role.value == "requester"
        for channel, signal, width, implementation_owned in (
            ("request", "payload", interface.request_type.width, requester),
            ("request", "valid", 1, requester),
            ("request", "ready", 1, not requester),
            ("response", "payload", interface.response_type.width, not requester),
            ("response", "valid", 1, not requester),
            ("response", "ready", 1, requester),
        ):
            key = port_observation_id(interface.name, f"{channel}.{signal}")
            direction = (
                PortDirection.OUTPUT.value
                if implementation_owned else PortDirection.INPUT.value
            )
            result.append(SignalBinding(
                key, rtl_module,
                overrides.get(key, f"{interface.name}_{channel}_{signal}"),
                width, direction, module.clock,
            ))
    return tuple(result)


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def _formal_domain_support_reason(
    domain: ClockDomain,
    domains: tuple[ClockDomain, ...],
) -> str | None:
    """Return the bounded executable-domain exclusion, if any.

    Existing per-goal multi-domain synchronous planning remains valid.  The
    async extension is deliberately one-domain: an unrelated async domain must
    not poison a synchronous goal, but a goal sampled by that async domain is
    not executable until cross-domain reset epochs have a frozen model.
    """

    return formal_domain_applicability_reason(
        domain,
        domain_count=len(domains),
    )


def _formal_item_domain(item: FormalProperty | CoverProperty, module: Module):
    """Resolve one goal to exactly one semantic clock/reset domain.

    Formal applicability belongs to the sampled goal, not to every physical
    domain declared by the surrounding module.  In particular, an unrelated
    asynchronous domain must not disable a goal sampled by a legacy domain.
    """

    domains = module.clock_domains
    if (
        not domains
        and module.clock is not None
        and module.reset is not None
    ):
        # Retain compatibility with hand-constructed pre-domain typed modules.
        # The historical clock/reset pair denotes exactly the legacy contract.
        domains = (ClockDomain(module.clock, module.reset),)
    matches = tuple(
        domain for domain in domains
        if domain.clock == item.clock
        and (
            item.reset_condition is None
            or domain.reset == item.reset_condition
        )
    )
    if len(matches) == 1:
        return matches[0], None
    reset = (
        "<unspecified>"
        if item.reset_condition is None else item.reset_condition
    )
    if not matches:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "does not resolve to a module clock/reset domain"
        )
    return None, (
        f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
        "resolves ambiguously to multiple module domains"
    )


def mark_formal_domain_applicability(
    design: FormalDesign,
    module: Module,
) -> FormalDesign:
    """Mark exact M35 clock/reset support independently for every source goal.

    The checker supports every validated single-domain edge/polarity/assertion
    contract carried by :class:`ClockDomain`, including the frozen two-cycle
    synchronized release.  Power-up initialization remains deliberately
    outside executable formal semantics.
    """

    domains = tuple(module.clock_domains)
    if (
        not domains
        and module.clock is not None
        and module.reset is not None
    ):
        domains = (ClockDomain(module.clock, module.reset),)
    domain_blocked: set[str] = set()

    def mark(item: FormalProperty | CoverProperty):
        if item.non_executable_reason is not None:
            return item
        domain, reason = _formal_item_domain(item, module)
        if reason is None and domain is not None:
            reason = _formal_domain_support_reason(domain, domains)
        if reason is None:
            return item
        domain_blocked.add(item.id)
        return replace(item, non_executable_reason=reason)

    properties = tuple(mark(item) for item in design.properties)
    covers = tuple(mark(item) for item in design.covers)
    original_executable = tuple(
        item for item in (*design.properties, *design.covers)
        if item.non_executable_reason is None
    )
    all_domain_blocked = bool(original_executable) and all(
        item.id in domain_blocked for item in original_executable
    )
    top_reason = design.non_executable_reason
    if all_domain_blocked:
        reasons = {
            item.non_executable_reason
            for item in (*properties, *covers)
            if item.id in domain_blocked
        }
        top_reason = (
            POWER_UP_FORMAL_DOMAIN_REASON
            if reasons == {POWER_UP_FORMAL_DOMAIN_REASON}
            else "all formal goals have unsupported or unresolved clock/reset domains"
        )
    return replace(
        design,
        properties=properties,
        covers=covers,
        non_executable_reason=top_reason,
    )


def _clock_domain_from_physical(item: object) -> ClockDomain:
    """Restore and validate one v10 physical-domain manifest record."""

    try:
        return ClockDomain(
            str(getattr(item, "clock")),
            str(getattr(item, "reset")),
            ClockEdge(str(getattr(item, "clock_edge"))),
            ResetMode(str(getattr(item, "reset_mode"))),
            ResetPolarity(str(getattr(item, "reset_polarity"))),
            PowerUpPolicy(str(getattr(item, "power_up"))),
            source_origin=getattr(item, "source_origin", None),
            reset_release_mode=ResetReleaseMode(
                str(getattr(item, "reset_release_mode"))
            ),
            reset_release_cycles=getattr(item, "reset_release_cycles"),
        )
    except (TypeError, ValueError) as error:
        raise FormalError(
            f"backend artifact publishes an invalid physical reset contract: {error}"
        ) from error


def _formal_item_domain_from_design(
    item: FormalProperty | CoverProperty,
    design: FormalDesign,
) -> tuple[ClockDomain | None, str | None]:
    domains = design.clock_domains
    if not domains:
        # Compatibility with hand-constructed FormalDesign values predating
        # exact domain retention.  Their historical meaning is the legacy
        # contract named directly by the property.
        reset = item.reset_condition or "reset"
        return ClockDomain(item.clock, reset), None
    matches = tuple(
        domain for domain in domains
        if domain.clock == item.clock
        and (
            item.reset_condition is None
            or domain.reset == item.reset_condition
        )
    )
    reset = item.reset_condition or "<unspecified>"
    if len(matches) == 1:
        reason = _formal_domain_support_reason(matches[0], domains)
        return (None, reason) if reason is not None else (matches[0], None)
    if not matches:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "does not resolve to its retained source domain"
        )
    return None, (
        f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
        "resolves ambiguously to retained source domains"
    )


def _physical_domain_for_formal_item(
    item: FormalProperty | CoverProperty,
    physical_domains: tuple[object, ...],
    source_domain: ClockDomain,
) -> tuple[object | None, str | None]:
    """Resolve one source goal against version-10 physical-domain records.

    Empty physical-domain metadata is the intentionally retained legacy
    manifest spelling and therefore does not by itself make a goal
    unavailable.  When records are present they contain every physical domain,
    so an exact match is mandatory.
    """

    if not physical_domains:
        if source_domain.is_legacy_default:
            return None, None
        return None, (
            f"formal goal '{item.id}' requires an exact non-default physical "
            "domain manifest"
        )
    matches = tuple(
        domain for domain in physical_domains
        if item.clock in {
            getattr(domain, "clock", None),
            getattr(domain, "rtl_clock_path", None),
        }
        and (
            item.reset_condition is None
            or item.reset_condition in {
                getattr(domain, "reset", None),
                getattr(domain, "rtl_reset_path", None),
            }
        )
    )
    reset = (
        "<unspecified>"
        if item.reset_condition is None else item.reset_condition
    )
    if not matches:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "does not resolve to a published physical domain"
        )
    if len(matches) != 1:
        return None, (
            f"formal goal '{item.id}' clock/reset '{item.clock}/{reset}' "
            "resolves ambiguously to published physical domains"
        )
    physical_domain = _clock_domain_from_physical(matches[0])
    source_contract = replace(source_domain, source_origin=None)
    physical_contract = replace(physical_domain, source_origin=None)
    if physical_contract != source_contract:
        return matches[0], (
            f"formal goal '{item.id}' source and backend physical reset "
            "contracts do not match"
        )
    if physical_domain.power_up is not PowerUpPolicy.UNSPECIFIED:
        return matches[0], POWER_UP_FORMAL_DOMAIN_REASON
    return matches[0], None


def _binding_role(item: object) -> str:
    role = getattr(item, "role", "")
    return str(getattr(role, "value", role))


def _public_domain_bindings(
    item: FormalProperty | CoverProperty,
    public: tuple[object, ...],
) -> tuple[object | None, object | None, str | None]:
    clocks = tuple(
        binding for binding in public
        if _binding_role(binding) == "clock"
        and getattr(binding, "physical_available", False)
        and bool(getattr(binding, "rtl_path", None))
        and item.clock in {
            getattr(binding, "clock_domain", None),
            getattr(binding, "rtl_path", None),
            getattr(binding, "semantic_signal_id", None),
        }
        and (
            item.reset_condition is None
            or getattr(binding, "reset_domain", None) == item.reset_condition
        )
    )
    reset = (
        "<unspecified>"
        if item.reset_condition is None else item.reset_condition
    )
    if len(clocks) != 1:
        qualifier = "no" if not clocks else "multiple"
        return None, None, (
            f"backend artifact has {qualifier} physical clock binding for "
            f"goal domain '{item.clock}/{reset}'"
        )
    expected_reset = (
        item.reset_condition
        if item.reset_condition is not None
        else getattr(clocks[0], "reset_domain", None)
    )
    if expected_reset is None:
        return clocks[0], None, None
    resets = tuple(
        binding for binding in public
        if _binding_role(binding) == "reset"
        and getattr(binding, "physical_available", False)
        and bool(getattr(binding, "rtl_path", None))
        and getattr(binding, "clock_domain", None)
        == getattr(clocks[0], "clock_domain", None)
        and expected_reset in {
            getattr(binding, "reset_domain", None),
            getattr(binding, "rtl_path", None),
            getattr(binding, "semantic_signal_id", None),
        }
    )
    if len(resets) != 1:
        qualifier = "no" if not resets else "multiple"
        return None, None, (
            f"backend artifact has {qualifier} physical reset binding for "
            f"goal domain '{item.clock}/{expected_reset}'"
        )
    return clocks[0], resets[0], None


def connect_formal_design(design: FormalDesign, artifact: object) -> FormalDesign:
    """Bind one semantic design to backend-published formal observation ports.

    The adapter consumes the exact tokens present in ``BackendArtifact`` v4;
    it never derives a token from an RTL or source name.  The returned design
    is the only form accepted by executable harness/SBY emission.
    """

    # This low-level connector is exported as part of ``zlang.ir`` as well as
    # through the compiler-owned wrapper.  Keep the physical-reset gate here so
    # callers cannot bypass the frozen M35 clock/reset model by constructing
    # properties directly and then attaching a current BackendArtifact.  The
    # gate is evaluated for each exact goal domain below; unrelated domains do
    # not poison otherwise executable goals.
    if design.non_executable_reason is not None:
        return design
    physical_domains = tuple(getattr(artifact, "physical_domains", ()))

    artifact_hash = getattr(artifact, "artifact_hash", None)
    formal_artifact_hash = getattr(artifact, "formal_artifact_hash", None)
    backend = getattr(artifact, "backend", None)
    implementation_text = getattr(artifact, "text", None)
    if not all(isinstance(item, str) and item for item in (
        artifact_hash, backend, implementation_text,
    )):
        raise FormalError("connected formal execution requires a complete BackendArtifact")
    try:
        getattr(artifact, "binding_map")().validate()
    except (AttributeError, ValueError) as error:
        raise FormalError(f"invalid backend binding map: {error}") from error

    recursive = tuple(getattr(artifact, "recursive_bindings", ()))
    observations = {
        item.semantic_binding_id: item
        for item in getattr(artifact, "formal_observations", ())
    }
    if len(observations) != len(tuple(getattr(artifact, "formal_observations", ()))):
        raise FormalError("backend artifact contains duplicate formal observation IDs")
    root_depth = min(
        (len(item.physical_instance_path) for item in recursive), default=None
    )
    root_recursive: dict[str, object] = {}
    if root_depth is not None:
        for item in recursive:
            if len(item.physical_instance_path) != root_depth:
                continue
            if item.local_semantic_id in root_recursive:
                raise FormalError(
                    f"backend artifact has duplicate root observation "
                    f"'{item.local_semantic_id}'"
                )
            root_recursive[item.local_semantic_id] = item

    public = tuple(getattr(artifact, "bindings", ()))
    public_by_id = {item.semantic_signal_id: item for item in public}
    if len(public_by_id) != len(public):
        raise FormalError("backend artifact contains duplicate public signal bindings")

    accepted_hashes = {artifact_hash}
    if formal_artifact_hash:
        accepted_hashes.add(formal_artifact_hash)

    def resolve_observation(
        semantic_id: str,
        expected_width: int,
        *,
        goal_clock: object,
        goal_reset: object | None,
    ) -> SignalBinding:
        if semantic_id in {"clock", "reset"}:
            item = goal_clock if semantic_id == "clock" else goal_reset
            if item is None or not item.physical_available or not item.rtl_path:
                raise FormalError(
                    f"backend artifact has no physical goal-domain binding "
                    f"for '{semantic_id}'"
                )
            if item.width != expected_width:
                raise FormalError(
                    f"backend binding width mismatch for '{semantic_id}': "
                    f"expected {expected_width}, got {item.width}"
                )
            return SignalBinding(
                semantic_id, item.rtl_module, item.rtl_path, item.width,
                "input", item.clock_domain, item.source_origin,
            )
        recursive_item = root_recursive.get(semantic_id)
        if recursive_item is None:
            raise FormalError(
                f"backend artifact has no root formal binding for '{semantic_id}'"
            )
        observation = observations.get(recursive_item.semantic_binding_id)
        token = getattr(observation, "observation_token", None)
        module_name = getattr(recursive_item, "rtl_module", None)
        if (
            observation is None
            or not observation.physical_available
            or not isinstance(token, str)
            or not token
            or not recursive_item.physical_available
            or not isinstance(module_name, str)
            or not module_name
        ):
            raise FormalError(
                f"formal observation unavailable for '{semantic_id}'"
            )
        if observation.width != expected_width or recursive_item.width != expected_width:
            raise FormalError(
                f"formal observation width mismatch for '{semantic_id}': "
                f"expected {expected_width}"
            )
        if observation.artifact_hash not in accepted_hashes:
            raise FormalError(
                f"formal observation artifact hash mismatch for '{semantic_id}'"
            )
        return SignalBinding(
            semantic_id, module_name, token, expected_width, "output",
            observation.clock_domain, recursive_item.source_origin,
        )

    # Bind each property independently.  A partially observable backend may
    # execute the exact subset it publishes, but every omitted property keeps
    # an explicit reason and is never weakened or wired to a guessed name.
    connected_by_id: dict[str, SignalBinding] = {}
    connected_modules: set[str] = set()
    connected_properties: list[FormalProperty] = []
    domain_blocked: set[str] = set()

    def goal_domain_bindings(
        item: FormalProperty | CoverProperty,
    ) -> tuple[object | None, object | None, str | None]:
        source_domain, reason = _formal_item_domain_from_design(item, design)
        if reason is not None or source_domain is None:
            return None, None, reason
        physical, reason = _physical_domain_for_formal_item(
            item, physical_domains, source_domain
        )
        if reason is not None:
            return None, None, reason
        clock_binding, reset_binding, reason = _public_domain_bindings(
            item, public
        )
        if reason is not None:
            return None, None, reason
        if physical is not None:
            if (
                getattr(clock_binding, "rtl_module", None)
                != getattr(physical, "rtl_module", None)
                or getattr(clock_binding, "rtl_path", None)
                != getattr(physical, "rtl_clock_path", None)
                or (
                    reset_binding is not None
                    and (
                        getattr(reset_binding, "rtl_module", None)
                        != getattr(physical, "rtl_module", None)
                        or getattr(reset_binding, "rtl_path", None)
                        != getattr(physical, "rtl_reset_path", None)
                    )
                )
            ):
                return None, None, (
                    f"formal goal '{item.id}' public clock/reset bindings "
                    "disagree with its physical-domain manifest"
                )
        return clock_binding, reset_binding, None

    for item in design.properties:
        if item.non_executable_reason is not None:
            connected_properties.append(item)
            continue
        if item.predicate is None:
            raise FormalError(
                f"property '{item.id}' has only a legacy string predicate and "
                "cannot be executed"
            )
        goal_clock, goal_reset, domain_reason = goal_domain_bindings(item)
        if domain_reason is not None:
            domain_blocked.add(item.id)
            connected_properties.append(replace(
                item, non_executable_reason=domain_reason,
            ))
            continue

        widths: dict[str, int] = {"clock": 1}
        if goal_reset is not None:
            widths["reset"] = 1
        for observation in item.predicate.observations():
            previous = widths.setdefault(
                observation.semantic_signal_id, observation.width
            )
            if previous != observation.width:
                raise FormalError(
                    f"observation '{observation.semantic_signal_id}' has "
                    "inconsistent predicate widths"
                )
        try:
            resolved = tuple(
                resolve_observation(
                    semantic_id,
                    width,
                    goal_clock=goal_clock,
                    goal_reset=goal_reset,
                )
                for semantic_id, width in widths.items()
            )
        except FormalError as error:
            connected_properties.append(replace(
                item, non_executable_reason=str(error)
            ))
            continue
        for binding in resolved:
            previous = connected_by_id.get(binding.semantic_signal_id)
            if previous is not None and previous != binding:
                raise FormalError(
                    f"inconsistent connected binding for "
                    f"'{binding.semantic_signal_id}'"
                )
            connected_by_id[binding.semantic_signal_id] = binding
            if binding.semantic_signal_id not in {"clock", "reset"}:
                connected_modules.add(binding.rtl_module)
        connected_properties.append(item)
    connected_covers: list[CoverProperty] = []
    for item in design.covers:
        if item.non_executable_reason is not None:
            connected_covers.append(item)
            continue
        if item.predicate is None:
            raise FormalError(
                f"cover property '{item.id}' has no structured executable predicate"
            )
        goal_clock, goal_reset, domain_reason = goal_domain_bindings(item)
        if domain_reason is not None:
            domain_blocked.add(item.id)
            connected_covers.append(replace(
                item, non_executable_reason=domain_reason,
            ))
            continue

        widths: dict[str, int] = {"clock": 1}
        if goal_reset is not None:
            widths["reset"] = 1
        for observation in item.predicate.observations():
            previous = widths.setdefault(
                observation.semantic_signal_id, observation.width
            )
            if previous != observation.width:
                raise FormalError(
                    f"observation '{observation.semantic_signal_id}' has "
                    "inconsistent predicate widths"
                )
        try:
            resolved = tuple(
                resolve_observation(
                    semantic_id,
                    width,
                    goal_clock=goal_clock,
                    goal_reset=goal_reset,
                )
                for semantic_id, width in widths.items()
            )
        except FormalError as error:
            connected_covers.append(replace(
                item, non_executable_reason=str(error)
            ))
            continue
        for binding in resolved:
            previous = connected_by_id.get(binding.semantic_signal_id)
            if previous is not None and previous != binding:
                raise FormalError(
                    f"inconsistent connected binding for "
                    f"'{binding.semantic_signal_id}'"
                )
            connected_by_id[binding.semantic_signal_id] = binding
            if binding.semantic_signal_id not in {"clock", "reset"}:
                connected_modules.add(binding.rtl_module)
        connected_covers.append(item)

    original_executable = tuple(
        item for item in (*design.properties, *design.covers)
        if item.non_executable_reason is None
    )
    if original_executable and all(
        item.id in domain_blocked for item in original_executable
    ):
        blocked = {
            item.non_executable_reason
            for item in (*connected_properties, *connected_covers)
            if item.id in domain_blocked
        }
        reason = (
            POWER_UP_FORMAL_DOMAIN_REASON
            if blocked == {POWER_UP_FORMAL_DOMAIN_REASON}
            else "all formal goals have unsupported or unresolved physical domains"
        )
        return replace(
            design,
            properties=tuple(connected_properties),
            covers=tuple(connected_covers),
            bindings=(),
            connected_backend=None,
            connected_artifact_hash=None,
            connected_module=None,
            implementation_text=None,
            dut_ports=(),
            non_executable_reason=reason,
        )
    if len(connected_modules) > 1:
        raise FormalError("one M35 harness cannot bind multiple root RTL modules")

    dut_ports: list[SignalBinding] = []
    for item in public:
        role = getattr(item.role, "value", str(item.role))
        if role not in {"input", "output", "clock", "reset"}:
            continue
        if not item.physical_available or not item.rtl_path:
            continue
        direction = "input" if role in {"input", "clock", "reset"} else "output"
        dut_ports.append(SignalBinding(
            item.semantic_signal_id, item.rtl_module, item.rtl_path, item.width,
            direction, item.clock_domain, item.source_origin,
        ))
    # A valid artifact can deliberately publish no usable formal observation
    # (for example, after one required state projection is removed in a
    # fail-closed test).  It is still a connected implementation artifact; all
    # affected properties above carry explicit non-executable reasons.  Keep
    # the declared artifact module so report/SBY generation can distinguish
    # this case from an entirely unconnected semantic design.
    module_name = next(iter(connected_modules), None)
    if module_name is None:
        candidate_module = getattr(artifact, "module", None)
        if not isinstance(candidate_module, str) or not candidate_module:
            raise FormalError(
                "connected formal artifact publishes no implementation module"
            )
        module_name = candidate_module
    return replace(
        design,
        properties=tuple(connected_properties),
        covers=tuple(connected_covers),
        bindings=tuple(connected_by_id.values()),
        connected_backend=backend,
        connected_artifact_hash=formal_artifact_hash or artifact_hash,
        connected_module=module_name,
        implementation_text=implementation_text,
        dut_ports=tuple(dut_ports),
        non_executable_reason=None,
    )


def _predicate_report(design: FormalDesign, *, mode: ProofMode, depth: int) -> str:
    design_reason = (
        design.non_executable_reason
        or "backend formal artifact is not connected"
    )
    lines = [
        "// ZLang M35 non-executable property report",
        f"// module={design.module_name} mode={mode.value} depth={depth}",
        f"// reason={design_reason}",
    ]
    for item in design.properties:
        predicate = item.predicate.render() if item.predicate is not None else item.expression
        reason = item.non_executable_reason or design_reason
        lines.append(
            f"// {item.kind.value} {item.id}: {predicate} [not-run: {reason}]"
        )
    for item in design.covers:
        predicate = (
            item.predicate.render()
            if item.predicate is not None else item.expression
        )
        reason = item.non_executable_reason or design_reason
        lines.append(
            f"// cover {item.id}: {predicate} [not-run: {reason}]"
        )
    return "\n".join(lines) + "\n"


def _sv_cast(expression: str, width: int, signedness: FormalSignedness) -> str:
    if signedness is FormalSignedness.SIGNED:
        return f"$signed({width}'($signed({expression})))"
    return f"{width}'($unsigned({expression}))"


def _render_predicate(value: FormalPredicate, bindings: dict[str, SignalBinding]) -> str:
    if isinstance(value, ObservationRef):
        item = bindings.get(value.semantic_signal_id)
        if item is None:
            raise FormalError(
                f"structured predicate has no binding for '{value.semantic_signal_id}'"
            )
        token = item.rtl_name
        if not _IDENT.match(token):
            raise FormalError(
                f"formal observation token is not a legal identifier: {token!r}"
            )
        return (
            f"$past({token})"
            if value.cycle is ObservationCycle.PREVIOUS else token
        )
    if isinstance(value, PredicateConstant):
        if value.signedness is FormalSignedness.SIGNED:
            if value.value < 0:
                return f"-{value.width}'sd{abs(value.value)}"
            return f"{value.width}'sd{value.value}"
        return f"{value.width}'d{value.value}"
    if isinstance(value, PredicateUnary):
        operand = _render_predicate(value.operand, bindings)
        if value.operator is FormalUnaryOperator.LOGICAL_NOT:
            return f"!({operand})"
        if value.operator is FormalUnaryOperator.BITWISE_NOT:
            return f"~({operand})"
        return _sv_cast(operand, value.width, value.signedness)
    if isinstance(value, PredicateMux):
        condition = _render_predicate(value.condition, bindings)
        when_true = _sv_cast(
            _render_predicate(value.when_true, bindings),
            value.width,
            value.signedness,
        )
        when_false = _sv_cast(
            _render_predicate(value.when_false, bindings),
            value.width,
            value.signedness,
        )
        return f"(({condition}) ? ({when_true}) : ({when_false}))"
    if isinstance(value, PredicateBinary):
        left = _render_predicate(value.left, bindings)
        right = _render_predicate(value.right, bindings)
        symbol = {
            FormalBinaryOperator.LOGICAL_AND: "&&",
            FormalBinaryOperator.LOGICAL_OR: "||",
            FormalBinaryOperator.ADD: "+",
            FormalBinaryOperator.SUBTRACT: "-",
            FormalBinaryOperator.MULTIPLY: "*",
            FormalBinaryOperator.BIT_AND: "&",
            FormalBinaryOperator.BIT_OR: "|",
            FormalBinaryOperator.BIT_XOR: "^",
            FormalBinaryOperator.SHIFT_LEFT: "<<",
            FormalBinaryOperator.SHIFT_RIGHT: ">>",
            FormalBinaryOperator.EQUAL: "==",
            FormalBinaryOperator.NOT_EQUAL: "!=",
            FormalBinaryOperator.LESS: "<",
            FormalBinaryOperator.LESS_EQUAL: "<=",
            FormalBinaryOperator.GREATER: ">",
            FormalBinaryOperator.GREATER_EQUAL: ">=",
        }
        if value.operator is FormalBinaryOperator.IMPLIES:
            return f"(!({left}) || ({right}))"
        if value.operator in {
            FormalBinaryOperator.LOGICAL_AND,
            FormalBinaryOperator.LOGICAL_OR,
        }:
            return f"(({left}) {symbol[value.operator]} ({right}))"
        if value.operator in {
            FormalBinaryOperator.ADD,
            FormalBinaryOperator.SUBTRACT,
            FormalBinaryOperator.MULTIPLY,
            FormalBinaryOperator.BIT_AND,
            FormalBinaryOperator.BIT_OR,
            FormalBinaryOperator.BIT_XOR,
            FormalBinaryOperator.SHIFT_LEFT,
            FormalBinaryOperator.SHIFT_RIGHT,
        }:
            left = _sv_cast(left, value.width, value.signedness)
            if value.operator not in {
                FormalBinaryOperator.SHIFT_LEFT,
                FormalBinaryOperator.SHIFT_RIGHT,
            }:
                right = _sv_cast(right, value.width, value.signedness)
            if value.operator is FormalBinaryOperator.SHIFT_RIGHT:
                shifted = render_right_shift(
                    left,
                    right,
                    signed=value.signedness is FormalSignedness.SIGNED,
                )
                return _sv_cast(
                    shifted,
                    value.width,
                    value.signedness,
                )
            return _sv_cast(
                f"(({left}) {symbol[value.operator]} ({right}))",
                value.width,
                value.signedness,
            )
        operand_signedness = value.left.signedness
        operand_width = value.left.width
        left = _sv_cast(left, operand_width, operand_signedness)
        right = _sv_cast(right, operand_width, operand_signedness)
        if value.operator in {
            FormalBinaryOperator.LESS,
            FormalBinaryOperator.LESS_EQUAL,
            FormalBinaryOperator.GREATER,
            FormalBinaryOperator.GREATER_EQUAL,
        }:
            return render_ordered_comparison(
                left,
                symbol[value.operator],
                right,
                signed=operand_signedness is FormalSignedness.SIGNED,
            )
        return f"(({left}) {symbol[value.operator]} ({right}))"
    raise FormalError(f"unsupported structured predicate node: {type(value).__name__}")


def render_bound_predicate(
    value: FormalPredicate,
    bindings: dict[str, SignalBinding],
) -> str:
    """Render one typed predicate using an explicit semantic binding map.

    This is the shared conservative SystemVerilog lowering used by connected
    top-level and recursive M35 harnesses. Keeping it public prevents backend
    adapters from reconstructing executable meaning from legacy report text.
    Every observation must be present in ``bindings``; no RTL-name fallback or
    token guessing is performed.
    """

    try:
        require_predicate(value)
    except FormalPredicateError as error:
        raise FormalError(str(error)) from error
    return _render_predicate(value, bindings)


def _canonical_dut_ports(
    ports: tuple[SignalBinding, ...],
) -> tuple[SignalBinding, ...]:
    """Return one physical DUT port per RTL token.

    BackendArtifact manifests intentionally retain both the public-port and
    typed protocol-endpoint semantic aliases for aggregate leaves.  Those
    aliases may name one exact physical RTL port, but a harness must declare
    and connect that port only once.  Alias collapsing is therefore based on
    the published physical contract, never on semantic-name spelling.

    Reusing an RTL token with a different module, width, direction, or domain
    remains a malformed artifact and fails closed.
    """

    canonical: list[SignalBinding] = []
    by_token: dict[str, SignalBinding] = {}
    for item in ports:
        previous = by_token.get(item.rtl_name)
        if previous is None:
            by_token[item.rtl_name] = item
            canonical.append(item)
            continue
        previous_contract = (
            previous.rtl_module,
            previous.width,
            previous.direction,
            previous.clock_domain,
        )
        current_contract = (
            item.rtl_module,
            item.width,
            item.direction,
            item.clock_domain,
        )
        if current_contract != previous_contract:
            def contract_text(binding: SignalBinding) -> str:
                return (
                    f"module={binding.rtl_module!r}, width={binding.width}, "
                    f"direction={binding.direction!r}, "
                    f"domain={binding.clock_domain!r}"
                )

            raise FormalError(
                "connected backend artifact reuses RTL port token "
                f"'{item.rtl_name}' with incompatible physical contracts: "
                f"'{previous.semantic_signal_id}' publishes "
                f"({contract_text(previous)}), while "
                f"'{item.semantic_signal_id}' publishes "
                f"({contract_text(item)})"
            )
    return tuple(canonical)


def _connected_domain_rendering(
    design: FormalDesign,
    items: tuple[FormalProperty | CoverProperty, ...],
    bindings: dict[str, SignalBinding],
    *,
    used_names: set[str],
) -> FormalDomainRendering:
    """Resolve and render the one exact physical domain owned by a harness."""

    clock_binding = bindings.get("clock")
    reset_binding = bindings.get("reset")
    if clock_binding is None:
        raise FormalError("connected formal harness requires a clock binding")
    if reset_binding is None:
        raise FormalError("connected formal harness requires a reset binding")
    domains: list[ClockDomain] = []
    for item in items:
        if item.non_executable_reason is not None:
            continue
        domain, reason = _formal_item_domain_from_design(item, design)
        if reason is not None or domain is None:
            raise FormalError(reason or "formal goal domain is unavailable")
        if domain not in domains:
            domains.append(domain)
    if not domains:
        if design.clock_domains:
            matches = tuple(
                item for item in design.clock_domains
                if item.clock == (clock_binding.clock_domain or item.clock)
            )
            if len(matches) == 1:
                domains.append(matches[0])
        if not domains:
            domains.append(ClockDomain(
                clock_binding.clock_domain or clock_binding.rtl_name,
                reset_binding.rtl_name,
            ))
    if len(domains) != 1:
        raise FormalError(
            "one executable formal harness cannot sample multiple physical domains"
        )
    try:
        return render_formal_domain(
            domains[0],
            clock_name=clock_binding.rtl_name,
            reset_name=reset_binding.rtl_name,
            used_names=used_names,
        )
    except FormalDomainRenderingError as error:
        raise FormalError(str(error)) from error


def _effective_reset_bindings(
    bindings: dict[str, SignalBinding],
    rendering: FormalDomainRendering,
) -> dict[str, SignalBinding]:
    """Return a predicate binding view with normalized effective reset."""

    reset = bindings.get("reset")
    if reset is None:
        return bindings
    # Recursive publication gives every concrete child-reset observation its
    # own semantic identity, while deliberately mapping it to the one physical
    # top-level reset port.  Compare that explicit physical binding instead of
    # guessing from semantic-ID or generated-name spelling: all aliases of the
    # same physical reset must observe the conditioned reset during an async
    # synchronized-release epoch.
    return {
        semantic_id: (
            replace(binding, rtl_name=rendering.reset_active)
            if (
                binding.rtl_module == reset.rtl_module
                and binding.rtl_name == reset.rtl_name
                and binding.width == reset.width
                and binding.direction == reset.direction
            )
            else binding
        )
        for semantic_id, binding in bindings.items()
    }


def _same_physical_binding(left: SignalBinding, right: SignalBinding) -> bool:
    """Return whether two semantic observations name one physical signal."""

    return (
        left.rtl_module == right.rtl_module
        and left.rtl_name == right.rtl_name
        and left.width == right.width
        and left.direction == right.direction
    )


def _previous_guard_name(
    predicate: FormalPredicate,
    bindings: dict[str, SignalBinding],
    *,
    history_valid: str,
    reset_history_valid: str | None,
) -> str | None:
    """Select the valid-history bit for one structured predicate.

    Ordinary previous-cycle values must not cross an effective reset epoch.
    Reset-epoch properties are different: they deliberately ask whether the
    *previous sampled cycle* was in reset, so they need a first-sample guard
    which is not itself cleared throughout synchronized release.
    """

    previous = tuple(
        item
        for item in predicate.observations()
        if item.cycle is ObservationCycle.PREVIOUS
    )
    if not previous:
        return None
    root_reset = bindings.get("reset")
    if root_reset is not None and reset_history_valid is not None and all(
        (bound := bindings.get(item.semantic_signal_id)) is not None
        and _same_physical_binding(bound, root_reset)
        for item in previous
    ):
        return reset_history_valid
    return history_valid


def formal_harness_domain_rendering(
    design: FormalDesign,
    *,
    top: str | None = None,
    cover: bool = False,
) -> FormalDomainRendering:
    """Resolve the exact checker-private reset signal for trace publication.

    This deliberately shares the harness emitter's allocation inputs.  Bundle
    publication can therefore retain the normalized effective reset without
    parsing generated Verilog or guessing a private RTL spelling.
    """

    if design.connected_artifact_hash is None:
        raise FormalError("formal harness domain rendering requires a connected design")
    bindings = {item.semantic_signal_id: item for item in design.bindings}
    dut_ports = _canonical_dut_ports(design.dut_ports)
    observation_tokens = {
        item.rtl_name: item for item in design.bindings
        if item.semantic_signal_id not in {"clock", "reset"}
    }
    used_names = {
        item.rtl_name for item in (*dut_ports, *observation_tokens.values())
    }
    checker_top = top or f"{design.module_name}__m35_formal"
    allocate_private_rtl_identifier(
        "zlang_m35_past_valid",
        semantic_identity=(
            f"{checker_top}|m35|history-valid"
            if cover else f"{design.module_name}|m35|history-valid"
        ),
        used=used_names,
    )
    properties: tuple[FormalProperty | CoverProperty, ...] = (
        tuple(design.properties) + tuple(design.covers)
    )
    return _connected_domain_rendering(
        design,
        properties,
        bindings,
        used_names=used_names,
    )


def emit_harness(design: FormalDesign, *, mode: ProofMode = ProofMode.BMC, depth: int = 20) -> str:
    """Emit a connected checker, or an explicit non-executable report."""
    if depth < 1:
        raise FormalError("formal depth must be positive")
    if design.connected_artifact_hash is None:
        # Preserve a useful compiler artifact without pretending that free
        # semantic wires are an executable proof harness.
        for prop in design.properties:
            if prop.non_executable_reason is not None:
                continue
            for signal in prop.relevant_signals:
                if signal not in {item.semantic_signal_id for item in design.bindings}:
                    raise FormalError(
                        f"property '{prop.id}' has no signal binding for '{signal}'"
                    )
        return _predicate_report(design, mode=mode, depth=depth)
    binding = {item.semantic_signal_id: item for item in design.bindings}
    for prop in design.properties:
        if prop.non_executable_reason is not None:
            continue
        if prop.predicate is None:
            raise FormalError(
                f"property '{prop.id}' has no structured executable predicate"
            )
        if prop.temporal_form in {
            TemporalForm.STABLE_WHILE,
            TemporalForm.BOUNDED_IMPLICATION,
        }:
            raise FormalError(
                f"property '{prop.id}' uses unsupported executable temporal form "
                f"'{prop.temporal_form.value}'"
            )
        for signal in prop.relevant_signals:
            if signal not in binding:
                raise FormalError(
                    f"property '{prop.id}' has no signal binding for '{signal}'"
                )
    assert design.connected_module is not None
    assert design.implementation_text is not None
    dut_ports = _canonical_dut_ports(design.dut_ports)
    port_by_name = {item.rtl_name: item for item in dut_ports}
    input_ports = tuple(item for item in dut_ports if item.direction == "input")
    lines = [design.implementation_text.rstrip(), "", "`default_nettype none"]
    header = f"module {design.module_name}__m35_formal"
    if input_ports:
        declarations = []
        for item in input_ports:
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            declarations.append(f"input wire{packed} {item.rtl_name}")
        header += "(" + ", ".join(declarations) + ");"
    else:
        header += ";"
    lines.append(header)
    for item in dut_ports:
        if item.direction == "output":
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            lines.append(f"  wire{packed} {item.rtl_name};")
    observation_tokens = {
        item.rtl_name: item for item in design.bindings
        if item.semantic_signal_id not in {"clock", "reset"}
    }
    for item in observation_tokens.values():
        if item.rtl_name not in port_by_name:
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            lines.append(f"  wire{packed} {item.rtl_name};")
    connections = [
        f".{item.rtl_name}({item.rtl_name})" for item in dut_ports
    ] + [
        f".{item.rtl_name}({item.rtl_name})"
        for item in observation_tokens.values()
        if item.rtl_name not in port_by_name
    ]
    lines.append(
        f"  {design.connected_module} dut (" + ", ".join(connections) + ");"
    )
    used_names = {
        item.rtl_name for item in (*dut_ports, *observation_tokens.values())
    }
    history_valid = allocate_private_rtl_identifier(
        "zlang_m35_past_valid",
        semantic_identity=f"{design.module_name}|m35|history-valid",
        used=used_names,
    )
    rendering = _connected_domain_rendering(
        design,
        design.properties,
        binding,
        used_names=used_names,
    )
    predicate_binding = _effective_reset_bindings(binding, rendering)
    lines.extend(f"  {item}" for item in rendering.support_lines)
    lines.append(f"  {rendering.initial_assumption}")
    lines.append(f"  reg {history_valid} = 1'b0;")
    legacy = rendering.domain.is_legacy_default
    reset_history_valid = None
    if not legacy:
        reset_history_valid = allocate_private_rtl_identifier(
            "zlang_m35_reset_past_valid",
            semantic_identity=f"{design.module_name}|m35|reset-history-valid",
            used=used_names,
        )
        lines.append(f"  reg {reset_history_valid} = 1'b0;")
        lines.append(f"  always @({rendering.sample_event}) begin")
        lines.append(f"    {reset_history_valid} <= 1'b1;")
        lines.append("  end")
    if legacy:
        lines.append(f"  always @({rendering.sample_event}) begin")
        lines.append(f"    {history_valid} <= 1'b1;")
    else:
        lines.append(f"  always @({rendering.history_event}) begin")
        lines.append(f"    if ({rendering.external_reset_asserted}) begin")
        lines.append(f"      {history_valid} <= 1'b0;")
        if rendering.release_tracker_name is not None:
            lines.append(
                f"    end else if ({rendering.reset_active}) begin"
            )
            lines.append(f"      {history_valid} <= 1'b0;")
        lines.append("    end else begin")
        lines.append(f"      {history_valid} <= 1'b1;")
        lines.extend(["    end", "  end"])
        lines.append(f"  always @({rendering.sample_event}) begin")
    property_indent = "    "
    for prop in design.properties:
        if prop.non_executable_reason is not None:
            lines.append(
                f"{property_indent}// not-run {prop.id}: "
                f"{prop.non_executable_reason}"
            )
            continue
        assert prop.predicate is not None
        statement = "assume" if prop.kind is PropertyKind.ASSUMPTION else "assert"
        condition = render_bound_predicate(prop.predicate, predicate_binding)
        guards: list[str] = []
        previous_guard = _previous_guard_name(
            prop.predicate,
            binding,
            history_valid=history_valid,
            reset_history_valid=reset_history_valid,
        )
        if previous_guard is not None:
            guards.append(previous_guard)
        if prop.reset_condition is not None:
            guards.append(f"!{rendering.reset_active}")
        guard = " && ".join(guards)
        if guard:
            lines.append(
                f"{property_indent}if ({guard}) {statement} ({condition}); "
                f"// {prop.id}"
            )
        else:
            lines.append(
                f"{property_indent}{statement} ({condition}); // {prop.id}"
            )
    lines.extend(["  end", "endmodule", "`default_nettype wire", ""])
    return "\n".join(lines)


def cover_harness_top(design: FormalDesign, cover_id: str) -> str:
    """Return the stable wrapper name for one per-goal cover harness."""

    if not cover_id:
        raise FormalError("cover harness requires a property id")
    digest = hashlib.sha256(cover_id.encode()).hexdigest()[:12]
    return f"{design.module_name}__m35_cover_{digest}"


def emit_cover_harness(
    design: FormalDesign,
    *,
    cover_id: str,
    depth: int = 20,
    top: str | None = None,
) -> str:
    """Emit one connected bounded-reachability checker.

    One goal per harness keeps SBY's pass/fail cover result unambiguous.  A
    connected implementation and the exact backend-published observation
    bindings are mandatory for executable output; an unconnected design emits
    the same explicit non-executable report used by safety properties.
    """

    if depth < 1:
        raise FormalError("formal depth must be positive")
    matches = tuple(item for item in design.covers if item.id == cover_id)
    if len(matches) != 1:
        if not matches:
            raise FormalError(f"unknown cover property: {cover_id}")
        raise FormalError(f"duplicate cover property id: {cover_id}")
    prop = matches[0]
    if design.connected_artifact_hash is None:
        if prop.non_executable_reason is None:
            available = {item.semantic_signal_id for item in design.bindings}
            for signal in prop.relevant_signals:
                if signal not in available:
                    raise FormalError(
                        f"cover property '{prop.id}' has no signal binding for '{signal}'"
                    )
        return _predicate_report(design, mode=ProofMode.BMC, depth=depth)
    if prop.non_executable_reason is not None:
        return _predicate_report(
            replace(
                design,
                non_executable_reason=prop.non_executable_reason,
            ),
            mode=ProofMode.BMC,
            depth=depth,
        )
    if prop.predicate is None:
        raise FormalError(
            f"cover property '{prop.id}' has no structured executable predicate"
        )

    binding = {item.semantic_signal_id: item for item in design.bindings}
    for signal in prop.relevant_signals:
        if signal not in binding:
            raise FormalError(
                f"cover property '{prop.id}' has no signal binding for '{signal}'"
            )
    assumptions = tuple(
        item for item in design.properties
        if item.kind is PropertyKind.ASSUMPTION
    )
    for assumption in assumptions:
        if assumption.non_executable_reason is not None:
            return _predicate_report(
                replace(
                    design,
                    non_executable_reason=(
                        f"cover property '{prop.id}' requires executable assumption "
                        f"'{assumption.id}': {assumption.non_executable_reason}"
                    ),
                ),
                mode=ProofMode.BMC,
                depth=depth,
            )
        if assumption.predicate is None:
            raise FormalError(
                f"assumption '{assumption.id}' has no structured executable predicate"
            )
        if assumption.temporal_form in {
            TemporalForm.STABLE_WHILE,
            TemporalForm.BOUNDED_IMPLICATION,
        }:
            raise FormalError(
                f"assumption '{assumption.id}' uses unsupported executable temporal "
                f"form '{assumption.temporal_form.value}'"
            )
        for signal in assumption.relevant_signals:
            if signal not in binding:
                raise FormalError(
                    f"assumption '{assumption.id}' has no signal binding for '{signal}'"
                )
    assert design.connected_module is not None
    assert design.implementation_text is not None
    top = top or cover_harness_top(design, cover_id)
    if not _IDENT.match(top):
        raise FormalError(f"cover harness top is not a legal identifier: {top!r}")

    dut_ports = _canonical_dut_ports(design.dut_ports)
    port_by_name = {item.rtl_name: item for item in dut_ports}
    input_ports = tuple(item for item in dut_ports if item.direction == "input")
    lines = [design.implementation_text.rstrip(), "", "`default_nettype none"]
    header = f"module {top}"
    if input_ports:
        declarations = []
        for item in input_ports:
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            declarations.append(f"input wire{packed} {item.rtl_name}")
        header += "(" + ", ".join(declarations) + ");"
    else:
        header += ";"
    lines.append(header)
    for item in dut_ports:
        if item.direction == "output":
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            lines.append(f"  wire{packed} {item.rtl_name};")
    observation_tokens = {
        item.rtl_name: item for item in design.bindings
        if item.semantic_signal_id not in {"clock", "reset"}
    }
    for item in observation_tokens.values():
        if item.rtl_name not in port_by_name:
            packed = "" if item.width == 1 else f" [{item.width - 1}:0]"
            lines.append(f"  wire{packed} {item.rtl_name};")
    connections = [
        f".{item.rtl_name}({item.rtl_name})" for item in dut_ports
    ] + [
        f".{item.rtl_name}({item.rtl_name})"
        for item in observation_tokens.values()
        if item.rtl_name not in port_by_name
    ]
    lines.append(
        f"  {design.connected_module} dut (" + ", ".join(connections) + ");"
    )
    used_names = {
        item.rtl_name for item in (*dut_ports, *observation_tokens.values())
    }
    history_valid = allocate_private_rtl_identifier(
        "zlang_m35_past_valid",
        semantic_identity=f"{top}|m35|history-valid",
        used=used_names,
    )
    rendering = _connected_domain_rendering(
        design,
        (*assumptions, prop),
        binding,
        used_names=used_names,
    )
    predicate_binding = _effective_reset_bindings(binding, rendering)
    lines.extend(f"  {item}" for item in rendering.support_lines)
    lines.append(f"  {rendering.initial_assumption}")
    cover_needs_past = any(
        item.cycle is ObservationCycle.PREVIOUS
        for item in prop.predicate.observations()
    )
    needs_past = cover_needs_past or any(
        observation.cycle is ObservationCycle.PREVIOUS
        for assumption in assumptions
        if assumption.predicate is not None
        for observation in assumption.predicate.observations()
    )
    if needs_past:
        lines.append(f"  reg {history_valid} = 1'b0;")
    legacy = rendering.domain.is_legacy_default
    reset_history_valid = None
    if needs_past and not legacy:
        reset_history_valid = allocate_private_rtl_identifier(
            "zlang_m35_reset_past_valid",
            semantic_identity=f"{top}|m35|reset-history-valid",
            used=used_names,
        )
        lines.append(f"  reg {reset_history_valid} = 1'b0;")
        lines.append(f"  always @({rendering.sample_event}) begin")
        lines.append(f"    {reset_history_valid} <= 1'b1;")
        lines.append("  end")
    if needs_past and not legacy:
        lines.append(f"  always @({rendering.history_event}) begin")
        lines.append(f"    if ({rendering.external_reset_asserted}) begin")
        lines.append(f"      {history_valid} <= 1'b0;")
        if rendering.release_tracker_name is not None:
            lines.append(
                f"    end else if ({rendering.reset_active}) begin"
            )
            lines.append(f"      {history_valid} <= 1'b0;")
        lines.append("    end else begin")
        lines.append(f"      {history_valid} <= 1'b1;")
        lines.extend(["    end", "  end"])
    lines.append(f"  always @({rendering.sample_event}) begin")
    statement_indent = "    "
    if needs_past and legacy:
        lines.append(f"    {history_valid} <= 1'b1;")
    for assumption in assumptions:
        if assumption.non_executable_reason is not None:
            lines.append(
                f"    // not-run {assumption.id}: "
                f"{assumption.non_executable_reason}"
            )
            continue
        assert assumption.predicate is not None
        assumption_condition = render_bound_predicate(
            assumption.predicate, predicate_binding
        )
        assumption_guards: list[str] = []
        previous_guard = _previous_guard_name(
            assumption.predicate,
            binding,
            history_valid=history_valid,
            reset_history_valid=reset_history_valid,
        )
        if previous_guard is not None:
            assumption_guards.append(previous_guard)
        if assumption.reset_condition is not None:
            assumption_guards.append(f"!{rendering.reset_active}")
        assumption_guard = " && ".join(assumption_guards)
        if assumption_guard:
            lines.append(
                f"{statement_indent}if ({assumption_guard}) "
                f"assume ({assumption_condition}); "
                f"// {assumption.id}"
            )
        else:
            lines.append(
                f"{statement_indent}assume ({assumption_condition}); "
                f"// {assumption.id}"
            )
    condition = render_bound_predicate(prop.predicate, predicate_binding)
    guards: list[str] = []
    if cover_needs_past:
        previous_guard = _previous_guard_name(
            prop.predicate,
            binding,
            history_valid=history_valid,
            reset_history_valid=reset_history_valid,
        )
        assert previous_guard is not None
        guards.append(previous_guard)
    if prop.reset_condition is not None:
        guards.append(f"!{rendering.reset_active}")
    guard = " && ".join(guards)
    if guard:
        lines.append(
            f"{statement_indent}if ({guard}) cover ({condition}); // {prop.id}"
        )
    else:
        lines.append(f"{statement_indent}cover ({condition}); // {prop.id}")
    lines.extend(["  end", "endmodule", "`default_nettype wire", ""])
    return "\n".join(lines)


def classify_result(*, property_id: str, mode: ProofMode, outcome: str,
                    engine: str | None, solver: str | None, depth: int | None = None,
                    source_origin: SourceOrigin | None = None, reason: str | None = None) -> FormalResult:
    """Map an external runner outcome without conflating BMC and proof."""
    normalized = outcome.lower().strip()
    if normalized == "proven" and mode is not ProofMode.PROVE:
        raise FormalError("proven is only valid for prove mode")
    mapping = {"proven": FormalStatus.PROVEN, "failed": FormalStatus.FAILED,
               "unknown": FormalStatus.UNKNOWN, "skipped": FormalStatus.SKIPPED,
               "pass": FormalStatus.BOUNDED_PASS if mode is ProofMode.BMC else FormalStatus.PROVEN}
    if normalized not in mapping:
        raise FormalError(f"unknown formal outcome: {outcome}")
    if normalized == "failed":
        raise FormalError(
            "failed outcomes require typed counterexample metadata; construct "
            "FormalResult with a Counterexample"
        )
    return FormalResult(property_id, mapping[normalized], mode, engine, solver, depth,
                        source_origin=source_origin, reason=reason)
