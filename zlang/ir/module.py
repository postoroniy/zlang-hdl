"""Typed module-level IR."""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import TYPE_CHECKING

from zlang.ir.expressions import Expression, Reduce, ValueRange
from zlang.ir.callables import (
    CallableKind,
    CallableMetadata,
    stable_callee_identity,
)
from zlang.ir.interfaces import (
    ConnectionAdapter,
    InterfaceProtocol,
    InterfaceSignal,
    RequestResponseChannel,
    RequestResponseOrdering,
    RequestResponseRole,
)
from zlang.ir.types import BitType, HardwareType
from zlang.ir.types import EnumType, StructType, TaggedUnionType
from zlang.ir.csr import CsrAccessInterface, CsrBlock
from zlang.ir.storage import Fifo, Memory, Rom
from zlang.ir.cdc import ClockDomain, Crossing
from zlang.ir.arbitration import PacketArbiter
from zlang.ir.verification import (
    Contract,
    VerificationScope,
    validate_verification_overlay,
)
from zlang.ir.pipelines import PipelineExploration
from zlang.ir.elastic import ElasticPipelineRegion
from zlang.ir.architectures import ArchitectureExploration
from zlang.ir.state import ResolvedTransition
from zlang.ir.timing import (
    InstanceOutputTiming,
    ModuleTimingContract,
    OutputTiming,
)
from zlang.dependencies import DependencyClosure, DependencyModuleIdentity
from zlang.common import stable_digest

if TYPE_CHECKING:
    from zlang.ir.external import ExternalModuleContract


def dependency_context_identity(value: object) -> str | None:
    """Return the exact project/dependency identity carried by an IR object.

    This intentionally uses a small structural interface so proof candidates
    and backend artifacts can participate without depending on ``Module``.
    Legacy modules with no project context retain their historical identities.
    """

    root = getattr(value, "root_module_identity", None)
    closure = getattr(value, "dependency_closure", None)
    if root is None and closure is None:
        return None
    root_data = root.to_data() if root is not None else None
    closure_data = closure.to_data() if closure is not None else None
    return stable_digest(
        {
            "schema": "zlang-dependency-context-v1",
            "root_module_identity": root_data,
            "dependency_closure": closure_data,
        }
    )


def default_selected_ir_identity(module: object) -> str:
    """Select a dependency-sensitive default while preserving legacy spelling."""

    name = str(getattr(module, "name"))
    dependency_identity = dependency_context_identity(module)
    if dependency_identity is None:
        return f"selected:{name}"
    return "selected:" + stable_digest(
        {
            "schema": "zlang-selected-ir-v1",
            "module": name,
            "dependency_identity": dependency_identity,
        }
    )


class EquivalenceGuardKind(str, Enum):
    """The frozen, compile-time-only M27 guard predicate set."""

    UNSIGNED = "unsigned"
    SIGNED = "signed"
    BITS = "bits"
    BIT = "bit"
    WIDTH = "width"
    SAME_TYPE = "same_type"
    CONSTANT = "constant"
    POWER_OF_TWO = "power_of_two"


@dataclass(frozen=True)
class EquivalenceGuardPredicate:
    """One validated M27 guard atom.

    Source guard text is deliberately not retained as semantic data.  The
    analyzer validates arity, bound variables, and the optional integer value
    before this representation reaches canonical IR or egglog.
    """

    kind: EquivalenceGuardKind
    arguments: tuple[str, ...]
    value: int | None = None

    def render(self) -> str:
        arguments = ",".join(self.arguments)
        suffix = "" if self.value is None else f" == {self.value}"
        return f"{self.kind.value}({arguments}){suffix}"


@dataclass(frozen=True)
class EquivalenceRule:
    name: str
    kind: str
    variables: tuple[str, ...]
    guards: tuple[EquivalenceGuardPredicate, ...] = ()
    # Pattern roles retain the source variable associated with the safe frozen
    # rule template.  This avoids guessing that a user's variable is named
    # ``x`` or ``condition`` during egglog registration.
    bindings: tuple[tuple[str, str], ...] = ()
    operator: str | None = None


