"""AST nodes which preserve source-level concepts without semantic types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from zlang.source import SourceSpan


class Direction(str, Enum):
    INPUT = "in"
    OUTPUT = "out"


@dataclass(frozen=True)
class TypeName:
    text: str


@dataclass(frozen=True)
class VectorTypeName:
    length: int | str
    element_type: TypeSyntax


@dataclass(frozen=True)
class TupleTypeName:
    """One structural source tuple type in component order."""

    elements: tuple[TypeSyntax, ...]

    def __post_init__(self) -> None:
        if not 2 <= len(self.elements) <= 8:
            raise ValueError("a tuple type requires between 2 and 8 components")

    def __str__(self) -> str:
        return f"({','.join(str(element) for element in self.elements)})"


TypeSyntax = TypeName | VectorTypeName | TupleTypeName


class InterfaceKind(str, Enum):
    WIRE = "wire"
    READY_VALID = "rv"
    CREDIT = "credit"
    PACKET = "packet"
    VC_CREDIT = "vc_credit"


@dataclass(frozen=True)
class InterfaceTypeName:
    kind: InterfaceKind
    payload_type: TypeSyntax
    capacity: int | None = None
    virtual_channels: int | None = None


PortTypeSyntax = TypeSyntax | InterfaceTypeName


@dataclass(frozen=True)
class TypeAlias:
    name: str
    target: TypeSyntax


@dataclass(frozen=True)
class EnumDecl:
    """A source-level nominal enumeration in declaration order."""

    name: str
    members: tuple[str, ...]
    source_identity: str | None = field(default=None, compare=False)
    origin: SourceSpan | None = field(default=None, compare=False)
    backing_type: TypeSyntax | None = None
    encodings: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class TaggedUnionFieldDecl:
    name: str
    type_name: TypeSyntax


@dataclass(frozen=True)
class TaggedUnionVariantDecl:
    name: str
    fields: tuple[TaggedUnionFieldDecl, ...] = ()


@dataclass(frozen=True)
class TaggedUnionDecl:
    """A source-level nominal tagged union in representation order."""

    name: str
    variants: tuple[TaggedUnionVariantDecl, ...]
    source_identity: str | None = field(default=None, compare=False)
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ImportDecl:
    """One dotted logical source-module import.

    Physical checkout, installed-stdlib, path-dependency, and locked Git-cache
    locations are deliberately absent from syntax.  A per-compilation resolver
    maps this logical identity to one immutable source record.
    """

    path: str
    origin: SourceSpan | None = field(default=None, compare=False)
    # A source-local logical namespace only.  It never changes the resolved
    # module identity and is erased before semantic typing/canonical lowering.
    alias: str | None = None


@dataclass(frozen=True)
class ResourcePortDecl:
    name: str
    direction: str
    signedness: str
    width: int


@dataclass(frozen=True)
class ResourceRegisterSiteDecl:
    name: str
    minimum: int
    maximum: int


@dataclass(frozen=True)
class ResourceDedicatedLinkDecl:
    name: str
    source_port: str
    destination_port: str
    width: int
    relation: str
    fabric_fallback: bool = False


@dataclass(frozen=True)
class ResourcePipelineSiteDecl:
    name: str
    semantic_location: str
    latency_delta: int
    initiation_interval: int
    resource_local: bool
    estimated_delay_ps: int


@dataclass(frozen=True)
class ResourcePipelineConfigurationDecl:
    name: str
    sites: tuple[str, ...]
    latency: int
    initiation_interval: int
    physical_settings: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ResourceDefinitionDecl:
    name: str
    ports: tuple[ResourcePortDecl, ...]
    operation: str
    limits: tuple[tuple[str, int], ...]
    register_sites: tuple[ResourceRegisterSiteDecl, ...]
    dedicated_links: tuple[ResourceDedicatedLinkDecl, ...]
    bindings: tuple[tuple[str, str], ...]
    resource_class: str = "generic"
    capabilities: tuple[tuple[str, str], ...] = ()
    pipeline_sites: tuple[ResourcePipelineSiteDecl, ...] = ()
    pipeline_configurations: tuple[ResourcePipelineConfigurationDecl, ...] = ()
    physical_primitive: str | None = None
    physical_site_bindings: tuple[tuple[str, str], ...] = ()
    physical_edge_bindings: tuple[tuple[str, str, str], ...] = ()
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TargetFamilyDecl:
    name: str
    resources: tuple[str, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TargetInstanceDecl:
    name: str
    family: str
    part: str
    inventory: tuple[tuple[str, int], ...]
    dedicated_capacities: tuple[tuple[str, str, int], ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ArchitectureTemplateDecl:
    name: str
    operation: str
    resource: str
    resource_count: int
    latency: int
    initiation_interval: int
    register_configuration: tuple[tuple[str, int], ...]
    pipeline_configuration: str | None = None
    dedicated_link: str | None = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ProtocolChannelDecl:
    name: str
    type_name: PortTypeSyntax
    source_role: str
    sink_role: str
    domain: str | None = None


@dataclass(frozen=True)
class ProtocolDecl:
    name: str
    roles: tuple[str, ...] = ()
    channels: tuple[ProtocolChannelDecl, ...] = ()
    parameters: tuple["ModuleParameter", ...] = ()


@dataclass(frozen=True)
class StructFieldDecl:
    name: str
    type_name: TypeSyntax


@dataclass(frozen=True)
class StructDecl:
    name: str
    fields: tuple[StructFieldDecl, ...]
    parameters: tuple[ModuleParameter, ...] = ()
    source_identity: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AggregateInterfaceDecl:
    name: str
    protocol: str
    arguments: tuple[SpecializationArgument, ...]
    role: str
    domain: str | None = None


@dataclass(frozen=True)
class Parameter:
    name: str
    type_name: TypeSyntax


@dataclass(frozen=True)
class PortDecl:
    direction: Direction
    name: str
    type_name: PortTypeSyntax
    domain: str | None = None
    # ``names`` is populated only for a grouped source declaration.  Keeping
    # the legacy ``name`` field makes older AST consumers/source fixtures
    # source-compatible until semantic normalization expands the group.
    names: tuple[str, ...] = ()
    initializer: "Expression | None" = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ModuleParameter:
    name: str
    kind: str = "value"
    default: int | str | None = None
    # ``constant`` parameters carry one exact, recursively bit-packable
    # compile-time value.  ``callable`` parameters carry a statically resolved
    # pure named function.  Neither kind is a runtime hardware port.
    type_name: TypeSyntax | None = None
    callable_parameters: tuple[TypeSyntax, ...] = ()
    callable_return_type: TypeSyntax | None = None


@dataclass(frozen=True)
class CallableRef:
    """Source reference to one statically selected pure function."""

    name: str
    specializations: tuple["SpecializationArgument", ...] = ()


@dataclass(frozen=True)
class SpecializationArgument:
    name: str | None
    value: int | str | TypeSyntax | CallableRef


@dataclass(frozen=True)
class InstanceDecl:
    name: str
    module: str
    arguments: tuple[SpecializationArgument, ...] = ()
    # Literal lengths remain integers for compatibility.  A parameterized
    # specialization may carry an unevaluated compile-time expression until
    # semantic elaboration resolves it.
    array_length: int | str | None = None
    bindings: tuple["Assignment", ...] = ()


@dataclass(frozen=True)
class GenericDeclaration:
    """Category-neutral ``name : reference`` module-item syntax.

    Semantic analysis resolves the reference as a module specialization,
    aggregate protocol endpoint, or value type.  Keeping that decision out of
    the parser prevents source spelling and capitalization from defining
    hardware meaning.
    """

    name: str
    type_name: TypeSyntax
    role: str | None = None
    domain: str | None = None
    array_length: int | str | None = None
    bindings: tuple["Assignment", ...] = ()
    initializer: "Expression | None" = None
    # Named module/protocol actuals cannot be represented by ``TypeName``'s
    # compact textual spelling without losing their names.  Keep them as
    # syntax metadata until category-neutral declarations are resolved.
    specializations: tuple[SpecializationArgument, ...] = ()
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class StructDestructureDecl:
    """Exhaustive immutable binding of one nominal struct's fields.

    This is syntax-only.  Semantic normalization expands it to one hidden,
    exactly typed local plus ordinary field-access locals before typed IR is
    built, so backends never acquire destructuring-specific behavior.
    """

    type_name: TypeSyntax
    fields: tuple[str, ...]
    expression: "Expression"
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TupleDestructureDecl:
    """Flat immutable binding of one structural tuple's components."""

    names: tuple[str, ...]
    expression: "Expression"
    origin: SourceSpan | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not 2 <= len(self.names) <= 8:
            raise ValueError("tuple destructuring requires between 2 and 8 names")


