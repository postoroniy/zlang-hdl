"""Typed combinational expression IR."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum

from zlang.ir.interfaces import (
    CreditSignal,
    PacketSignal,
    ReadyValidSignal,
    RequestResponseChannel,
    VirtualChannelCreditSignal,
)
from zlang.ir.storage import FifoSignal, MemorySignal, RomSignal
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    CompileTimeExpr,
    ExactReductionOperator,
    ExactReductionPlan,
    FunctionalRegionKind,
    FunctionalTable,
    compile_time_range,
)
from zlang.ir.types import (
    BitsType,
    EnumType,
    HardwareType,
    TaggedUnionType,
    TupleType,
    UIntType,
    VecType,
)
from zlang.source import SourceOrigin


@dataclass(frozen=True, kw_only=True)
class TracedExpression:
    """Typed expression retaining its source-level origin when available."""

    origin: SourceOrigin | None = field(default=None, compare=False)


@dataclass(frozen=True)
class InputRef(TracedExpression):
    name: str
    type: HardwareType


@dataclass(frozen=True)
class ParameterRef(TracedExpression):
    name: str
    type: HardwareType


@dataclass(frozen=True)
class RegisterRef(TracedExpression):
    name: str
    type: HardwareType


@dataclass(frozen=True)
class ReadyValidRef(TracedExpression):
    interface: str
    signal: ReadyValidSignal
    type: HardwareType


@dataclass(frozen=True)
class CreditRef(TracedExpression):
    interface: str
    signal: CreditSignal
    type: HardwareType


@dataclass(frozen=True)
class PacketRef(TracedExpression):
    interface: str
    signal: PacketSignal
    type: HardwareType


@dataclass(frozen=True)
class VirtualChannelCreditRef(TracedExpression):
    interface: str
    signal: VirtualChannelCreditSignal
    type: HardwareType


@dataclass(frozen=True)
class RequestResponseRef(TracedExpression):
    interface: str
    channel: RequestResponseChannel
    signal: ReadyValidSignal
    type: HardwareType


@dataclass(frozen=True)
class FifoRef(TracedExpression):
    fifo: str
    signal: FifoSignal
    type: HardwareType


@dataclass(frozen=True)
class MemoryRef(TracedExpression):
    memory: str
    signal: MemorySignal
    type: HardwareType


@dataclass(frozen=True)
class RomRef(TracedExpression):
    rom: str
    signal: RomSignal
    type: HardwareType


@dataclass(frozen=True)
class Add(TracedExpression):
    left: Expression
    right: Expression
    type: HardwareType


class BinaryOperator(str, Enum):
    SUBTRACT = "-"
    MULTIPLY = "*"
    BIT_AND = "&"
    BIT_OR = "|"
    BIT_XOR = "^"
    SHIFT_LEFT = "<<"
    SHIFT_RIGHT = ">>"
    EQUAL = "=="
    NOT_EQUAL = "!="
    LESS = "<"
    LESS_EQUAL = "<="
    GREATER = ">"
    GREATER_EQUAL = ">="


@dataclass(frozen=True)
class Constant(TracedExpression):
    value: int
    type: HardwareType


@dataclass(frozen=True)
class EnumEncode(TracedExpression):
    """Lossless conversion from one nominal enum value to its raw bits."""

    expression: Expression
    type: HardwareType


@dataclass(frozen=True)
class EnumValid(TracedExpression):
    """Exact membership test for one enum's physical code set."""

    expression: Expression
    enum_type: EnumType
    type: HardwareType


@dataclass(frozen=True)
class EnumDecode(TracedExpression):
    """Total raw-to-enum decode using an explicit nominal fallback."""

    expression: Expression
    fallback: Expression
    type: EnumType


@dataclass(frozen=True)
class Binary(TracedExpression):
    operator: BinaryOperator
    left: Expression
    right: Expression
    operand_type: HardwareType
    type: HardwareType