@dataclass(frozen=True)
class GenericSpecialization:
    """Identity and exact compile-time bindings of one specialization."""

    kind: str
    name: str
    identity: str
    arguments: tuple[tuple[str, str], ...]
    return_type: HardwareType
    bindings: tuple["SpecializationBinding", ...] = ()
    bindings_identity: str = ""

    def __post_init__(self) -> None:
        expected = specialization_bindings_identity(self.bindings)
        if not self.bindings_identity:
            object.__setattr__(self, "bindings_identity", expected)
        elif self.bindings_identity != expected:
            raise ValueError(
                "generic specialization binding identity is inconsistent"
            )


class SpecializationBindingKind(str, Enum):
    """Backend-independent kinds of non-runtime specialization binding."""

    CONSTANT = "constant"
    CALLABLE = "callable"


@dataclass(frozen=True)
class SpecializationBinding:
    """Exact metadata for one typed constant or statically selected callable.

    Source spelling and origin are intentionally absent.  Constants retain a
    type-directed canonical value plus its digest.  Callables retain their
    exact monomorphic signature and selected callee identity.  Dependency and
    evaluator identities make stale elaboration/cache reuse fail closed.
    """

    name: str
    kind: SpecializationBindingKind
    canonical_type: HardwareType | None
    canonical_value: object | None
    content_hash: str
    parameter_types: tuple[HardwareType, ...] = ()
    return_type: HardwareType | None = None
    callee_identity: str | None = None
    dependency_identity: tuple[tuple[str, str], ...] = ()
    evaluator_schema: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("specialization binding name must not be empty")
        if not self.content_hash:
            raise ValueError(
                f"specialization binding '{self.name}' content hash must not be empty"
            )
        if not self.evaluator_schema:
            raise ValueError(
                f"specialization binding '{self.name}' evaluator schema must not be empty"
            )
        if self.dependency_identity != tuple(sorted(self.dependency_identity)):
            raise ValueError(
                f"specialization binding '{self.name}' dependencies must be ordered"
            )
        dependency_names = tuple(name for name, _digest in self.dependency_identity)
        if len(dependency_names) != len(set(dependency_names)):
            raise ValueError(
                f"specialization binding '{self.name}' dependencies must be unique"
            )
        if self.kind is SpecializationBindingKind.CONSTANT:
            if self.canonical_type is None or self.canonical_value is None:
                raise ValueError(
                    f"constant specialization binding '{self.name}' requires "
                    "canonical type and value"
                )
            if self.parameter_types or self.return_type is not None or self.callee_identity:
                raise ValueError(
                    f"constant specialization binding '{self.name}' cannot carry "
                    "callable metadata"
                )
            expected = stable_digest({
                "schema": self.evaluator_schema,
                "kind": self.kind.value,
                "type": str(self.canonical_type),
                "value": self.canonical_value,
                "dependencies": self.dependency_identity,
            })
        else:
            if self.canonical_type is not None or self.canonical_value is not None:
                raise ValueError(
                    f"callable specialization binding '{self.name}' cannot carry "
                    "a constant value"
                )
            if self.return_type is None or not self.callee_identity:
                raise ValueError(
                    f"callable specialization binding '{self.name}' requires "
                    "an exact signature and callee identity"
                )
            expected = stable_digest({
                "schema": self.evaluator_schema,
                "kind": self.kind.value,
                "parameters": tuple(map(str, self.parameter_types)),
                "return": str(self.return_type),
                "callee_identity": self.callee_identity,
                "dependencies": self.dependency_identity,
            })
        if self.content_hash != expected:
            raise ValueError(
                f"specialization binding '{self.name}' content hash is inconsistent"
            )


def validate_specialization_bindings(
    bindings: tuple[SpecializationBinding, ...],
) -> None:
    """Validate deterministic ownership and integrity of binding records."""

    names = tuple(binding.name for binding in bindings)
    if len(names) != len(set(names)):
        raise ValueError("specialization binding names must be unique")
    # Re-run validation so canonical corruption through low-level deserializers
    # cannot bypass the frozen dataclass constructor.
    for binding in bindings:
        binding.__post_init__()