@dataclass(frozen=True)
class StructFieldValue:
    name: str
    # ``None`` is the syntax-level field pun ``field``.  Semantic analysis
    # resolves it to an ordinary lexical NameExpr before type checking.
    expression: "Expression | None"


class RequestResponseOrdering(str, Enum):
    IN_ORDER = "in_order"
    OUT_OF_ORDER = "out_of_order"


@dataclass(frozen=True)
class RequestResponseDecl:
    name: str
    request_type: TypeSyntax
    response_type: TypeSyntax
    max_outstanding: int
    ordering: RequestResponseOrdering
    match_by: str | None = None


class ConnectionAdapter(str, Enum):
    READY_VALID_TO_CREDIT = "rv_to_credit"
    CREDIT_TO_READY_VALID = "credit_to_rv"


class CrossingKind(str, Enum):
    SYNC_LEVEL = "sync_level"
    PULSE_TOGGLE = "pulse_toggle"
    HANDSHAKE = "handshake"
    ASYNC_FIFO = "async_fifo"


@dataclass(frozen=True)
class Crossing:
    kind: CrossingKind
    depth: int | None = None


@dataclass(frozen=True)
class ConnectionDecl:
    source: str
    destination: str
    buffer_depth: int = 0
    request_buffer_depth: int = 0
    response_buffer_depth: int = 0
    adapter: ConnectionAdapter | None = None
    crossing: Crossing | None = None
    # A bounded ready/valid transform owns the connection rather than adding a
    # second ordinary protocol edge.  The parser retains the existing
    # ``PipelineExpr`` surface; semantic analysis turns it into the dedicated
    # backend-independent elastic region/plan IR.
    transform: "PipelineExpr | None" = None