@dataclass(frozen=True)
class Extend(TracedExpression):
    expression: Expression
    type: HardwareType


@dataclass(frozen=True)
class Truncate(TracedExpression):
    expression: Expression
    type: HardwareType


class FixedRounding(str, Enum):
    TOWARD_ZERO = "toward_zero"
    # Source compatibility name.  New syntax never exposes ambiguous
    # "truncate" terminology.
    TRUNCATE = "toward_zero"
    FLOOR = "floor"
    AWAY_ZERO = "away_zero"
    NEAREST_EVEN = "nearest_even"


class FixedOverflow(str, Enum):
    WRAP = "wrap"
    SATURATE = "saturate"


class FixedConversionKind(str, Enum):
    RESCALE = "rescale"
    FROM_RAW = "from_raw"
    TO_RAW = "to_raw"


@dataclass(frozen=True)
class FixedConvert(TracedExpression):
    expression: Expression
    rounding: FixedRounding
    overflow: FixedOverflow
    kind: FixedConversionKind
    type: HardwareType
    rational_denominator: int | None = None


@dataclass(frozen=True)
class Mux(TracedExpression):
    condition: Expression
    when_true: Expression
    when_false: Expression
    type: HardwareType


@dataclass(frozen=True)
class SwitchCase:
    key: int
    expression: Expression


@dataclass(frozen=True)
class Switch(TracedExpression):
    selector: Expression
    cases: tuple[SwitchCase, ...]
    default: Expression
    type: HardwareType


@dataclass(frozen=True)
class Call(TracedExpression):
    function: str
    arguments: tuple[Expression, ...]
    type: HardwareType
    # Legacy source functions resolve by ``function``.  Monomorphic generic
    # and operator definitions use this stable semantic identity so multiple
    # specializations may share one source-level owner without ambiguity.
    callee_identity: str | None = None

    def __post_init__(self) -> None:
        if not self.function:
            raise ValueError("call function name must not be empty")
        if self.callee_identity == "":
            raise ValueError("call callee identity must not be empty")


@dataclass(frozen=True)
class FieldAccess(TracedExpression):
    expression: Expression
    field: str
    type: HardwareType


@dataclass(frozen=True)
class StructConstruct(TracedExpression):
    struct_name: str
    fields: tuple[tuple[str, Expression], ...]
    type: HardwareType


@dataclass(frozen=True)
class TupleConstruct(TracedExpression):
    """Ordered construction of one structural tuple value."""

    elements: tuple[Expression, ...]
    type: TupleType

    def __post_init__(self) -> None:
        if tuple(element.type for element in self.elements) != self.type.elements:
            raise ValueError("tuple constructor element types do not match its tuple type")


@dataclass(frozen=True)
class TupleProject(TracedExpression):
    """Compile-time projection of one zero-based tuple component."""

    expression: Expression
    index: int
    type: HardwareType

    def __post_init__(self) -> None:
        if not isinstance(self.expression.type, TupleType):
            raise ValueError("tuple projection requires a tuple expression")
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise ValueError("tuple projection index must be an integer")
        if not 0 <= self.index < len(self.expression.type.elements):
            raise ValueError(
                f"tuple projection index {self.index} is out of range for "
                f"{self.expression.type}"
            )
        if self.type != self.expression.type.elements[self.index]:
            raise ValueError("tuple projection result type does not match its component")


@dataclass(frozen=True)
class UnionConstruct(TracedExpression):
    variant: str
    fields: tuple[tuple[str, Expression], ...]
    type: TaggedUnionType

    def __post_init__(self) -> None:
        declaration = self.type.variant(self.variant)
        if declaration is None:
            raise ValueError(
                f"tagged union '{self.type.name}' has no variant '{self.variant}'"
            )
        if tuple(name for name, _ in self.fields) != tuple(
            field.name for field in declaration.fields
        ):
            raise ValueError("tagged-union constructor fields do not match declaration")
        if any(
            value.type != field.type
            for field, (_, value) in zip(
                declaration.fields, self.fields, strict=True
            )
        ):
            raise ValueError("tagged-union constructor field type mismatch")