def specialization_bindings_identity(
    bindings: tuple[SpecializationBinding, ...],
) -> str:
    """Return the sealed content identity for an exact binding table."""

    return stable_digest({
        "schema": "zlang-specialization-bindings-v1",
        "bindings": tuple(
            {
                "name": binding.name,
                "kind": binding.kind.value,
                "canonical_type": (
                    str(binding.canonical_type)
                    if binding.canonical_type is not None else None
                ),
                "canonical_value": binding.canonical_value,
                "content_hash": binding.content_hash,
                "parameter_types": tuple(map(str, binding.parameter_types)),
                "return_type": (
                    str(binding.return_type)
                    if binding.return_type is not None else None
                ),
                "callee_identity": binding.callee_identity,
                "dependency_identity": binding.dependency_identity,
                "evaluator_schema": binding.evaluator_schema,
            }
            for binding in bindings
        ),
    })


def validate_generic_specialization(specialization: GenericSpecialization) -> None:
    """Cross-check compact identity arguments against their typed records."""

    validate_specialization_bindings(specialization.bindings)
    if specialization.bindings_identity != specialization_bindings_identity(
        specialization.bindings
    ):
        raise ValueError("generic specialization binding identity is inconsistent")
    arguments = dict(specialization.arguments)
    if len(arguments) != len(specialization.arguments):
        raise ValueError("generic specialization argument names must be unique")
    for binding in specialization.bindings:
        expected = (
            f"constant:{binding.content_hash}"
            if binding.kind is SpecializationBindingKind.CONSTANT
            else f"callable:{binding.callee_identity}"
        )
        if arguments.get(binding.name) != expected:
            raise ValueError(
                f"generic specialization binding '{binding.name}' does not "
                "match its compact identity argument"
            )


@dataclass(frozen=True)
class ProtocolMember:
    name: str
    protocol: InterfaceProtocol
    payload_type: HardwareType
    source_role: str
    sink_role: str
    domain: str | None = None


@dataclass(frozen=True)
class ProtocolSchema:
    name: str
    roles: tuple[str, ...]
    members: tuple[ProtocolMember, ...]
    library_path: str | None = None
    parameters: tuple[tuple[str, str, int | str | None], ...] = ()
    specialization_identity: str | None = None


@dataclass(frozen=True)
class AggregateProtocolEndpoint:
    name: str
    protocol: str
    role: str
    members: tuple[ProtocolMember, ...]
    domain: str | None = None
    specialization_identity: str | None = None


@dataclass(frozen=True)
class AggregateProtocolConnection:
    source: str
    destination: str
    protocol: str
    specialization_identity: str | None = None
    delegation: bool = False
    crossing: Crossing | None = None


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


@dataclass(frozen=True)
class Port:
    direction: PortDirection
    name: str
    type: HardwareType
    protocol: InterfaceProtocol = InterfaceProtocol.WIRE
    capacity: int | None = None
    domain: str | None = None
    virtual_channels: int | None = None


