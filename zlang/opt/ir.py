"""Canonical, typed optimization IR.

The semantic IR remains the source of language meaning. This layer gives later
optimization passes a deterministic DAG with value, state, protocol,
transaction, and architecture categories without depending on a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum

from zlang.ir.arbitration import PacketArbiter
from zlang.ir.cdc import ClockDomain
from zlang.ir.csr import CsrBlock
from zlang.ir.interfaces import (
    InterfaceSignal,
    RequestResponseChannel,
)
from zlang.ir.expressions import (
    ImplementationCostEstimate,
    ImplementationKind,
    YosysMeasurement,
)
from zlang.ir.module import (
    Connection,
    FunctionParameter,
    ModuleSignature,
    Port,
    RequestResponseInterface,
    RulePriority,
    EquivalenceRule,
    validate_elastic_module_regions,
)
from zlang.ir.callables import CallableMetadata, stable_callee_identity
from zlang.ir.functional_regions import (
    CompileTimeBinderRef,
    ExactReductionPlan,
    FunctionalRegionKind,
)
from zlang.ir.storage import MemoryCollision
from zlang.ir.state import StateActionKind, StateResource
from zlang.source import SourceOrigin
from zlang.ir.pipelines import (
    MultiplierMapping,
    PipelineConstraint,
    PipelineCostSource,
    PipelineEstimate,
    PipelineTree,
    RegisterPlacement,
    PipelinePlan,
)
from zlang.ir.elastic import (
    ElasticPipelinePlan,
    ElasticStallPolicy,
    ElasticTimingContract,
    validate_elastic_region_metadata,
)
from zlang.ir.architectures import (
    ArchitectureConstraint,
    ArchitectureEquivalence,
    FirArchitectureKind,
)
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    HardwareType,
    SIntType,
    StructType,
    TaggedUnionType,
    UFixedType,
    UIntType,
    VecType,
)
from zlang.ir.verification import ContractKind, VerificationGoalKind
from zlang.ir.timing import (
    InstanceOutputTiming,
    ModuleTimingContract,
    OutputTiming,
)
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.source import SourceOrigin


NodeId = int
AttributeValue = object


class NodeCategory(str, Enum):
    VALUE = "value"
    STATE = "state"
    PROTOCOL = "protocol"
    TRANSACTION = "transaction"
    ARCHITECTURE = "architecture"

    # Internal compatibility aliases for Milestone 17-24 code. New reports and
    # iteration use the five ZLang 0.2 category names above.
    SEQUENTIAL = "state"
    ARCHITECTURAL = "architecture"


class OptimizationStage(str, Enum):
    HIGH_LEVEL = "high_level"
    SELECTED_ARCHITECTURE = "selected_architecture"


class Signedness(str, Enum):
    CONTROL = "control"
    UNSIGNED = "unsigned"
    SIGNED = "signed"
    RAW_BITS = "raw_bits"
    AGGREGATE = "aggregate"


class Purity(str, Enum):
    PURE = "pure"
    OBSERVATIONAL = "observational"
    ARCHITECTURAL = "architectural"


class EffectKind(str, Enum):
    READ_STATE = "read_state"
    OBSERVE_PROTOCOL = "observe_protocol"
    OBSERVE_TRANSACTION = "observe_transaction"
    TIME_SHIFT = "time_shift"
    SELECT_ARCHITECTURE = "select_architecture"


@dataclass(frozen=True)
class NodeMetadata:
    """Derived facts required by optimization legality checks."""

    width: int
    signedness: Signedness
    latency: int
    initiation_interval: int
    domains: tuple[str, ...]
    purity: Purity
    effects: tuple[EffectKind, ...]

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("canonical node width must be positive")
        if self.latency < 0:
            raise ValueError("canonical node latency must not be negative")
        if self.initiation_interval < 1:
            raise ValueError("canonical node initiation interval must be positive")
        if len(self.domains) != len(set(self.domains)):
            raise ValueError("canonical node domains must be unique")
        if len(self.effects) != len(set(self.effects)):
            raise ValueError("canonical node effects must be unique")
        if self.purity is Purity.PURE and self.effects:
            raise ValueError("a pure canonical node cannot carry effects")
        if self.purity is not Purity.PURE and not self.effects:
            raise ValueError("a non-pure canonical node requires an effect")


def pure_metadata(type_: HardwareType) -> NodeMetadata:
    """Construct metadata for a zero-latency pure combinational node."""

    if isinstance(type_, BitType):
        signedness = Signedness.CONTROL
    elif isinstance(type_, UIntType):
        signedness = Signedness.UNSIGNED
    elif isinstance(type_, SIntType):
        signedness = Signedness.SIGNED
    elif isinstance(type_, FixedType):
        signedness = Signedness.SIGNED
    elif isinstance(type_, UFixedType):
        signedness = Signedness.UNSIGNED
    elif isinstance(type_, (BitsType, EnumType)):
        signedness = Signedness.RAW_BITS
    else:
        signedness = Signedness.AGGREGATE
    return NodeMetadata(
        width=type_.width,
        signedness=signedness,
        latency=0,
        initiation_interval=1,
        domains=(),
        purity=Purity.PURE,
        effects=(),
    )


class ExpressionOp(str, Enum):
    INPUT = "input"
    PARAMETER = "parameter"
    REGISTER_REF = "register_ref"
    READY_VALID_REF = "ready_valid_ref"
    CREDIT_REF = "credit_ref"
    PACKET_REF = "packet_ref"
    VC_CREDIT_REF = "vc_credit_ref"
    REQUEST_RESPONSE_REF = "request_response_ref"
    FIFO_REF = "fifo_ref"
    MEMORY_REF = "memory_ref"
    ROM_REF = "rom_ref"
    CONSTANT = "constant"
    ENUM_ENCODE = "enum_encode"
    ENUM_VALID = "enum_valid"
    ENUM_DECODE = "enum_decode"
    UNION_CONSTRUCT = "union_construct"
    UNION_TAG = "union_tag"
    UNION_FIELD = "union_field"
    ADD = "add"
    BINARY = "binary"
    EXTEND = "extend"
    TRUNCATE = "truncate"
    FIXED_CONVERT = "fixed_convert"
    MUX = "mux"
    SWITCH = "switch"
    CALL = "call"
    FIELD = "field"
    STRUCT_CONSTRUCT = "struct_construct"
    TUPLE_CONSTRUCT = "tuple_construct"
    TUPLE_PROJECT = "tuple_project"
    FUNCTIONAL_CAPTURE = "functional_capture"
    FUNCTIONAL_TABLE_LOOKUP = "functional_table_lookup"
    FUNCTIONAL_REGION = "functional_region"
    INSTANCE_OUTPUT = "instance_output"
    VECTOR_INDEX = "vector_index"
    RUNTIME_INDEX = "runtime_index"
    VECTOR_UPDATE = "vector_update"
    SLICE = "slice"
    CONCAT = "concat"
    BITCAST = "bitcast"
    VECTOR_CONCAT = "vector_concat"
    RESHAPE = "reshape"
    PACK = "pack"
    UNPACK = "unpack"
    GENERATE = "generate"
    MAP = "map"
    DOT = "dot"
    REDUCE = "reduce"
    DELAY = "delay"
    PIPELINE = "pipeline"
    IMPLEMENTATION_CHOICE = "implementation_choice"


class TargetKind(str, Enum):
    PORT = "port"
    REQUEST_RESPONSE = "request_response"
    REGISTER = "register"


class EquivalenceMode(str, Enum):
    MATHEMATICAL = "mathematical"
    CYCLE_ACCURATE = "cycle_accurate"
    OBSERVATIONAL = "observational"


class Observation(str, Enum):
    TYPED_VALUE = "typed_value"
    CLOCK_EDGE = "clock_edge"
    RESET = "reset"
    PORT = "port"
    PROTOCOL_EVENT = "protocol_event"


@dataclass(frozen=True)
class EquivalenceDefinition:
    mode: EquivalenceMode
    observations: tuple[Observation, ...]
    permits_internal_architecture_change: bool


_EQUIVALENCE_DEFINITIONS = {
    EquivalenceMode.MATHEMATICAL: EquivalenceDefinition(
        EquivalenceMode.MATHEMATICAL,
        (Observation.TYPED_VALUE,),
        False,
    ),
    EquivalenceMode.CYCLE_ACCURATE: EquivalenceDefinition(
        EquivalenceMode.CYCLE_ACCURATE,
        (
            Observation.TYPED_VALUE,
            Observation.CLOCK_EDGE,
            Observation.RESET,
        ),
        False,
    ),
    EquivalenceMode.OBSERVATIONAL: EquivalenceDefinition(
        EquivalenceMode.OBSERVATIONAL,
        (
            Observation.PORT,
            Observation.PROTOCOL_EVENT,
            Observation.CLOCK_EDGE,
            Observation.RESET,
        ),
        True,
    ),
}


def equivalence_definition(mode: EquivalenceMode) -> EquivalenceDefinition:
    return _EQUIVALENCE_DEFINITIONS[mode]


@dataclass(frozen=True)
class CanonicalExpression:
    id: NodeId
    category: NodeCategory
    op: ExpressionOp
    type: HardwareType
    metadata: NodeMetadata
    operands: tuple[NodeId, ...] = ()
    attributes: tuple[tuple[str, AttributeValue], ...] = ()
    origins: tuple[SourceOrigin, ...] = ()

    def __post_init__(self) -> None:
        if self.metadata.width != self.type.width:
            raise ValueError(
                f"canonical expression %{self.id} metadata width "
                f"{self.metadata.width} does not match type width {self.type.width}"
            )
        if len(self.origins) != len(set(self.origins)):
            raise ValueError(
                f"canonical expression %{self.id} source origins must be unique"
            )

    def attribute(self, name: str) -> AttributeValue:
        for key, value in self.attributes:
            if key == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True)
class CanonicalEntity:
    id: str
    category: NodeCategory
    kind: str
    name: str
    roots: tuple[NodeId, ...] = ()
    details: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CanonicalAssignment:
    target_kind: TargetKind
    target_name: str
    expression: NodeId
    signal: InterfaceSignal | None = None
    channel: RequestResponseChannel | None = None


@dataclass(frozen=True)
class CanonicalFunction:
    name: str
    parameters: tuple[FunctionParameter, ...]
    return_type: HardwareType
    body: NodeId
    callee_identity: str = ""
    metadata: CallableMetadata | None = None

    def __post_init__(self) -> None:
        expected_identity = stable_callee_identity(
            self.name, self.parameters, self.return_type, self.metadata
        )
        if self.callee_identity and self.callee_identity != expected_identity:
            raise ValueError(
                f"canonical function '{self.name}' callee identity does not match"
            )
        if not self.callee_identity:
            object.__setattr__(self, "callee_identity", expected_identity)


@dataclass(frozen=True)
class CanonicalExternalModuleContract:
    """Canonical copy of one semantic model-backed external contract."""

    logical_name: str
    signature: ModuleSignature
    model_callee_identity: str
    semantic_identity: str
    source_origin: SourceOrigin | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CanonicalRegister:
    name: str
    type: HardwareType
    initial: NodeId
    domain: str | None


@dataclass(frozen=True)
class CanonicalNextAssignment:
    target_kind: TargetKind
    target_name: str
    expression: NodeId


@dataclass(frozen=True)
class CanonicalRule:
    name: str
    guard: NodeId
    actions: tuple[CanonicalNextAssignment, ...]


@dataclass(frozen=True)
class CanonicalFifo:
    name: str
    element_type: HardwareType
    depth: int
    data: NodeId | None
    push: NodeId | None
    pop: NodeId | None
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class CanonicalStateAction:
    semantic_id: str
    resource_id: str
    kind: StateActionKind
    operands: tuple[NodeId, ...]
    owner_group: str
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class CanonicalActionGroup:
    semantic_id: str
    rule_name: str
    guard: NodeId
    actions: tuple[CanonicalStateAction, ...]
    source_origin: SourceOrigin | None = None


@dataclass(frozen=True)
class CanonicalResolvedTransition:
    semantic_id: str
    domain: str | None
    reset: str | None
    resources: tuple[StateResource, ...]
    action_groups: tuple[CanonicalActionGroup, ...]
    priorities: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CanonicalMemory:
    name: str
    semantic_id: str
    element_type: HardwareType
    depth: int
    read_latency: int
    collision: MemoryCollision
    read_address: NodeId | None
    write_enable: NodeId | None
    write_address: NodeId | None
    write_data: NodeId | None
    source_origin: SourceOrigin | None = None
    write_mask_width: int | None = None
    write_mask: NodeId | None = None


@dataclass(frozen=True)
class CanonicalRom:
    name: str
    semantic_id: str
    element_type: HardwareType
    depth: int
    address_type: HardwareType
    read_latency: int
    contents: tuple[NodeId, ...]
    read_address: NodeId
    initialization_identity: str
    dependency_identity: tuple[tuple[str, str], ...]
    evaluator_schema: str
    content_hash: str
    source_origin: SourceOrigin | None = None

    def __post_init__(self) -> None:
        if not self.semantic_id:
            raise ValueError("canonical ROM semantic identity must not be empty")
        if self.depth < 1:
            raise ValueError("canonical ROM depth must be positive")
        if self.read_latency != 1:
            raise ValueError("canonical ROM read latency must be exactly one")
        if len(self.contents) != self.depth:
            raise ValueError("canonical ROM contents must match its depth")


@dataclass(frozen=True)
class CanonicalContract:
    kind: ContractKind
    name: str
    clock: str
    reset: str
    expression: NodeId


@dataclass(frozen=True)
class CanonicalVerificationRequirement:
    semantic_id: str
    name: str
    expression: NodeId
    source_origin: SourceOrigin | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CanonicalVerificationGoal:
    semantic_id: str
    scope_id: str
    kind: VerificationGoalKind
    name: str
    expression: NodeId
    source_origin: SourceOrigin | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CanonicalVerificationScope:
    semantic_id: str
    name: str
    clock: str
    reset: str
    requirements: tuple[CanonicalVerificationRequirement, ...]
    goals: tuple[CanonicalVerificationGoal, ...]
    source_origin: SourceOrigin | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.semantic_id or not self.name or not self.clock or not self.reset:
            raise ValueError("canonical verification scope metadata is incomplete")
        if not self.requirements and not self.goals:
            raise ValueError("canonical verification scope must not be empty")
        if any(item.scope_id != self.semantic_id for item in self.goals):
            raise ValueError("canonical verification goal references the wrong scope")
        if len({item.name for item in self.requirements}) != len(self.requirements):
            raise ValueError("canonical verification requirement names must be unique")
        if len({item.name for item in self.goals}) != len(self.goals):
            raise ValueError("canonical verification goal names must be unique")
        if {item.name for item in self.requirements} & {
            item.name for item in self.goals
        }:
            raise ValueError(
                "canonical verification clause names must be unique in a scope"
            )


@dataclass(frozen=True)
class CanonicalImplementationEvidence:
    """Cost evidence with estimates and tool measurements kept disjoint."""

    candidate: ImplementationKind
    estimate: ImplementationCostEstimate | None
    measurement: YosysMeasurement | None


@dataclass(frozen=True)
class CanonicalPipelineCandidate:
    name: str
    expression: NodeId
    tree: PipelineTree
    register_placement: RegisterPlacement
    multiplier_mapping: MultiplierMapping
    transformations: tuple[str, ...]
    latency: int
    initiation_interval: int
    estimate: PipelineEstimate
    cost_source: PipelineCostSource
    violations: tuple[str, ...]
    pipeline_plan: PipelinePlan = PipelinePlan((), 0)


@dataclass(frozen=True)
class CanonicalPipelineExploration:
    output: str
    result_type: HardwareType
    source_expression: NodeId
    constraints: tuple[PipelineConstraint, ...]
    candidates: tuple[CanonicalPipelineCandidate, ...]
    selected: str
    search_bound: int


@dataclass(frozen=True)
class CanonicalElasticPipelineRegion:
    semantic_id: str
    source_endpoint: str
    destination_endpoint: str
    input_type: HardwareType
    output_type: HardwareType
    source_expression: NodeId
    constraints: tuple[PipelineConstraint, ...]
    candidates: tuple[CanonicalPipelineCandidate, ...]
    selected: str
    plan: ElasticPipelinePlan
    timing: ElasticTimingContract
    clock: str
    reset: str
    source_origin: object | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        validate_elastic_region_metadata(self)


@dataclass(frozen=True)
class CanonicalArchitectureCandidate:
    name: str
    expression: NodeId
    kind: FirArchitectureKind
    parallelism: int
    add_depth: int
    multiplier_count: int
    adder_count: int
    transformations: tuple[str, ...]
    equivalence: ArchitectureEquivalence
    violations: tuple[str, ...]


@dataclass(frozen=True)
class CanonicalArchitectureExploration:
    output: str
    result_type: HardwareType
    source_expression: NodeId
    constraints: tuple[ArchitectureConstraint, ...]
    candidates: tuple[CanonicalArchitectureCandidate, ...]
    selected: str
    theoretical_candidates: int
    search_bound: int
    budget_pruned: int
    constraint_pruned: int


@dataclass(frozen=True)
class CanonicalModule:
    name: str
    stage: OptimizationStage
    expressions: tuple[CanonicalExpression, ...]
    entities: tuple[CanonicalEntity, ...]
    ports: tuple[Port, ...]
    assignments: tuple[CanonicalAssignment, ...]
    structs: tuple[StructType, ...] = ()
    enums: tuple[EnumType, ...] = ()
    functions: tuple[CanonicalFunction, ...] = ()
    clock: str | None = None
    reset: str | None = None
    registers: tuple[CanonicalRegister, ...] = ()
    next_assignments: tuple[CanonicalNextAssignment, ...] = ()
    request_responses: tuple[RequestResponseInterface, ...] = ()
    connections: tuple[Connection, ...] = ()
    csr_blocks: tuple[CsrBlock, ...] = ()
    csr_access: object | None = None
    rules: tuple[CanonicalRule, ...] = ()
    rule_priorities: tuple[RulePriority, ...] = ()
    fifos: tuple[CanonicalFifo, ...] = ()
    memories: tuple[CanonicalMemory, ...] = ()
    roms: tuple[CanonicalRom, ...] = ()
    clock_domains: tuple[ClockDomain, ...] = ()
    arbiters: tuple[PacketArbiter, ...] = ()
    contracts: tuple[CanonicalContract, ...] = ()
    pipeline_explorations: tuple[CanonicalPipelineExploration, ...] = ()
    architecture_explorations: tuple[
        CanonicalArchitectureExploration, ...
    ] = ()
    equivalences: tuple[EquivalenceRule, ...] = ()
    locals: tuple[object, ...] = ()
    instances: tuple[object, ...] = ()
    parameters: tuple[tuple[str, str, int | str | None], ...] = ()
    instance_bindings: tuple[object, ...] = ()
    children: tuple[object, ...] = ()
    elaborated_instances: tuple[object, ...] = ()
    protocol_endpoints: tuple[object, ...] = ()
    hierarchical_connections: tuple[object, ...] = ()
    request_response_connections: tuple[object, ...] = ()
    protocol_schemas: tuple[object, ...] = ()
    aggregate_protocol_endpoints: tuple[object, ...] = ()
    aggregate_protocol_connections: tuple[object, ...] = ()
    library_imports: tuple[str, ...] = ()
    library_dependencies: tuple[tuple[str, str], ...] = ()
    generic_specializations: tuple[object, ...] = ()
    resolved_transition: CanonicalResolvedTransition | None = None
    timing_contract: ModuleTimingContract | None = None
    output_timings: tuple[OutputTiming, ...] = ()
    instance_output_timings: tuple[InstanceOutputTiming, ...] = ()
    root_module_identity: DependencyModuleIdentity | None = None
    dependency_closure: DependencyClosure | None = None
    module_signature: object | None = None
    # Trailing default preserves the positional constructor shape used by the
    # pre-callable canonical module.
    callable_definitions: tuple[CanonicalFunction, ...] = ()
    external_contract: CanonicalExternalModuleContract | None = None
    # Appended to preserve the historical positional constructor ABI.
    tagged_unions: tuple[TaggedUnionType, ...] = ()
    elastic_pipeline_regions: tuple[CanonicalElasticPipelineRegion, ...] = ()
    specialization_bindings: tuple[object, ...] = ()
    # Stored losslessly, but excluded from hardware canonical identity.  The
    # overlay has its own verification identity and must not perturb M36/M38.
    verification_scopes: tuple[CanonicalVerificationScope, ...] = ()
    verification_expressions: tuple[CanonicalExpression, ...] = ()

    def __post_init__(self) -> None:
        from zlang.ir.module import (
            validate_generic_specialization,
            validate_specialization_bindings,
        )
        from zlang.ir.verification import validate_verification_overlay

        validate_specialization_bindings(self.specialization_bindings)
        validate_verification_overlay(
            self.verification_scopes,
            label="canonical verification",
        )
        for domain in self.clock_domains:
            domain.validate()
        for specialization in self.generic_specializations:
            validate_generic_specialization(specialization)
        validate_elastic_module_regions(
            self.elastic_pipeline_regions,
            self.ports,
            self.clock,
            self.reset,
            self.clock_domains,
        )
        declaration_names = tuple(item.name for item in self.tagged_unions)
        declaration_identities = tuple(
            item.declaration_identity for item in self.tagged_unions
        )
        if len(declaration_names) != len(set(declaration_names)):
            raise ValueError(
                "canonical tagged-union declaration names must be unique"
            )
        if len(declaration_identities) != len(set(declaration_identities)):
            raise ValueError(
                "canonical tagged-union declaration identities must be unique"
            )
        declared_unions = {
            item.declaration_identity: item for item in self.tagged_unions
        }

        def validate_union_types(value: object, seen: set[int]) -> None:
            if isinstance(value, TaggedUnionType):
                declared = declared_unions.get(value.declaration_identity)
                if declared is None or declared != value:
                    raise ValueError(
                        f"tagged-union type '{value.name}' is absent from the "
                        "exact canonical declaration table"
                    )
                return
            if isinstance(value, tuple):
                for item in value:
                    validate_union_types(item, seen)
                return
            if is_dataclass(value) and not isinstance(value, type):
                identity = id(value)
                if identity in seen:
                    return
                seen.add(identity)
                for item in fields(value):
                    if item.name in {"origin", "source_origin", "children"}:
                        continue
                    validate_union_types(getattr(value, item.name), seen)

        validate_union_types(
            (
                self.ports,
                self.expressions,
                self.entities,
                self.functions,
                self.registers,
                self.next_assignments,
                self.request_responses,
                self.connections,
                self.rules,
                self.fifos,
                self.memories,
                self.roms,
                self.locals,
                self.instance_bindings,
                self.elaborated_instances,
                self.protocol_endpoints,
                self.hierarchical_connections,
                self.request_response_connections,
                self.protocol_schemas,
                self.aggregate_protocol_endpoints,
                self.aggregate_protocol_connections,
                self.callable_definitions,
                self.module_signature,
                self.external_contract,
            ),
            set(),
        )
        for expected, node in enumerate(self.expressions):
            if node.id != expected:
                raise ValueError("canonical expression IDs must be contiguous")
            if any(operand >= node.id or operand < 0 for operand in node.operands):
                raise ValueError(
                    f"canonical expression %{node.id} has a non-prior operand"
                )
            attribute_names = [name for name, _ in node.attributes]
            if len(attribute_names) != len(set(attribute_names)):
                raise ValueError(
                    f"canonical expression %{node.id} has duplicate attributes"
                )
            if node.op is ExpressionOp.IMPLEMENTATION_CHOICE:
                kinds = node.attribute("kinds")
                evidence = node.attribute("evidence")
                if len(kinds) != len(evidence) or any(
                    kind is not item.candidate
                    for kind, item in zip(kinds, evidence, strict=True)
                ):
                    raise ValueError(
                        f"canonical expression %{node.id} has mismatched "
                        "implementation cost evidence"
                    )
            if node.op is ExpressionOp.ENUM_ENCODE:
                if (
                    len(node.operands) != 1
                    or not isinstance(self.expressions[node.operands[0]].type, EnumType)
                    or not isinstance(node.type, BitsType)
                    or node.type.width != self.expressions[node.operands[0]].type.width
                ):
                    raise ValueError(
                        f"canonical enum encode %{node.id} has invalid types"
                    )
            if node.op is ExpressionOp.ENUM_VALID:
                enum_type = node.attribute("enum_type")
                if (
                    len(node.operands) != 1
                    or not isinstance(enum_type, EnumType)
                    or self.expressions[node.operands[0]].type != BitsType(enum_type.width)
                    or node.type != BitType()
                ):
                    raise ValueError(
                        f"canonical enum valid %{node.id} has invalid types"
                    )
            if node.op is ExpressionOp.ENUM_DECODE:
                if (
                    len(node.operands) != 2
                    or not isinstance(node.type, EnumType)
                    or self.expressions[node.operands[0]].type != BitsType(node.type.width)
                    or self.expressions[node.operands[1]].type != node.type
                ):
                    raise ValueError(
                        f"canonical enum decode %{node.id} has invalid types"
                    )
            if node.op is ExpressionOp.UNION_CONSTRUCT:
                if not isinstance(node.type, TaggedUnionType):
                    raise ValueError(
                        f"canonical union constructor %{node.id} has non-union type"
                    )
                variant = node.type.variant(node.attribute("variant"))
                field_names = node.attribute("field_names")
                if (
                    variant is None
                    or tuple(field.name for field in variant.fields) != field_names
                    or len(node.operands) != len(variant.fields)
                    or any(
                        self.expressions[operand].type != field.type
                        for operand, field in zip(
                            node.operands, variant.fields, strict=True
                        )
                    )
                ):
                    raise ValueError(
                        f"canonical union constructor %{node.id} has invalid layout"
                    )
            if node.op is ExpressionOp.UNION_TAG:
                source = (
                    self.expressions[node.operands[0]].type
                    if len(node.operands) == 1 else None
                )
                if (
                    not isinstance(source, TaggedUnionType)
                    or node.type != BitsType(source.tag_width)
                ):
                    raise ValueError(
                        f"canonical union tag %{node.id} has invalid types"
                    )
            if node.op is ExpressionOp.UNION_FIELD:
                source = (
                    self.expressions[node.operands[0]].type
                    if len(node.operands) == 1 else None
                )
                variant = (
                    source.variant(node.attribute("variant"))
                    if isinstance(source, TaggedUnionType) else None
                )
                field = (
                    variant.field(node.attribute("field"))
                    if variant is not None else None
                )
                if field is None or field.type != node.type:
                    raise ValueError(
                        f"canonical union field %{node.id} has invalid projection"
                    )
        if self.external_contract is not None:
            matching = tuple(
                function
                for function in self.functions
                if function.callee_identity
                == self.external_contract.model_callee_identity
            )
            if len(matching) != 1:
                raise ValueError(
                    "canonical external contract must reference exactly one function"
                )
        entity_ids = [entity.id for entity in self.entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("canonical entity IDs must be unique")
        expression_count = len(self.expressions)
        callable_identities = tuple(
            function.callee_identity
            for function in (*self.functions, *self.callable_definitions)
        )
        if len(callable_identities) != len(set(callable_identities)):
            raise ValueError("canonical callable identities must be unique")
        ordered_specializations = tuple(
            sorted(
                self.callable_definitions,
                key=lambda function: function.callee_identity,
            )
        )
        if self.callable_definitions != ordered_specializations:
            raise ValueError(
                "canonical callable definitions must be ordered by callee identity"
            )
        definitions_by_identity = {
            function.callee_identity: function
            for function in (*self.functions, *self.callable_definitions)
        }
        legacy_by_name = {function.name: function for function in self.functions}

        def resolve_call(node: CanonicalExpression) -> CanonicalFunction | None:
            attributes = dict(node.attributes)
            callee_identity = attributes.get("callee_identity")
            if callee_identity is not None:
                return definitions_by_identity.get(callee_identity)
            return legacy_by_name.get(attributes.get("function"))

        for function in (*self.functions, *self.callable_definitions):
            if function.body < 0 or function.body >= expression_count:
                raise ValueError(
                    f"canonical callable '{function.name}' body root does not exist"
                )
            if self.expressions[function.body].type != function.return_type:
                raise ValueError(
                    f"canonical callable '{function.name}' body type does not match"
                )
        for node in self.expressions:
            if node.op is not ExpressionOp.CALL:
                if node.op is ExpressionOp.FUNCTIONAL_REGION:
                    attributes = dict(node.attributes)
                    layout = attributes.get("table_layout")
                    captures = attributes.get("capture_layout")
                    binder = attributes.get("binder")
                    kind = attributes.get("kind")
                    if (
                        not isinstance(layout, tuple)
                        or not isinstance(captures, tuple)
                        or not isinstance(binder, CompileTimeBinderRef)
                        or not isinstance(kind, FunctionalRegionKind)
                        or not isinstance(node.type, VecType)
                        or not node.operands
                    ):
                        raise ValueError(
                            f"canonical functional region %{node.id} is malformed"
                        )
                    if any(
                        not isinstance(item, tuple)
                        or len(item) != 4
                        or not isinstance(item[0], str)
                        or not isinstance(item[1], int)
                        or not isinstance(item[3], int)
                        or item[3] < 1
                        for item in layout
                    ) or any(
                        not isinstance(item, tuple)
                        or len(item) != 3
                        or not isinstance(item[0], str)
                        or not isinstance(item[1], str)
                        for item in captures
                    ):
                        raise ValueError(
                            f"canonical functional region %{node.id} layout is malformed"
                        )
                    expected = 1 + sum(item[3] for item in layout) + len(captures)
                    if len(node.operands) != expected:
                        raise ValueError(
                            f"canonical functional region %{node.id} layout is invalid"
                        )
                    if self.expressions[node.operands[0]].type != node.type.element_type:
                        raise ValueError(
                            f"canonical functional region %{node.id} template type is invalid"
                        )
                    if (
                        node.type.length != binder.stop - binder.start
                        or len({item[0] for item in layout}) != len(layout)
                        or len({item[0] for item in captures}) != len(captures)
                    ):
                        raise ValueError(
                            f"canonical functional region %{node.id} boundary is invalid"
                        )
                    cursor = 1
                    for _, _, table_type, count in layout:
                        if any(
                            self.expressions[item].type != table_type
                            for item in node.operands[cursor : cursor + count]
                        ):
                            raise ValueError(
                                f"canonical functional region %{node.id} table type is invalid"
                            )
                        cursor += count
                    for offset, (_, _, capture_type) in enumerate(captures):
                        if self.expressions[node.operands[cursor + offset]].type != capture_type:
                            raise ValueError(
                                f"canonical functional region %{node.id} capture type is invalid"
                            )
                if node.op is ExpressionOp.REDUCE:
                    plan = dict(node.attributes).get("plan")
                    if plan is not None:
                        if not isinstance(plan, ExactReductionPlan):
                            raise ValueError(
                                f"canonical reduce %{node.id} plan is malformed"
                            )
                        if len(node.operands) != 1 or node.type != plan.root_type:
                            raise ValueError(
                                f"canonical reduce %{node.id} plan boundary is invalid"
                            )
                        collection_type = self.expressions[node.operands[0]].type
                        if (
                            not isinstance(collection_type, VecType)
                            or collection_type.length != plan.length
                            or collection_type.element_type != plan.leaf_type
                        ):
                            raise ValueError(
                                f"canonical reduce %{node.id} plan collection is invalid"
                            )
                        for level in plan.levels:
                            for operation in level.operations:
                                if operation.callee_identity is None:
                                    continue
                                definition = definitions_by_identity.get(
                                    operation.callee_identity
                                )
                                if (
                                    definition is None
                                    or definition.name != operation.function
                                    or tuple(
                                        parameter.type
                                        for parameter in definition.parameters
                                    )
                                    != (operation.left_type, operation.right_type)
                                    or definition.return_type != operation.result_type
                                ):
                                    raise ValueError(
                                        f"canonical reduce %{node.id} references an "
                                        "invalid exact callable"
                                    )
                continue
            attributes = dict(node.attributes)
            function_name = attributes.get("function")
            callee_identity = attributes.get("callee_identity")
            definition = resolve_call(node)
            if definition is None:
                target = callee_identity or function_name
                raise ValueError(f"canonical call references unknown callable '{target}'")
            if function_name != definition.name:
                raise ValueError(
                    f"canonical call name '{function_name}' does not match its callable"
                )
            operand_types = tuple(self.expressions[item].type for item in node.operands)
            parameter_types = tuple(
                parameter.type for parameter in definition.parameters
            )
            if operand_types != parameter_types:
                raise ValueError(
                    f"canonical call to '{definition.name}' has invalid argument types"
                )
            if node.type != definition.return_type:
                raise ValueError(
                    f"canonical call to '{definition.name}' has invalid return type"
                )

        callable_dependencies: dict[str, tuple[str, ...]] = {}
        for function in (*self.functions, *self.callable_definitions):
            pending = [function.body]
            visited_nodes: set[NodeId] = set()
            dependencies: set[str] = set()
            while pending:
                node_id = pending.pop()
                if node_id in visited_nodes:
                    continue
                visited_nodes.add(node_id)
                node = self.expressions[node_id]
                if node.op is ExpressionOp.CALL:
                    definition = resolve_call(node)
                    # Every call has already been validated above, so resolution
                    # cannot fail here unless that validation changes independently.
                    assert definition is not None
                    dependencies.add(definition.callee_identity)
                elif node.op is ExpressionOp.REDUCE:
                    plan = dict(node.attributes).get("plan")
                    if isinstance(plan, ExactReductionPlan):
                        dependencies.update(
                            operation.callee_identity
                            for level in plan.levels
                            for operation in level.operations
                            if operation.callee_identity is not None
                        )
                pending.extend(node.operands)
            callable_dependencies[function.callee_identity] = tuple(
                sorted(dependencies)
            )

        visited_callables: set[str] = set()
        active_callables: list[str] = []
        active_positions: dict[str, int] = {}

        def validate_acyclic_callable(identity: str) -> None:
            if identity in visited_callables:
                return
            position = active_positions.get(identity)
            if position is not None:
                cycle = (*active_callables[position:], identity)
                rendered = " -> ".join(
                    definitions_by_identity[item].name for item in cycle
                )
                raise ValueError(f"canonical callable cycle: {rendered}")
            active_positions[identity] = len(active_callables)
            active_callables.append(identity)
            for dependency in callable_dependencies[identity]:
                validate_acyclic_callable(dependency)
            active_callables.pop()
            del active_positions[identity]
            visited_callables.add(identity)

        for identity in sorted(definitions_by_identity):
            validate_acyclic_callable(identity)
        for rom in self.roms:
            roots = (rom.read_address, *rom.contents)
            if any(root < 0 or root >= expression_count for root in roots):
                raise ValueError(
                    f"canonical ROM '{rom.name}' has an invalid expression root"
                )
            if self.expressions[rom.read_address].type != rom.address_type:
                raise ValueError(
                    f"canonical ROM '{rom.name}' address type does not match"
                )
            if any(
                self.expressions[word].type != rom.element_type
                for word in rom.contents
            ):
                raise ValueError(
                    f"canonical ROM '{rom.name}' content type does not match"
                )
        for entity in self.entities:
            if any(root < 0 or root >= expression_count for root in entity.roots):
                raise ValueError(
                    f"canonical entity '{entity.id}' has an invalid expression root"
                )
        for exploration in self.pipeline_explorations:
            if not 0 <= exploration.source_expression < expression_count:
                raise ValueError(
                    f"pipeline exploration '{exploration.output}' has an "
                    "invalid source expression"
                )
            names = [candidate.name for candidate in exploration.candidates]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"pipeline exploration '{exploration.output}' has duplicate candidates"
                )
            if exploration.selected not in names:
                raise ValueError(
                    f"pipeline exploration '{exploration.output}' has no selected candidate"
                )
            if exploration.search_bound != len(exploration.candidates):
                raise ValueError(
                    f"pipeline exploration '{exploration.output}' search bound mismatch"
                )
        for region in self.elastic_pipeline_regions:
            if not 0 <= region.source_expression < expression_count:
                raise ValueError(
                    f"elastic pipeline region '{region.semantic_id}' has an "
                    "invalid source expression"
                )
            names = [candidate.name for candidate in region.candidates]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"elastic pipeline region '{region.semantic_id}' has "
                    "duplicate candidates"
                )
            if region.selected not in names:
                raise ValueError(
                    f"elastic pipeline region '{region.semantic_id}' selected "
                    "candidate is absent"
                )
            source_node = self.expressions[region.source_expression]
            if (
                source_node.type != region.output_type
                or source_node.metadata.latency != 0
                or source_node.metadata.initiation_interval != 1
                or source_node.metadata.domains != (region.clock,)
            ):
                raise ValueError(
                    f"elastic pipeline region '{region.semantic_id}' has an "
                    "invalid source-expression boundary"
                )
            for candidate in region.candidates:
                if not 0 <= candidate.expression < expression_count:
                    raise ValueError(
                        f"elastic candidate '{candidate.name}' has an invalid "
                        "expression root"
                    )
                candidate_node = self.expressions[candidate.expression]
                if (
                    candidate_node.type != region.output_type
                    or candidate_node.metadata.latency != candidate.latency
                    or candidate_node.metadata.initiation_interval
                    != candidate.initiation_interval
                    or candidate_node.metadata.domains != (region.clock,)
                ):
                    raise ValueError(
                        f"elastic candidate '{candidate.name}' metadata disagrees "
                        "with its expression"
                    )

            selected = next(
                candidate
                for candidate in region.candidates
                if candidate.name == region.selected
            )
            pending = [selected.expression]
            visited: set[NodeId] = set()
            stages: list[tuple[int, int]] = []
            while pending:
                node_id = pending.pop()
                if node_id in visited:
                    continue
                visited.add(node_id)
                node = self.expressions[node_id]
                if node.op is ExpressionOp.DELAY:
                    raise ValueError(
                        f"elastic candidate '{selected.name}' cannot contain Delay state"
                    )
                if node.op is ExpressionOp.PIPELINE:
                    attributes = dict(node.attributes)
                    instance = attributes.get("instance")
                    stage_count = attributes.get("stages")
                    if (
                        len(node.operands) != 1
                        or type(instance) is not int
                        or instance < 0
                        or type(stage_count) is not int
                        or stage_count < 1
                    ):
                        raise ValueError(
                            f"elastic candidate '{selected.name}' has malformed "
                            "pipeline-stage metadata"
                        )
                    stages.append((instance, stage_count))
                pending.extend(node.operands)
            actual_stages = tuple(sorted(stages))
            if not actual_stages or actual_stages != region.plan.data_stage_instances:
                raise ValueError(
                    f"elastic candidate '{selected.name}' data stages disagree "
                    "with its plan"
                )
        for exploration in self.architecture_explorations:
            if not 0 <= exploration.source_expression < expression_count:
                raise ValueError(
                    f"architecture exploration '{exploration.output}' has an "
                    "invalid source expression"
                )
            names = [candidate.name for candidate in exploration.candidates]
            if len(names) != len(set(names)):
                raise ValueError(
                    f"architecture exploration '{exploration.output}' has "
                    "duplicate candidates"
                )
            if exploration.selected not in names:
                raise ValueError(
                    f"architecture exploration '{exploration.output}' has no "
                    "selected candidate"
                )
            if exploration.search_bound != len(exploration.candidates):
                raise ValueError(
                    f"architecture exploration '{exploration.output}' search "
                    "bound mismatch"
                )
            if exploration.theoretical_candidates != (
                exploration.search_bound + exploration.budget_pruned
            ):
                raise ValueError(
                    f"architecture exploration '{exploration.output}' pruning "
                    "count mismatch"
                )

    def nodes(self, category: NodeCategory) -> tuple[object, ...]:
        return tuple(
            node for node in self.expressions if node.category is category
        ) + tuple(entity for entity in self.entities if entity.category is category)