@dataclass(frozen=True)
class UnionTag(TracedExpression):
    expression: Expression
    type: BitsType

    def __post_init__(self) -> None:
        if not isinstance(self.expression.type, TaggedUnionType):
            raise ValueError("tagged-union tag projection requires a union value")
        if self.type != BitsType(self.expression.type.tag_width):
            raise ValueError("tagged-union tag projection has incorrect width")


@dataclass(frozen=True)
class UnionField(TracedExpression):
    expression: Expression
    variant: str
    field: str
    type: HardwareType

    def __post_init__(self) -> None:
        if not isinstance(self.expression.type, TaggedUnionType):
            raise ValueError("tagged-union field projection requires a union value")
        declaration = self.expression.type.variant(self.variant)
        field = None if declaration is None else declaration.field(self.field)
        if field is None or field.type != self.type:
            raise ValueError("tagged-union field projection does not match declaration")


@dataclass(frozen=True)
class FunctionalCaptureRef(TracedExpression):
    """Typed use of one runtime value captured by a functional region."""

    identity: str
    display_name: str
    type: HardwareType

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("functional capture identity must not be empty")
        if not self.display_name:
            raise ValueError("functional capture display name must not be empty")


@dataclass(frozen=True)
class FunctionalTableLookup(TracedExpression):
    """Typed lookup selected exclusively by compile-time binder arithmetic."""

    table_name: str
    index: CompileTimeExpr
    type: HardwareType
    # Functional compaction can replace a statically indexed vector element
    # with a table lookup.  Retain the already proven interval so a later
    # runtime index does not lose its safety proof merely because the compact
    # representation changed.
    value_range: "ValueRange | None" = None

    def __post_init__(self) -> None:
        if not self.table_name:
            raise ValueError("functional table lookup name must not be empty")
        if not isinstance(self.index, CompileTimeExpr):
            raise ValueError("functional table lookup index must be compile-time")
        if self.value_range is None and isinstance(self.type, (UIntType, BitsType)):
            object.__setattr__(
                self,
                "value_range",
                ValueRange(0, (1 << self.type.width) - 1, "static_type"),
            )
        if self.value_range is not None:
            if not isinstance(self.type, (UIntType, BitsType)):
                raise ValueError(
                    "functional table value ranges require an unsigned integral type"
                )
            maximum = (1 << self.type.width) - 1
            if self.value_range.minimum < 0 or self.value_range.maximum > maximum:
                raise ValueError(
                    "functional table value range is outside its result type"
                )


@dataclass(frozen=True)
class VectorIndex(TracedExpression):
    expression: Expression
    index: int | CompileTimeExpr
    type: HardwareType

    def __post_init__(self) -> None:
        if not isinstance(self.expression.type, VecType):
            raise ValueError("static vector index requires a vector expression")
        if self.type != self.expression.type.element_type:
            raise ValueError("static vector index result type does not match vector element")
        if isinstance(self.index, bool) or not isinstance(
            self.index, (int, CompileTimeExpr)
        ):
            raise ValueError("static vector index must be an integer or compile-time expression")
        minimum, maximum = compile_time_range(self.index)
        if minimum < 0 or maximum >= self.expression.type.length:
            raise ValueError(
                f"static vector index range {minimum}..{maximum} is out of bounds "
                f"for {self.expression.type}"
            )


@dataclass(frozen=True)
class ValueRange:
    """Conservative compile-time interval for one concrete value expression."""

    minimum: int
    maximum: int
    provenance: str = "derived"

    def __post_init__(self) -> None:
        if self.minimum > self.maximum:
            raise ValueError("value range minimum must not exceed maximum")