def validate_elastic_module_regions(
    regions: tuple[ElasticPipelineRegion, ...] | tuple[object, ...],
    ports: tuple[Port, ...],
    clock: str | None,
    reset: str | None,
    clock_domains: tuple[ClockDomain, ...],
) -> None:
    """Seal elastic endpoint and physical-domain metadata against its module.

    The same validator is used by semantic and canonical modules.  Backends
    may therefore consume endpoint names only after the exact ready/valid ABI
    and the frozen single-domain clock/reset contract have been checked.
    """

    if not regions:
        return
    if len(regions) != 1:
        raise ValueError("a module may contain exactly one elastic pipeline region")
    region = regions[0]
    source_ports = tuple(
        port for port in ports if port.name == region.source_endpoint
    )
    destination_ports = tuple(
        port for port in ports if port.name == region.destination_endpoint
    )
    if len(source_ports) != 1 or len(destination_ports) != 1:
        raise ValueError("elastic pipeline endpoints do not resolve uniquely")
    source = source_ports[0]
    destination = destination_ports[0]
    if (
        source.direction is not PortDirection.INPUT
        or source.protocol is not InterfaceProtocol.READY_VALID
    ):
        raise ValueError("elastic source endpoint must be a ready/valid input")
    if (
        destination.direction is not PortDirection.OUTPUT
        or destination.protocol is not InterfaceProtocol.READY_VALID
    ):
        raise ValueError("elastic destination endpoint must be a ready/valid output")
    if source.type != region.input_type:
        raise ValueError("elastic source endpoint payload type disagrees with region")
    if destination.type != region.output_type:
        raise ValueError(
            "elastic destination endpoint payload type disagrees with region"
        )
    protocol_ports = tuple(
        port for port in ports
        if port.protocol is InterfaceProtocol.READY_VALID
    )
    if len(protocol_ports) != 2:
        raise ValueError(
            "elastic pipeline module must expose exactly two ready/valid ports"
        )
    # A formal-only artifact may append explicit output wire projections for
    # existing M35 observations.  Those do not alter the production protocol
    # ABI; no other extra runtime/protocol port is admissible here.
    endpoint_names = {region.source_endpoint, region.destination_endpoint}
    if any(
        port.name not in endpoint_names
        and (
            port.direction is not PortDirection.OUTPUT
            or port.protocol is not InterfaceProtocol.WIRE
        )
        for port in ports
    ):
        raise ValueError(
            "elastic pipeline accepts only output-wire formal observation ports "
            "outside its ready/valid endpoints"
        )
    if clock != region.clock or reset != region.reset:
        raise ValueError("elastic region clock/reset disagrees with its module")
    if len(clock_domains) != 1:
        raise ValueError("elastic pipeline requires exactly one clock/reset domain")
    domain = clock_domains[0]
    if (
        domain.clock != region.clock
        or domain.reset != region.reset
        or source.domain != region.clock
        or destination.domain != region.clock
    ):
        raise ValueError("elastic endpoints and region must share one clock domain")
    if not domain.is_legacy_default:
        raise ValueError(
            "elastic pipeline requires rising-edge synchronous active-high reset "
            "with unspecified power-up"
        )


@dataclass(frozen=True)
class ModuleSignatureParameter:
    """One canonical argument of an applied named module interface."""

    name: str
    kind: str
    value: int | str
    declared_default: int | str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"type", "value"}:
            raise ValueError("module signature parameter kind must be type or value")

    def to_data(self) -> dict[str, object]:
        return {
            "declared_default": self.declared_default,
            "kind": self.kind,
            "name": self.name,
            "value": self.value,
        }