@dataclass(frozen=True)
class ConnectionChainDecl:
    """Option-free source-to-sink chain through typed transform instances."""

    endpoints: tuple[str, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


class ArbitrationPolicy(str, Enum):
    FIXED_PRIORITY = "fixed_priority"
    ROUND_ROBIN = "round_robin"


class GrantScope(str, Enum):
    BEAT = "beat"
    PACKET = "packet"


@dataclass(frozen=True)
class ArbiterDecl:
    sources: tuple[str, ...]
    destination: str
    policy: ArbitrationPolicy
    grant_scope: GrantScope


class ContractKind(str, Enum):
    ASSUME = "assume"
    GUARANTEE = "guarantee"


class VerificationGoalKind(str, Enum):
    """User-facing first-class verification goal category."""

    ASSERT = "assert"
    ENSURE = "ensure"
    COVER = "cover"


class CsrAccess(str, Enum):
    READ_WRITE = "rw"
    READ_ONLY = "ro"
    WRITE_ONLY = "wo"
    WRITE_ONE_TO_CLEAR = "w1c"
    PULSE = "pulse"
    RESERVED = "reserved"


class CsrBindingKind(str, Enum):
    STATUS = "status"
    STICKY = "sticky"
    COMMAND = "command"


class CsrPriority(str, Enum):
    HARDWARE = "hardware"
    SOFTWARE = "software"


@dataclass(frozen=True, kw_only=True)
class LocatedExpression:
    """Syntax expression carrying a parser-provided source span."""

    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class StructConstructExpr(LocatedExpression):
    struct_name: str
    fields: tuple[StructFieldValue, ...]


@dataclass(frozen=True)
class TaggedUnionConstructExpr(LocatedExpression):
    union_name: str
    variant: str
    fields: tuple[StructFieldValue, ...]


@dataclass(frozen=True)
class TaggedUnionMatchArm:
    union_name: str
    variant: str
    binders: tuple[str, ...]
    expression: "Expression"
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TaggedUnionMatchExpr(LocatedExpression):
    selector: "Expression"
    arms: tuple[TaggedUnionMatchArm, ...]


@dataclass(frozen=True)
class StructUpdateExpr(LocatedExpression):
    """Immutable update of one concrete nominal struct value."""

    expression: "Expression"
    fields: tuple[StructFieldValue, ...]


@dataclass(frozen=True)
class VectorLiteralExpr(LocatedExpression):
    """Source-order vector literal with exact element typing."""

    elements: tuple["Expression", ...]


@dataclass(frozen=True)
class CharLiteralExpr(LocatedExpression):
    """One exact source byte, later erased to an ordinary ``u8`` constant."""

    value: int


@dataclass(frozen=True)
class StringLiteralExpr(LocatedExpression):
    """A fixed source byte sequence, later erased to ``vec<N,u8>``."""

    values: tuple[int, ...]


@dataclass(frozen=True)
class TupleLiteralExpr(LocatedExpression):
    elements: tuple["Expression", ...]

    def __post_init__(self) -> None:
        if not 2 <= len(self.elements) <= 8:
            raise ValueError("a tuple literal requires between 2 and 8 elements")


@dataclass(frozen=True)
class CsrBinding:
    kind: CsrBindingKind
    signal: str
    priority: CsrPriority | None = None


@dataclass(frozen=True)
class CsrFieldDecl:
    name: str
    type_name: TypeSyntax
    access: CsrAccess
    msb: int | None = None
    lsb: int | None = None
    reset: int | None = None
    binding: CsrBinding | None = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CsrRegisterDecl:
    name: str
    offset: int
    fields: tuple[CsrFieldDecl, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CsrBlockDecl:
    name: str
    base_address: int
    registers: tuple[CsrRegisterDecl, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class NameExpr(LocatedExpression):
    name: str


@dataclass(frozen=True)
class TypeValueExpr(LocatedExpression):
    """A canonical type value used only in specialization-time conditions."""

    type_name: TypeSyntax


class PatternConstantKind(str, Enum):
    ZERO = "zero"
    ZEROS = "zeros"
    ONES = "ones"


@dataclass(frozen=True)
class PatternConstantExpr(LocatedExpression):
    kind: PatternConstantKind
    witness: str


@dataclass(frozen=True)
class EquivGuard:
    predicates: tuple[str, ...]


@dataclass(frozen=True)
class EquivDecl:
    name: str
    left: Expression
    right: Expression
    guard: EquivGuard | None = None


@dataclass(frozen=True)
class AddExpr(LocatedExpression):
    left: Expression
    right: Expression


class BinaryOperator(str, Enum):
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"
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
    LOGIC_AND = "&&"
    LOGIC_OR = "||"
    LOGIC_NOT = "!"
    BIT_NOT = "~"


@dataclass(frozen=True)
class BinaryExpr(LocatedExpression):
    operator: BinaryOperator
    left: Expression
    right: Expression


@dataclass(frozen=True)
class UnaryExpr(LocatedExpression):
    operator: BinaryOperator
    expression: Expression


@dataclass(frozen=True)
class CompileTimeIfExpr(LocatedExpression):
    """Specialization-time branch, removed before typed hardware IR."""

    condition: Expression
    when_true: Expression
    when_false: Expression | None = None


@dataclass(frozen=True)
class NumberExpr(LocatedExpression):
    value: int


@dataclass(frozen=True)
class RationalExpr(LocatedExpression):
    numerator: int
    denominator: int


class FixedRoundingMode(str, Enum):
    TOWARD_ZERO = "toward_zero"
    FLOOR = "floor"
    AWAY_ZERO = "away_zero"
    NEAREST_EVEN = "nearest_even"


class FixedOverflowMode(str, Enum):
    WRAP = "wrap"
    SATURATE = "saturate"


@dataclass(frozen=True)
class QuantizeExpr(LocatedExpression):
    expression: Expression
    rounding: FixedRoundingMode
    target_type: TypeSyntax | None = None
    overflow: FixedOverflowMode | None = None


class ResizeKind(str, Enum):
    TRUNCATE = "truncate"
    EXTEND = "extend"


@dataclass(frozen=True)
class ResizeExpr(LocatedExpression):
    kind: ResizeKind
    # ``None`` is the concise contextual spelling ``truncate(expr)`` or
    # ``extend(expr)``. Semantic analysis resolves it from an explicit typed
    # boundary before backend-independent IR is constructed.
    width: int | str | None
    expression: Expression


@dataclass(frozen=True)
class MuxExpr(LocatedExpression):
    condition: Expression
    when_true: Expression
    when_false: Expression


@dataclass(frozen=True)
class EnumMemberRef:
    enum_name: str
    member: str


@dataclass(frozen=True)
class SwitchArm:
    key: int | EnumMemberRef
    expression: Expression


@dataclass(frozen=True)
class SwitchExpr(LocatedExpression):
    selector: Expression
    arms: tuple[SwitchArm, ...]
    default: Expression | None


@dataclass(frozen=True)
class CallExpr(LocatedExpression):
    function: str
    arguments: tuple[Expression, ...]
    specializations: tuple[SpecializationArgument, ...] = ()


@dataclass(frozen=True)
class FieldExpr(LocatedExpression):
    expression: Expression
    field: str


@dataclass(frozen=True)
class IndexExpr(LocatedExpression):
    expression: Expression
    index: int | Expression


@dataclass(frozen=True)
class SliceExpr(LocatedExpression):
    """Inclusive, compile-time bit slice ``expression[msb:lsb]``."""

    expression: Expression
    msb: int | str
    lsb: int | str


@dataclass(frozen=True)
class VectorRangeExpr(LocatedExpression):
    """Half-open compile-time vector range ``expression[start..stop]``."""

    expression: Expression
    start: int | str
    stop: int | str


@dataclass(frozen=True)
class ConcatExpr(LocatedExpression):
    """Source-order bit or homogeneous-vector concatenation."""

    arguments: tuple[Expression, ...]


@dataclass(frozen=True)
class BitcastExpr(LocatedExpression):
    """Exact-width reinterpretation of a recursively bit-packable value."""

    target_type: TypeSyntax
    expression: Expression


@dataclass(frozen=True)
class ReshapeExpr(LocatedExpression):
    """Compile-time vector shape change preserving outer-to-inner leaf order."""

    target_type: TypeSyntax | None
    expression: Expression


@dataclass(frozen=True)
class PackExpr(LocatedExpression):
    expression: Expression


@dataclass(frozen=True)
class UnpackExpr(LocatedExpression):
    target_type: TypeSyntax
    expression: Expression


@dataclass(frozen=True)
class GenerateExpr(LocatedExpression):
    index: str
    start: int | str
    stop: int | str
    expression: Expression


@dataclass(frozen=True)
class MapExpr(LocatedExpression):
    index: str
    start: int | str
    stop: int | str
    expression: Expression


class ReductionOperator(str, Enum):
    ADD = "+"
    MULTIPLY = "*"
    BIT_AND = "&"
    BIT_OR = "|"
    BIT_XOR = "^"


@dataclass(frozen=True)
class ReduceExpr(LocatedExpression):
    operator: ReductionOperator
    collection: Expression


@dataclass(frozen=True)
class IndexedSumExpr(LocatedExpression):
    index: str
    start: int | str
    stop: int | str
    expression: Expression


@dataclass(frozen=True)
class CollectionSumExpr(LocatedExpression):
    collection: Expression


@dataclass(frozen=True)
class DotExpr(LocatedExpression):
    left: Expression
    right: Expression
    rounding: FixedRoundingMode | None = None


@dataclass(frozen=True)
class DelayExpr(LocatedExpression):
    cycles: int
    expression: Expression


class PipelineMetric(str, Enum):
    LATENCY = "latency"
    THROUGHPUT = "throughput"
    INITIATION_INTERVAL = "ii"
    DSP = "dsp"
    FMAX = "fmax"


class PipelineRelation(str, Enum):
    MAXIMUM = "<="
    EXACT = "=="
    MINIMUM = ">="


@dataclass(frozen=True)
class PipelineConstraint:
    metric: PipelineMetric
    relation: PipelineRelation
    value: int


@dataclass(frozen=True)
class PipelineExpr(LocatedExpression):
    stages: int | None
    expression: Expression
    constraints: tuple[PipelineConstraint, ...] = ()


class ArchitectureMetric(str, Enum):
    PARALLELISM = "parallelism"
    DEPTH = "depth"
    CANDIDATES = "candidates"


@dataclass(frozen=True)
class ArchitectureConstraint:
    metric: ArchitectureMetric
    maximum: int


@dataclass(frozen=True)
class ArchitectureExpr(LocatedExpression):
    expression: Expression
    constraints: tuple[ArchitectureConstraint, ...] = ()


class ImplementationKind(str, Enum):
    MUL_ADD = "mul_add"
    DSP_MAC = "dsp_mac"


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


class ExplorationFamily(str, Enum):
    PIPELINE = "pipeline"
    DSP = "dsp"
    REDUCTION = "reduction"
    REASSOCIATE = "reassociate"
    ADAPTER = "adapter"


class ExplorationRelation(str, Enum):
    MAXIMUM = "<="
    MINIMUM = ">="
    EXACT = "=="


@dataclass(frozen=True)
class ExplorationConstraint:
    metric: CostMetric
    relation: ExplorationRelation
    value: int


@dataclass(frozen=True)
class ExplorationObjective:
    direction: str
    metric: CostMetric


@dataclass(frozen=True)
class ExploreExpr(LocatedExpression):
    expression: Expression
    allowed: tuple[ExplorationFamily, ...] = ()
    avoided: tuple[ExplorationFamily, ...] = ()
    constraints: tuple[ExplorationConstraint, ...] = ()
    objective: ExplorationObjective | None = None


@dataclass(frozen=True)
class ImplementationArm:
    kind: ImplementationKind
    expression: Expression


@dataclass(frozen=True)
class ImplementationChoiceExpr(LocatedExpression):
    selected: ImplementationKind | None
    alternatives: tuple[ImplementationArm, ...]
    cost_policy: CostPolicy | None = None


Expression = (
    NameExpr
    | TypeValueExpr
    | AddExpr
    | BinaryExpr
    | UnaryExpr
    | CompileTimeIfExpr
    | NumberExpr
    | RationalExpr
    | PatternConstantExpr
    | QuantizeExpr
    | ResizeExpr
    | MuxExpr
    | SwitchExpr
    | CallExpr
    | FieldExpr
    | IndexExpr
    | SliceExpr
    | VectorRangeExpr
    | ConcatExpr
    | BitcastExpr
    | ReshapeExpr
    | PackExpr
    | UnpackExpr
    | GenerateExpr
    | MapExpr
    | ReduceExpr
    | IndexedSumExpr
    | CollectionSumExpr
    | DotExpr
    | DelayExpr
    | PipelineExpr
    | ArchitectureExpr
    | ImplementationChoiceExpr
    | ExploreExpr
    | StructConstructExpr
    | TaggedUnionConstructExpr
    | TaggedUnionMatchExpr
    | StructUpdateExpr
    | VectorLiteralExpr
    | CharLiteralExpr
    | StringLiteralExpr
    | TupleLiteralExpr
)


@dataclass(frozen=True)
class ContractDecl:
    kind: ContractKind
    name: str
    clock: str
    reset: str
    expression: Expression


@dataclass(frozen=True)
class VerificationRequirementDecl:
    """One named environment requirement local to a verification scope."""

    name: str
    expression: Expression
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class VerificationGoalDecl:
    """One named same-cycle assertion/ensure goal or reachability cover."""

    kind: VerificationGoalKind
    name: str
    expression: Expression
    clock: str | None = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class VerificationScopeDecl:
    """A named group of requirements and verification goals."""

    name: str
    requirements: tuple[VerificationRequirementDecl, ...]
    goals: tuple[VerificationGoalDecl, ...]
    clock: str | None = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FunctionDecl:
    name: str
    parameters: tuple[Parameter, ...]
    return_type: TypeSyntax | None
    body: Expression
    generic_parameters: tuple[ModuleParameter, ...] = ()
    # Concise function-local bindings are immutable inferred aliases.  They
    # are elaborated into the existing typed expression body and therefore do
    # not introduce a runtime/local-storage IR entity.
    bindings: tuple[Assignment | TupleDestructureDecl, ...] = ()
    origin: SourceSpan | None = field(default=None, compare=False)
    source_identity: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class OperatorDecl:
    operator: str
    parameters: tuple[Parameter, ...]
    return_type: TypeSyntax | None
    body: Expression
    generic_parameters: tuple[ModuleParameter, ...] = ()
    bindings: tuple[Assignment | TupleDestructureDecl, ...] = ()
    origin: SourceSpan | None = field(default=None, compare=False)
    source_identity: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Assignment:
    target: str
    expression: Expression
    type_name: TypeSyntax | None = None
    origin: SourceSpan | None = field(default=None, compare=False)
    # Syntax-only validation marker for the hidden value retained by flat
    # tuple destructuring.  It is erased before typed IR construction.
    tuple_destructure_arity: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CompileTimeIfDecl:
    """Module-item specialization branch.

    The parser retains both branches, while semantic elaboration selects one
    branch before ordinary module validation and IR construction.
    """

    condition: Expression
    when_true: tuple[object, ...]
    when_false: tuple[object, ...] = ()
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class GenerateBlock:
    """Parameterized structural generation block retained at the AST edge."""

    index: str
    start: int | str
    stop: int | str
    items: tuple[object, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class RegisterDecl:
    name: str
    type_name: TypeSyntax
    initial: Expression
    domain: str | None = None


@dataclass(frozen=True)
class NextAssignment:
    target: str | "IndexedAssignmentTarget"
    expression: Expression


@dataclass(frozen=True)
class IndexedAssignmentTarget:
    """One-dimensional element target used only by atomic rule actions."""

    register: str
    index: Expression
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ResourceAction:
    resource: str
    operation: str
    operands: tuple[Expression, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ConditionalAction:
    """Runtime action selection retained only until rule normalization.

    Nested ``when`` statements do not introduce a second scheduling model or
    split one source rule into leaf rules.  Semantic lowering keeps one atomic
    rule and attaches the selected path predicate to each typed effect.
    ``None`` distinguishes an omitted ``else`` from an explicit empty else
    block for precise source diagnostics.
    """

    guard: Expression
    when_true: tuple[NextAssignment | ResourceAction | "ConditionalAction", ...]
    when_false: (
        tuple[NextAssignment | ResourceAction | "ConditionalAction", ...] | None
    ) = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class RuleDecl:
    name: str
    guard: Expression
    actions: tuple[NextAssignment | ResourceAction | ConditionalAction, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AnonymousRuleDecl:
    guard: Expression
    actions: tuple[NextAssignment | ResourceAction | ConditionalAction, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class PriorityRuleArm:
    """One source-ordered arm of a concise ``priority`` block."""

    label: str | None
    guard: Expression | None
    actions: tuple[NextAssignment | ResourceAction | ConditionalAction, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class PriorityBlockDecl:
    """Concise ordered rule block retained until compile-time selection ends."""

    arms: tuple[PriorityRuleArm | "PriorityBlockDecl", ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FsmTransitionDecl:
    """One guarded or unconditional transition in concise FSM syntax."""

    target: str
    actions: tuple[NextAssignment | ResourceAction | ConditionalAction, ...]
    guard: Expression | None = None
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FsmStateDecl:
    """One exhaustive enum-member body in a concise FSM declaration."""

    member: str
    transitions: tuple[FsmTransitionDecl, ...] = ()
    priority: bool = False
    hold: bool = False
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class FsmDecl:
    """Syntax-only FSM normalized to an enum register and ordinary rules."""

    name: str
    type_name: TypeSyntax
    initial_member: str
    states: tuple[FsmStateDecl, ...]
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class RulePriority:
    higher: str
    lower: str


@dataclass(frozen=True)
class RulePriorityChain:
    """Syntax-only priority chain lowered to adjacent RulePriority edges."""

    names: tuple[str, ...]


@dataclass(frozen=True)
class FifoDecl:
    name: str
    element_type: TypeSyntax
    # Literal spellings remain integers for source compatibility.  A string is
    # an unevaluated compile-time value expression and must be resolved during
    # module specialization before semantic storage IR is constructed.
    depth: int | str
    origin: SourceSpan | None = field(default=None, compare=False)


class MemoryCollision(str, Enum):
    READ_FIRST = "read_first"
    WRITE_FIRST = "write_first"


class MemoryResetPolicy(str, Enum):
    """Source policy for one independently resettable memory state surface."""

    CLEAR = "clear"
    PRESERVE = "preserve"


@dataclass(frozen=True)
class MemoryDecl:
    name: str
    element_type: TypeSyntax
    depth: int | str
    read_latency: int
    collision: MemoryCollision
    origin: SourceSpan | None = field(default=None, compare=False)
    # Trailing defaults preserve the positional constructor used before reset
    # policy became source-visible.
    contents_reset: MemoryResetPolicy = MemoryResetPolicy.CLEAR
    read_data_reset: MemoryResetPolicy = MemoryResetPolicy.CLEAR


@dataclass(frozen=True)
class RomDecl:
    """One immutable, synchronously-read, compile-time initialized ROM."""

    name: str
    element_type: TypeSyntax
    depth: int | str
    read_latency: int
    initializer: Expression
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ModuleTimingDecl:
    """One exact public timing contract written on a source module."""

    latency: int
    initiation_interval: int
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ClockPhysicalDecl:
    """Source spelling of one clock's physical edge contract."""

    name: str
    edge: str = "rising"
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ResetPhysicalDecl:
    """Source spelling of one reset's physical contract."""

    name: str
    clock: str | None = None
    mode: str = "synchronous"
    polarity: str = "active_high"
    power_up: str = "unspecified"
    origin: SourceSpan | None = field(default=None, compare=False)
    release_mode: str = "native"
    release_cycles: int = 0


@dataclass(frozen=True)
class ModuleInterfaceDecl:
    """A reusable, behavior-free public module signature declaration."""

    name: str
    parameters: tuple[ModuleParameter, ...] = ()
    ports: tuple[PortDecl, ...] = ()
    clocks: tuple[str, ...] = ()
    resets: tuple[str, ...] = ()
    reset_domains: tuple[tuple[str, str | None], ...] = ()
    clock_physical: tuple[ClockPhysicalDecl, ...] = ()
    reset_physical: tuple[ResetPhysicalDecl, ...] = ()
    request_responses: tuple[RequestResponseDecl, ...] = ()
    aggregate_interfaces: tuple[AggregateInterfaceDecl, ...] = ()
    timing: ModuleTimingDecl | None = None
    source_identity: str | None = field(default=None, compare=False)
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ModuleInterfaceRef:
    """One explicit source choice of a named public interface."""

    name: str
    arguments: tuple[SpecializationArgument, ...] = ()
    origin: SourceSpan | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Module:
    name: str
    ports: tuple[PortDecl, ...]
    assignments: tuple[Assignment, ...]
    type_aliases: tuple[TypeAlias, ...] = ()
    enums: tuple[EnumDecl, ...] = ()
    structs: tuple[StructDecl, ...] = ()
    functions: tuple[FunctionDecl, ...] = ()
    operators: tuple[OperatorDecl, ...] = ()
    clocks: tuple[str, ...] = ()
    resets: tuple[str, ...] = ()
    reset_domains: tuple[tuple[str, str | None], ...] = ()
    clock_physical: tuple[ClockPhysicalDecl, ...] = ()
    reset_physical: tuple[ResetPhysicalDecl, ...] = ()
    registers: tuple[RegisterDecl, ...] = ()
    next_assignments: tuple[NextAssignment, ...] = ()
    request_responses: tuple[RequestResponseDecl, ...] = ()
    connections: tuple[ConnectionDecl, ...] = ()
    connection_chains: tuple[ConnectionChainDecl, ...] = ()
    csr_blocks: tuple[CsrBlockDecl, ...] = ()
    rules: tuple[RuleDecl | AnonymousRuleDecl, ...] = ()
    rule_priorities: tuple[RulePriority, ...] = ()
    fsms: tuple[FsmDecl, ...] = ()
    fifos: tuple[FifoDecl, ...] = ()
    memories: tuple[MemoryDecl, ...] = ()
    roms: tuple[RomDecl, ...] = ()
    arbiters: tuple[ArbiterDecl, ...] = ()
    contracts: tuple[ContractDecl, ...] = ()
    verification_goals: tuple[VerificationGoalDecl, ...] = ()
    verification_scopes: tuple[VerificationScopeDecl, ...] = ()
    equivalences: tuple[EquivDecl, ...] = ()
    parameters: tuple[ModuleParameter, ...] = ()
    instances: tuple[InstanceDecl, ...] = ()
    aggregate_interfaces: tuple[AggregateInterfaceDecl, ...] = ()
    generic_declarations: tuple[GenericDeclaration, ...] = ()
    # Compiler-shipped library modules carry a stable logical identity and
    # content hash.  Ordinary source modules leave these unset.
    source_identity: str | None = None
    source_hash: str | None = None
    submodules: tuple["Module", ...] = ()
    imports: tuple[ImportDecl, ...] = ()
    protocols: tuple[ProtocolDecl, ...] = ()
    resource_definitions: tuple[ResourceDefinitionDecl, ...] = ()
    target_families: tuple[TargetFamilyDecl, ...] = ()
    target_instances: tuple[TargetInstanceDecl, ...] = ()
    architecture_templates: tuple[ArchitectureTemplateDecl, ...] = ()
    compile_time_ifs: tuple["CompileTimeIfDecl", ...] = ()
    generate_blocks: tuple["GenerateBlock", ...] = ()
    # Module declarations retain their source order at the syntax boundary.
    # Category-specific tuples above remain the compatibility view consumed by
    # existing semantic passes; ordered_items is used when elaboration must
    # splice compile-time branches without changing dependency order.
    ordered_items: tuple[object, ...] = ()
    timing: ModuleTimingDecl | None = None
    module_interfaces: tuple[ModuleInterfaceDecl, ...] = ()
    conforms_to: ModuleInterfaceRef | None = None
    # The immutable public parameter declaration survives child specialization;
    # ``parameters`` may carry concrete effective values during elaboration.
    declared_parameters: tuple[ModuleParameter, ...] = ()
    # Present only for the bounded ``extern module`` declaration.  The named
    # function is the complete backend-independent semantic model.
    external_model: str | None = None
    external_origin: SourceSpan | None = field(default=None, compare=False)
    # Appended to preserve the historical positional constructor ABI.
    tagged_unions: tuple[TaggedUnionDecl, ...] = ()
    # Declaration-only source units use one syntax carrier because the legacy
    # parser API returns Module.  Import merging consumes its declarations but
    # never publishes the carrier as an elaborated child.
    declaration_only: bool = False
    # One bounded compile-time legality condition over exact type/value module
    # parameters.  It is discharged during specialization and never reaches a
    # backend as hardware logic.
    parameter_constraint: Expression | None = None