@dataclass(frozen=True)
class RuntimeIndex(TracedExpression):
    expression: Expression
    index: Expression
    vector_length: int
    index_range: ValueRange
    type: HardwareType

    def __post_init__(self) -> None:
        if not isinstance(self.expression.type, VecType):
            raise ValueError("runtime index source must be a vector")
        if self.vector_length != self.expression.type.length:
            raise ValueError("runtime index length does not match its vector type")
        if self.type != self.expression.type.element_type:
            raise ValueError("runtime index result does not match the element type")
        if not isinstance(self.index.type, (UIntType, BitsType)):
            raise ValueError("runtime index selector must be unsigned integral")
        if (
            self.index_range.minimum < 0
            or self.index_range.maximum >= self.vector_length
        ):
            raise ValueError("runtime index range is outside its vector")


@dataclass(frozen=True)
class VectorUpdate(TracedExpression):
    """Pure replacement of one element in a one-dimensional vector value."""

    expression: Expression
    index: Expression
    value: Expression
    vector_length: int
    index_range: ValueRange
    type: HardwareType

    def __post_init__(self) -> None:
        if not isinstance(self.expression.type, VecType):
            raise ValueError("vector update source must be a vector")
        if self.type != self.expression.type:
            raise ValueError("vector update result must retain the source vector type")
        if self.vector_length != self.expression.type.length:
            raise ValueError("vector update length does not match its vector type")
        if self.value.type != self.expression.type.element_type:
            raise ValueError("vector update value does not match the element type")
        if not isinstance(self.index.type, (UIntType, BitsType)):
            raise ValueError("vector update index must be unsigned integral")
        if (
            self.index_range.minimum < 0
            or self.index_range.maximum >= self.vector_length
        ):
            raise ValueError("vector update index range is outside its vector")


@dataclass(frozen=True)
class Slice(TracedExpression):
    """Inclusive bit slice with already-resolved compile-time bounds."""

    expression: Expression
    msb: int
    lsb: int
    type: HardwareType


@dataclass(frozen=True)
class Concat(TracedExpression):
    """Bit concatenation in MSB-to-LSB source order."""

    operands: tuple[Expression, ...]
    type: HardwareType


@dataclass(frozen=True)
class Bitcast(TracedExpression):
    """Exact-width representation reinterpretation."""

    expression: Expression
    type: HardwareType


@dataclass(frozen=True)
class VectorConcat(TracedExpression):
    """Homogeneous vector concatenation preserving source sequence order."""

    operands: tuple[Expression, ...]
    type: HardwareType


@dataclass(frozen=True)
class Reshape(TracedExpression):
    """Vector-only shape change preserving outer-to-inner leaf order."""

    expression: Expression
    type: HardwareType


@dataclass(frozen=True)
class Pack(TracedExpression):
    expression: Expression
    type: HardwareType


@dataclass(frozen=True)
class Unpack(TracedExpression):
    expression: Expression
    type: HardwareType


@dataclass(frozen=True)
class InstanceOutputRef(TracedExpression):
    instance: str
    port: str
    type: HardwareType


@dataclass(frozen=True)
class Generate(TracedExpression):
    index: str
    start: int
    stop: int
    elements: tuple[Expression, ...]
    type: HardwareType