@dataclass(frozen=True)
class ModuleSignature:
    """An exact, backend-independent applied public module contract.

    The declaration is nominal, while all member types and parameter arguments
    are canonical.  The logical declaration identity is semantic; physical
    paths and diagnostic source provenance are intentionally excluded so
    concise spelling and checkout placement cannot change ABI.
    """

    name: str
    parameters: tuple[ModuleSignatureParameter, ...]
    ports: tuple[Port, ...]
    clock_domains: tuple[ClockDomain, ...] = ()
    request_responses: tuple[RequestResponseInterface, ...] = ()
    aggregate_protocol_endpoints: tuple[AggregateProtocolEndpoint, ...] = ()
    timing_contract: ModuleTimingContract | None = None
    declaration_identity: str | None = None
    source_origin: object | None = field(default=None, compare=False)

    def to_data(self) -> dict[str, object]:
        def port_data(port: Port) -> dict[str, object]:
            return {
                "capacity": port.capacity,
                "direction": port.direction.value,
                "domain": port.domain,
                "name": port.name,
                "protocol": port.protocol.value,
                "type": str(port.type),
                "virtual_channels": port.virtual_channels,
            }

        def member_data(member: ProtocolMember) -> dict[str, object]:
            return {
                "domain": member.domain,
                "name": member.name,
                "payload_type": str(member.payload_type),
                "protocol": member.protocol.value,
                "sink_role": member.sink_role,
                "source_role": member.source_role,
            }

        timing = self.timing_contract
        return {
            "aggregate_protocol_endpoints": [
                {
                    "domain": endpoint.domain,
                    "members": [member_data(member) for member in endpoint.members],
                    "name": endpoint.name,
                    "protocol": endpoint.protocol,
                    "role": endpoint.role,
                    "specialization_identity": endpoint.specialization_identity,
                }
                for endpoint in self.aggregate_protocol_endpoints
            ],
            "clock_domains": [
                {
                    "clock": domain.clock,
                    "edge": domain.edge.value,
                    "power_up": domain.power_up.value,
                    "reset": domain.reset,
                    "reset_mode": domain.reset_mode.value,
                    "reset_polarity": domain.reset_polarity.value,
                    "reset_release_mode": domain.reset_release_mode.value,
                    "reset_release_cycles": domain.reset_release_cycles,
                }
                for domain in self.clock_domains
            ],
            "declaration_identity": self.declaration_identity or self.name,
            "name": self.name,
            "parameters": [parameter.to_data() for parameter in self.parameters],
            "ports": [port_data(port) for port in self.ports],
            "request_responses": [
                {
                    "id_type": None if item.id_type is None else str(item.id_type),
                    "match_by": item.match_by,
                    "max_outstanding": item.max_outstanding,
                    "name": item.name,
                    "ordering": item.ordering.value,
                    "request_type": str(item.request_type),
                    "response_type": str(item.response_type),
                    "role": item.role.value,
                }
                for item in self.request_responses
            ],
            "schema": "zlang-module-signature-v1",
            "timing_contract": (
                None
                if timing is None
                else {
                    "clock_domain": timing.clock_domain,
                    "ii": timing.initiation_interval,
                    "latency": timing.latency,
                    "reset_domain": timing.reset_domain,
                }
            ),
        }

    @property
    def identity(self) -> str:
        return stable_digest(self.to_data())

    @property
    def nominal_identity(self) -> str:
        return stable_digest(
            {
                "schema": "zlang-module-interface-v1",
                "declaration_identity": self.declaration_identity or self.name,
            }
        )


@dataclass(frozen=True)
class RequestResponseInterface:
    name: str
    request_type: HardwareType
    response_type: HardwareType
    max_outstanding: int
    ordering: RequestResponseOrdering
    match_by: str | None = None
    id_type: HardwareType | None = None
    role: RequestResponseRole = RequestResponseRole.REQUESTER


@dataclass(frozen=True)
class Connection:
    source: Port
    destination: Port
    buffer_depth: int = 0
    adapter: ConnectionAdapter | None = None
    crossing: Crossing | None = None


@dataclass(frozen=True)
class Assignment:
    target: Port | RequestResponseInterface
    expression: Expression
    signal: InterfaceSignal | None = None
    channel: RequestResponseChannel | None = None


@dataclass(frozen=True)
class LocalValue:
    """A named pure value binding inside a module."""

    name: str
    type: HardwareType
    expression: Expression
    compile_time: bool = False
    # Conservative value metadata belongs to the immutable binding rather than
    # to one particular consumer.  Consumers may still re-derive a tighter
    # range after specialization, but must not infer safety from the source
    # spelling of ``name``.
    value_range: ValueRange | None = None
    semantic_identity: str | None = None


@dataclass(frozen=True)
class Specialization:
    name: str
    value: int | str


@dataclass(frozen=True)
class Instance:
    name: str
    module: str
    specializations: tuple[Specialization, ...] = ()
    array_length: int | None = None


@dataclass(frozen=True)
class InstancePortBinding:
    instance: str
    port: str
    expression: Expression


@dataclass(frozen=True)
class ElaboratedInstance:
    """Concrete child instance with an explicit inherited clock/reset domain."""

    instance: Instance
    child_module: str
    clock: str | None
    reset: str | None
    # Stable semantic identities are deliberately separate from backend RTL
    # names.  Defaults preserve the pre-recursive IR constructor ABI; semantic
    # elaboration fills these for physical instances.
    instance_identity: str | None = None
    semantic_path: tuple[str, ...] = ()
    specialization_identity: str | None = None


@dataclass(frozen=True)
class ProtocolEndpoint:
    owner: str
    name: str
    direction: PortDirection
    protocol: InterfaceProtocol
    payload_type: HardwareType
    capacity: int | None = None
    domain: str | None = None
    # Request/response channels are represented as two explicit ready/valid
    # endpoints sharing the same interface owner.  Ordinary ready/valid ports
    # leave this field unset.
    channel: RequestResponseChannel | None = None


@dataclass(frozen=True)
class HierarchicalConnection:
    source: ProtocolEndpoint
    destination: ProtocolEndpoint
    buffer_depth: int = 0
    request_buffer_depth: int = 0
    response_buffer_depth: int = 0
    adapter: ConnectionAdapter | None = None
    crossing: Crossing | None = None


@dataclass(frozen=True)
class RequestResponseConnection:
    """Cross-channel transaction contract for a hierarchical RR link."""

    semantic_id: str
    request: HierarchicalConnection
    response: HierarchicalConnection
    request_type: HardwareType
    response_type: HardwareType
    max_outstanding: int
    ordering: RequestResponseOrdering
    requester: str = ""
    responder: str = ""
    clock_domain: str | None = None
    reset_domain: str | None = None
    reset_epoch_policy: str = "synchronous_shared"
    source_origin: object | None = None


@dataclass(frozen=True)
class FunctionParameter:
    name: str
    type: HardwareType


@dataclass(frozen=True)
class Function:
    name: str
    parameters: tuple[FunctionParameter, ...]
    return_type: HardwareType
    body: Expression
    callee_identity: str = ""
    metadata: CallableMetadata | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("function name must not be empty")
        parameter_names = tuple(parameter.name for parameter in self.parameters)
        if any(not name for name in parameter_names):
            raise ValueError("function parameter name must not be empty")
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError(f"function '{self.name}' parameter names must be unique")
        if self.body.type != self.return_type:
            raise ValueError(
                f"function '{self.name}' body type {self.body.type} does not match "
                f"return type {self.return_type}"
            )
        expected_identity = stable_callee_identity(
            self.name, self.parameters, self.return_type, self.metadata
        )
        if self.callee_identity and self.callee_identity != expected_identity:
            raise ValueError(
                f"function '{self.name}' callee identity does not match its metadata"
            )
        if not self.callee_identity:
            object.__setattr__(self, "callee_identity", expected_identity)


@dataclass(frozen=True)
class Register:
    name: str
    type: HardwareType
    initial: Expression
    domain: str | None = None


@dataclass(frozen=True)
class NextAssignment:
    target: Register | Port
    expression: Expression
    # ``None`` is the compatibility spelling for an unconditionally active
    # effect.  Nested atomic control supplies an exact, already-typed bit
    # predicate here; it does not turn the assignment into a second rule.
    activation: Expression | None = None

    def __post_init__(self) -> None:
        if self.activation is not None and self.activation.type != BitType():
            raise ValueError("rule-action activation must have type bit")


@dataclass(frozen=True)
class Rule:
    name: str
    guard: Expression
    actions: tuple[NextAssignment, ...]


@dataclass(frozen=True)
class RulePriority:
    higher: str
    lower: str