@dataclass(frozen=True)
class FunctionalRegion(TracedExpression):
    """One bounded typed template evaluated over a compile-time binder."""

    kind: FunctionalRegionKind
    binder: CompileTimeBinderRef
    template: Expression
    tables: tuple[FunctionalTable, ...]
    captures: tuple[tuple[FunctionalCaptureRef, Expression], ...]
    type: HardwareType

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FunctionalRegionKind):
            try:
                object.__setattr__(self, "kind", FunctionalRegionKind(self.kind))
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid functional region kind {self.kind!r}") from error
        if not isinstance(self.binder, CompileTimeBinderRef):
            raise ValueError("functional region requires a compile-time binder")
        if not isinstance(self.type, VecType):
            raise ValueError("functional region result must be a vector")
        if self.type.length != self.binder.stop - self.binder.start:
            raise ValueError("functional region vector length does not match binder domain")
        if self.template.type != self.type.element_type:
            raise ValueError("functional region template type does not match vector element")
        table_names = tuple(table.name for table in self.tables)
        if len(table_names) != len(set(table_names)):
            raise ValueError("functional region table names must be unique")
        capture_ids = tuple(reference.identity for reference, _ in self.captures)
        if len(capture_ids) != len(set(capture_ids)):
            raise ValueError("functional region capture identities must be unique")
        for reference, value in self.captures:
            if reference.type != value.type:
                raise ValueError(
                    f"functional capture '{reference.display_name}' type does not match"
                )
        owned_values = (
            self.template,
            *(value for table in self.tables for value in table.values),
            *(value for _, value in self.captures),
        )
        if any(_functional_value_has_effect(value) for value in owned_values):
            raise ValueError("functional region must be pure combinational IR")
        table_by_name = {table.name: table for table in self.tables}
        capture_by_identity = {
            reference.identity: reference for reference, _ in self.captures
        }
        references = _functional_references(
            (self.template, *(value for table in self.tables for value in table.values))
        )
        binder_identities = _functional_binder_identities(
            (self.template, *(value for table in self.tables for value in table.values))
        )
        foreign_binders = binder_identities - {self.binder.identity}
        if foreign_binders:
            raise ValueError(
                "functional region uses a foreign or escaped compile-time binder"
            )
        for reference in references[0]:
            declared = capture_by_identity.get(reference.identity)
            if declared is None or declared.type != reference.type:
                raise ValueError(
                    f"functional region uses undeclared capture '{reference.display_name}'"
                )
        for lookup in references[1]:
            table = table_by_name.get(lookup.table_name)
            if table is None:
                raise ValueError(
                    f"functional region uses unknown table '{lookup.table_name}'"
                )
            if table.type != lookup.type:
                raise ValueError(
                    f"functional table lookup '{lookup.table_name}' type does not match"
                )
            minimum, maximum = compile_time_range(lookup.index)
            if minimum < table.start or maximum >= table.stop:
                raise ValueError(
                    f"functional table lookup '{lookup.table_name}' range "
                    f"{minimum}..{maximum} is outside {table.start}..{table.stop}"
                )


def _functional_references(
    roots: tuple[Expression, ...],
) -> tuple[tuple[FunctionalCaptureRef, ...], tuple[FunctionalTableLookup, ...]]:
    captures: list[FunctionalCaptureRef] = []
    lookups: list[FunctionalTableLookup] = []

    def walk(value: object) -> None:
        if isinstance(value, FunctionalCaptureRef):
            captures.append(value)
            return
        if isinstance(value, FunctionalTableLookup):
            lookups.append(value)
            return
        if isinstance(value, FunctionalRegion):
            return
        if isinstance(value, tuple):
            for item in value:
                walk(item)
            return
        if is_dataclass(value) and not isinstance(value, type):
            for item in fields(value):
                if item.name in {"type", "origin"}:
                    continue
                walk(getattr(value, item.name))

    for expression in roots:
        walk(expression)
    return tuple(captures), tuple(lookups)


def _functional_binder_identities(value: object) -> set[str]:
    """Collect binder identities owned directly by one functional region."""

    result: set[str] = set()

    def walk(item: object) -> None:
        if isinstance(item, CompileTimeBinderRef):
            result.add(item.identity)
            return
        if isinstance(item, FunctionalRegion):
            return
        if isinstance(item, tuple):
            for child in item:
                walk(child)
            return
        if is_dataclass(item) and not isinstance(item, type):
            for field_ in fields(item):
                if field_.name in {"type", "origin"}:
                    continue
                walk(getattr(item, field_.name))

    walk(value)
    return result