def _validate_exact_reduction_callables(
    value: object,
    definitions: dict[str, Function],
) -> None:
    if isinstance(value, Reduce) and value.plan is not None:
        for level in value.plan.levels:
            for operation in level.operations:
                if operation.callee_identity is None:
                    continue
                definition = definitions.get(operation.callee_identity)
                if (
                    definition is None
                    or definition.name != operation.function
                    or tuple(parameter.type for parameter in definition.parameters)
                    != (operation.left_type, operation.right_type)
                    or definition.return_type != operation.result_type
                ):
                    raise ValueError(
                        "exact reduction plan references an invalid callable"
                    )
    if isinstance(value, tuple):
        for item in value:
            _validate_exact_reduction_callables(item, definitions)
        return
    if is_dataclass(value) and not isinstance(value, type):
        for item in fields(value):
            if item.name in {"type", "origin", "plan"}:
                continue
            _validate_exact_reduction_callables(
                getattr(value, item.name), definitions
            )


def _validate_tagged_union_references(
    declarations: tuple[TaggedUnionType, ...],
    roots: tuple[object, ...],
) -> None:
    """Require every nominal union use to name one exact local declaration."""

    names = tuple(item.name for item in declarations)
    identities = tuple(item.declaration_identity for item in declarations)
    if len(names) != len(set(names)):
        raise ValueError("module tagged-union declaration names must be unique")
    if len(identities) != len(set(identities)):
        raise ValueError("module tagged-union declaration identities must be unique")
    by_identity = {item.declaration_identity: item for item in declarations}
    seen: set[int] = set()

    def visit(value: object) -> None:
        if isinstance(value, TaggedUnionType):
            declared = by_identity.get(value.declaration_identity)
            if declared is None or declared != value:
                raise ValueError(
                    f"tagged-union type '{value.name}' is absent from the exact "
                    "module declaration table"
                )
            return
        if isinstance(value, tuple):
            for item in value:
                visit(item)
            return
        if is_dataclass(value) and not isinstance(value, type):
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            for item in fields(value):
                if item.name in {"origin", "source_origin", "children"}:
                    continue
                visit(getattr(value, item.name))

    for root in roots:
        visit(root)


@dataclass(frozen=True)
class Module:
    name: str
    ports: tuple[Port, ...]
    assignments: tuple[Assignment, ...]
    structs: tuple[StructType, ...] = ()
    enums: tuple[EnumType, ...] = ()
    functions: tuple[Function, ...] = ()
    clock: str | None = None
    reset: str | None = None
    registers: tuple[Register, ...] = ()
    next_assignments: tuple[NextAssignment, ...] = ()
    request_responses: tuple[RequestResponseInterface, ...] = ()
    connections: tuple[Connection, ...] = ()
    csr_blocks: tuple[CsrBlock, ...] = ()
    csr_access: CsrAccessInterface | None = None
    rules: tuple[Rule, ...] = ()
    rule_priorities: tuple[RulePriority, ...] = ()
    fifos: tuple[Fifo, ...] = ()
    memories: tuple[Memory, ...] = ()
    roms: tuple[Rom, ...] = ()
    clock_domains: tuple[ClockDomain, ...] = ()
    arbiters: tuple[PacketArbiter, ...] = ()
    contracts: tuple[Contract, ...] = ()
    pipeline_explorations: tuple[PipelineExploration, ...] = ()
    architecture_explorations: tuple[ArchitectureExploration, ...] = ()
    equivalences: tuple[EquivalenceRule, ...] = ()
    locals: tuple[LocalValue, ...] = ()
    instances: tuple[Instance, ...] = ()
    parameters: tuple[tuple[str, str, int | str | None], ...] = ()
    instance_bindings: tuple[InstancePortBinding, ...] = ()
    children: tuple["Module", ...] = ()
    elaborated_instances: tuple[ElaboratedInstance, ...] = ()
    protocol_endpoints: tuple[ProtocolEndpoint, ...] = ()
    hierarchical_connections: tuple[HierarchicalConnection, ...] = ()
    request_response_connections: tuple[RequestResponseConnection, ...] = ()
    protocol_schemas: tuple[ProtocolSchema, ...] = ()
    aggregate_protocol_endpoints: tuple[AggregateProtocolEndpoint, ...] = ()
    aggregate_protocol_connections: tuple[AggregateProtocolConnection, ...] = ()
    library_imports: tuple[str, ...] = ()
    library_dependencies: tuple[tuple[str, str], ...] = ()
    generic_specializations: tuple[GenericSpecialization, ...] = ()
    resolved_transition: ResolvedTransition | None = None
    source_identity: str | None = None
    source_hash: str | None = None
    timing_contract: ModuleTimingContract | None = None
    output_timings: tuple[OutputTiming, ...] = ()
    instance_output_timings: tuple[InstanceOutputTiming, ...] = ()
    root_module_identity: DependencyModuleIdentity | None = None
    dependency_closure: DependencyClosure | None = None
    module_signature: ModuleSignature | None = None
    # Monomorphic function/operator bodies retained once per exact generic
    # specialization.  Ordinary non-generic source functions remain in
    # ``functions`` for compatibility.
    callable_definitions: tuple[Function, ...] = ()
    external_contract: ExternalModuleContract | None = None
    # Appended to preserve the historical positional constructor ABI.
    tagged_unions: tuple[TaggedUnionType, ...] = ()
    # Bounded ready/valid transforms are not ordinary fixed-latency scalar
    # assignments and therefore retain their own timing/state ownership.
    elastic_pipeline_regions: tuple[ElasticPipelineRegion, ...] = ()
    # Exact non-runtime arguments of this concrete module specialization.
    specialization_bindings: tuple[SpecializationBinding, ...] = ()
    # Verification is a source overlay, not hardware semantics.  It is kept on
    # the typed module for canonical round trips and bundle generation while
    # production backends deliberately ignore it.
    verification_scopes: tuple[VerificationScope, ...] = ()

    def __post_init__(self) -> None:
        validate_verification_overlay(self.verification_scopes)
        for domain in self.clock_domains:
            domain.validate()
        validate_elastic_module_regions(
            self.elastic_pipeline_regions,
            self.ports,
            self.clock,
            self.reset,
            self.clock_domains,
        )
        _validate_tagged_union_references(
            self.tagged_unions,
            (
                self.ports,
                self.assignments,
                self.structs,
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
                self.elastic_pipeline_regions,
                self.module_signature,
                self.external_contract,
            ),
        )
        ordered = tuple(
            sorted(
                self.callable_definitions,
                key=lambda function: function.callee_identity,
            )
        )
        if ordered != self.callable_definitions:
            object.__setattr__(self, "callable_definitions", ordered)
        validate_specialization_bindings(self.specialization_bindings)
        for specialization in self.generic_specializations:
            validate_generic_specialization(specialization)
        identities = tuple(
            function.callee_identity
            for function in (*self.functions, *self.callable_definitions)
        )
        if len(identities) != len(set(identities)):
            raise ValueError("module callable identities must be unique")
        definitions = {
            function.callee_identity: function
            for function in (*self.functions, *self.callable_definitions)
        }
        roots = (
            *(assignment.expression for assignment in self.assignments),
            *(function.body for function in self.functions),
            *(function.body for function in self.callable_definitions),
        )
        for root in roots:
            _validate_exact_reduction_callables(root, definitions)

    @property
    def is_multi_clock(self) -> bool:
        return len(self.clock_domains) > 1

    @property
    def inputs(self) -> tuple[Port, ...]:
        return tuple(
            port for port in self.ports if port.direction is PortDirection.INPUT
        )

    @property
    def outputs(self) -> tuple[Port, ...]:
        return tuple(
            port for port in self.ports if port.direction is PortDirection.OUTPUT
        )

    @property
    def is_sequential(self) -> bool:
        return bool(self.clock_domains)

    @property
    def has_protocol_interfaces(self) -> bool:
        return bool(self.request_responses) or any(
            port.protocol is not InterfaceProtocol.WIRE for port in self.ports
        )

    @property
    def top_aggregate_abi(self):
        from zlang.ir.top_abi import build_top_aggregate_abi
        return build_top_aggregate_abi(self)

    @property
    def top_physical_abi(self):
        """Return the backend-independent always-leaf public top contract."""
        from zlang.ir.top_abi import build_top_physical_abi
        return build_top_physical_abi(self)