def _functional_value_has_effect(value: object) -> bool:
    if isinstance(value, ReadyValidRef):
        # Payload is a pure current-cycle value.  The handshake observations
        # remain effectful and may not be owned by a FunctionalRegion.
        return value.signal is not ReadyValidSignal.PAYLOAD
    if isinstance(
        value,
        (
            RegisterRef,
            CreditRef,
            PacketRef,
            VirtualChannelCreditRef,
            RequestResponseRef,
            FifoRef,
            MemoryRef,
            RomRef,
            Delay,
            Pipeline,
            ImplementationChoice,
        ),
    ):
        return True
    if isinstance(value, FunctionalRegion):
        return False
    if isinstance(value, tuple):
        return any(_functional_value_has_effect(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return any(
            _functional_value_has_effect(getattr(value, item.name))
            for item in fields(value)
            if item.name not in {"type", "origin"}
        )
    return False


@dataclass(frozen=True)
class Map(TracedExpression):
    index: str
    start: int
    stop: int
    elements: tuple[Expression, ...]
    type: HardwareType


@dataclass(frozen=True)
class Dot(TracedExpression):
    left: Expression
    right: Expression
    products: tuple[Expression, ...]
    type: HardwareType


class ReductionOperator(str, Enum):
    ADD = "+"
    MULTIPLY = "*"
    BIT_AND = "&"
    BIT_OR = "|"
    BIT_XOR = "^"


@dataclass(frozen=True)
class Reduce(TracedExpression):
    operator: ReductionOperator
    collection: Expression
    type: HardwareType
    # Nominal aggregate reductions retain their exact, overload-resolved
    # balanced tree while preserving the high-level reduction identity.
    expanded: Expression | None = None
    # Compact fallback regions retain the same exact midpoint tree as a typed
    # operation schedule instead of cloning its expression body.
    plan: ExactReductionPlan | None = None

    def __post_init__(self) -> None:
        if self.expanded is not None and self.plan is not None:
            raise ValueError("reduction cannot carry both an expansion and a plan")
        if self.plan is None:
            return
        if self.operator is not ReductionOperator.ADD:
            raise ValueError("exact reduction plans currently support only addition")
        if self.plan.operator is not ExactReductionOperator.ADD:
            raise ValueError("exact reduction plan operator does not match reduction")
        if not isinstance(self.collection.type, VecType):
            raise ValueError("exact reduction plan requires a vector collection")
        if self.collection.type.length != self.plan.length:
            raise ValueError("exact reduction plan length does not match collection")
        if self.collection.type.element_type != self.plan.leaf_type:
            raise ValueError("exact reduction plan leaf type does not match collection")
        if self.type != self.plan.root_type:
            raise ValueError("exact reduction plan root type does not match reduction")


@dataclass(frozen=True)
class Delay(TracedExpression):
    cycles: int
    expression: Expression
    instance: int
    type: HardwareType


@dataclass(frozen=True)
class Pipeline(TracedExpression):
    stages: int
    expression: Expression
    instance: int
    type: HardwareType


class ImplementationKind(str, Enum):
    MUL_ADD = "mul_add"
    DSP_MAC = "dsp_mac"


class ImplementationResource(str, Enum):
    LOGIC = "logic"
    DSP = "dsp"


class CostMetric(str, Enum):
    LUT = "lut"
    FF = "ff"
    DSP = "dsp"
    BRAM = "bram"
    LATENCY = "latency"
    INITIATION_INTERVAL = "ii"
    FMAX_EST = "fmax_est"


class SynthesisFeedback(str, Enum):
    OPTIONAL_YOSYS = "optional_yosys"


@dataclass(frozen=True)
class CostConstraint:
    metric: CostMetric
    maximum: int


@dataclass(frozen=True)
class CostPolicy:
    goal: CostMetric
    constraints: tuple[CostConstraint, ...]
    feedback: SynthesisFeedback | None = None


@dataclass(frozen=True)
class ImplementationCostEstimate:
    """Target-independent first-pass estimate; never a measured result."""

    lut: int
    ff: int
    dsp: int
    bram: int
    latency: int
    initiation_interval: int
    fmax_est: float | None = None

    def value(self, metric: CostMetric) -> int:
        return {
            CostMetric.LUT: self.lut,
            CostMetric.FF: self.ff,
            CostMetric.DSP: self.dsp,
            CostMetric.BRAM: self.bram,
            CostMetric.LATENCY: self.latency,
            CostMetric.INITIATION_INTERVAL: self.initiation_interval,
            CostMetric.FMAX_EST: self.fmax_est,
        }[metric]


@dataclass(frozen=True)
class YosysMeasurement:
    """Measured generic synthesis data, separate from cost estimates."""

    candidate_hash: str
    cache_key: str
    yosys_version: str
    clash_version: str
    target: str
    constraints: tuple[tuple[str, str], ...]
    lut_cells: int
    flip_flops: int
    total_cells: int
    logic_depth: int


class ImplementationEquivalence(str, Enum):
    MATHEMATICAL = "mathematical"
    CYCLE_ACCURATE = "cycle_accurate"
    PROTOCOL_OBSERVATIONAL = "protocol_observational"


@dataclass(frozen=True)
class ImplementationApplicability:
    operation: str
    conditions: tuple[str, ...]
    multiplier_left_type: HardwareType
    multiplier_right_type: HardwareType
    addend_type: HardwareType
    result_type: HardwareType
    resource_hint: ImplementationResource


@dataclass(frozen=True)
class ImplementationSemantics:
    result_type: HardwareType
    latency: int
    initiation_interval: int
    protocol_events: tuple[str, ...]


@dataclass(frozen=True)
class ImplementationAlternative:
    kind: ImplementationKind
    expression: Expression
    applicability: ImplementationApplicability
    semantics: ImplementationSemantics
    estimate: ImplementationCostEstimate | None = None
    measurement: YosysMeasurement | None = None


@dataclass(frozen=True)
class ImplementationChoice(TracedExpression):
    selected: ImplementationKind | None
    alternatives: tuple[ImplementationAlternative, ...]
    proven_equivalences: tuple[ImplementationEquivalence, ...]
    type: HardwareType
    cost_policy: CostPolicy | None = None
    # M39 evidence is selection metadata and is intentionally excluded from
    # expression equality/canonical hardware identity.
    formal_records: tuple[object, ...] = field(default=(), compare=False)
    formal_eligible: tuple[ImplementationKind, ...] = field(
        default=(), compare=False
    )

    @property
    def selected_alternative(self) -> ImplementationAlternative:
        if self.selected is None:
            raise ValueError("automatic implementation choice has not been extracted")
        return next(
            alternative
            for alternative in self.alternatives
            if alternative.kind is self.selected
        )


Expression = (
    InputRef
    | ParameterRef
    | RegisterRef
    | ReadyValidRef
    | CreditRef
    | PacketRef
    | VirtualChannelCreditRef
    | RequestResponseRef
    | FifoRef
    | MemoryRef
    | RomRef
    | Constant
    | EnumEncode
    | EnumValid
    | EnumDecode
    | Add
    | Binary
    | Extend
    | Truncate
    | FixedConvert
    | Mux
    | Switch
    | Call
    | FieldAccess
    | StructConstruct
    | TupleConstruct
    | TupleProject
    | UnionConstruct
    | UnionTag
    | UnionField
    | FunctionalCaptureRef
    | FunctionalTableLookup
    | VectorIndex
    | RuntimeIndex
    | VectorUpdate
    | Slice
    | Concat
    | Bitcast
    | VectorConcat
    | Reshape
    | Pack
    | Unpack
    | InstanceOutputRef
    | Generate
    | FunctionalRegion
    | Map
    | Dot
    | Reduce
    | Delay
    | Pipeline
    | ImplementationChoice
)
