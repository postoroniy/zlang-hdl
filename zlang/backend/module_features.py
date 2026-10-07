"""Backend-neutral inventory of semantic entities owned by one module.

The inventory is deliberately derived from typed IR, not from generated names.
Backends use it as a fail-closed preflight before selecting one of the legacy
specialized emitters.  Those emitters predate compositional state lowering and
must never silently ignore an otherwise valid user state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.ir.module import InterfaceProtocol, Module


class ModuleFeatureKind(str, Enum):
    ASSIGNMENT = "assignment"
    LOCAL = "local"
    REGISTER = "register"
    NEXT_ASSIGNMENT = "next_assignment"
    RULE = "rule"
    FIFO = "fifo"
    MEMORY = "memory"
    ROM = "rom"
    CSR = "csr"
    REQUEST_RESPONSE = "request_response"
    ARBITER = "arbiter"
    CREDIT = "credit"
    VC_CREDIT = "vc_credit"
    PROTOCOL_ENDPOINT = "protocol_endpoint"
    AGGREGATE_ENDPOINT = "aggregate_endpoint"
    INSTANCE = "instance"
    CONNECTION = "connection"
    AGGREGATE_CONNECTION = "aggregate_connection"
    REQUEST_RESPONSE_LEDGER = "request_response_ledger"
    ELASTIC_PIPELINE = "elastic_pipeline"


class ModuleFeatureGroup(str, Enum):
    """Concrete typed-IR collections consumed by backend contributors.

    Groups deliberately name physical IR collections rather than feature
    *kinds*.  A backend plan must opt in to every collection it renders; adding
    a new collection therefore produces an unclaimed-entity failure instead of
    being hidden by a wildcard over :class:`ModuleFeatureKind`.
    """

    ASSIGNMENTS = "assignments"
    LOCALS = "locals"
    REGISTERS = "registers"
    NEXT_ASSIGNMENTS = "next_assignments"
    RULES = "rules"
    FIFOS = "fifos"
    MEMORIES = "memories"
    ROMS = "roms"
    CSR_BLOCKS = "csr_blocks"
    REQUEST_RESPONSE_INTERFACES = "request_response_interfaces"
    PROTOCOL_PORTS = "protocol_ports"
    HIERARCHICAL_PROTOCOL_ENDPOINTS = "hierarchical_protocol_endpoints"
    AGGREGATE_PROTOCOL_ENDPOINTS = "aggregate_protocol_endpoints"
    ARBITERS = "arbiters"
    CREDIT_PORTS = "credit_ports"
    VC_CREDIT_PORTS = "vc_credit_ports"
    INSTANCES = "instances"
    CONNECTIONS = "connections"
    HIERARCHICAL_CONNECTIONS = "hierarchical_connections"
    AGGREGATE_PROTOCOL_CONNECTIONS = "aggregate_protocol_connections"
    REQUEST_RESPONSE_LEDGERS = "request_response_ledgers"
    ELASTIC_PIPELINE_REGIONS = "elastic_pipeline_regions"


@dataclass(frozen=True, order=True)
class ModuleFeature:
    kind: ModuleFeatureKind
    identity: str


@dataclass(frozen=True)
class ModuleFeatureInventory:
    module: str
    features: tuple[ModuleFeature, ...]

    def __post_init__(self) -> None:
        identities = tuple((item.kind, item.identity) for item in self.features)
        if len(identities) != len(set(identities)):
            raise ValueError("module feature identities must be unique per kind")

    def of_kind(self, kind: ModuleFeatureKind) -> tuple[ModuleFeature, ...]:
        return tuple(item for item in self.features if item.kind is kind)

    @property
    def has_user_state(self) -> bool:
        return any(
            self.of_kind(kind)
            for kind in (
                ModuleFeatureKind.REGISTER,
                ModuleFeatureKind.NEXT_ASSIGNMENT,
                ModuleFeatureKind.RULE,
            )
        )


@dataclass(frozen=True, order=True)
class ModuleFeatureClaim:
    """One emitter contributor's ownership of one semantic entity."""

    feature: ModuleFeature
    contributor: str

    def __post_init__(self) -> None:
        if not self.contributor:
            raise ValueError("module feature claim contributor must not be empty")


class ModuleFeatureAccountingError(ValueError):
    """An emission plan omitted or multiply claimed a semantic entity."""


def claims_for_kinds(
    inventory: ModuleFeatureInventory,
    contributor: str,
    kinds: frozenset[ModuleFeatureKind],
) -> tuple[ModuleFeatureClaim, ...]:
    """Claim the inventory subset explicitly supported by one plan."""

    return tuple(
        ModuleFeatureClaim(feature, contributor)
        for feature in inventory.features
        if feature.kind in kinds
    )


def claims_for_groups(
    module: Module,
    contributor: str,
    groups: tuple[ModuleFeatureGroup, ...],
) -> tuple[ModuleFeatureClaim, ...]:
    """Claim concrete IR entities rendered by named plan contributors.

    The contributor recorded on each claim includes the concrete collection so
    accounting diagnostics identify which part of an emission plan claimed an
    entity.  Repeated groups intentionally create duplicate claims and are
    rejected by :func:`validate_feature_claims`.
    """

    grouped = module_feature_groups(module)
    claims: list[ModuleFeatureClaim] = []
    for group in groups:
        claims.extend(
            ModuleFeatureClaim(feature, f"{contributor}:{group.value}")
            for feature in grouped[group]
        )
    return tuple(claims)


def validate_feature_claims(
    inventory: ModuleFeatureInventory,
    claims: tuple[ModuleFeatureClaim, ...],
    *,
    backend: str,
    plan: str,
) -> None:
    """Require an exact one-to-one accounting before artifact publication."""

    owners: dict[ModuleFeature, list[str]] = {
        feature: [] for feature in inventory.features
    }
    unknown: list[ModuleFeatureClaim] = []
    for claim in claims:
        if claim.feature not in owners:
            unknown.append(claim)
            continue
        owners[claim.feature].append(claim.contributor)
    missing = tuple(feature for feature, found in owners.items() if not found)
    duplicate = tuple(
        (feature, tuple(found))
        for feature, found in owners.items()
        if len(found) > 1
    )
    if not (unknown or missing or duplicate):
        return
    details: list[str] = []
    if missing:
        details.append(
            "unclaimed="
            + ",".join(f"{item.kind.value}:{item.identity}" for item in missing)
        )
    if duplicate:
        details.append(
            "multiply_claimed="
            + ",".join(
                f"{item.kind.value}:{item.identity}[{'/'.join(found)}]"
                for item, found in duplicate
            )
        )
    if unknown:
        details.append(
            "unknown="
            + ",".join(
                f"{item.feature.kind.value}:{item.feature.identity}"
                for item in unknown
            )
        )
    raise ModuleFeatureAccountingError(
        f"{backend} emission plan '{plan}' does not account exactly once for "
        f"module '{inventory.module}': {'; '.join(details)}"
    )


def validate_plan_kinds(
    module: Module,
    *,
    backend: str,
    plan: str,
    kinds: frozenset[ModuleFeatureKind],
) -> ModuleFeatureInventory:
    """Convenience preflight for a single-contributor emission plan."""

    inventory = module_feature_inventory(module)
    validate_feature_claims(
        inventory,
        claims_for_kinds(inventory, plan, kinds),
        backend=backend,
        plan=plan,
    )
    return inventory


def _assignment_identity(index: int, assignment: object) -> str:
    target = getattr(getattr(assignment, "target", None), "name", "unknown")
    signal = getattr(getattr(assignment, "signal", None), "value", None)
    channel = getattr(getattr(assignment, "channel", None), "value", None)
    suffix = ".".join(item for item in (channel, signal) if item is not None)
    return f"{index}:{target}" + (f".{suffix}" if suffix else "")


def _protocol_endpoint_identity(prefix: str, endpoint: object) -> str:
    owner = getattr(endpoint, "owner", "")
    name = getattr(endpoint, "name", "unknown")
    channel = getattr(getattr(endpoint, "channel", None), "value", None)
    protocol = getattr(getattr(endpoint, "protocol", None), "value", None)
    direction = getattr(getattr(endpoint, "direction", None), "value", None)
    head = ".".join(item for item in (owner, name) if item)
    suffix = ":".join(
        item for item in (protocol, channel, direction) if item is not None
    )
    return f"{prefix}:{head}" + (f":{suffix}" if suffix else "")


def module_feature_groups(
    module: Module,
) -> dict[ModuleFeatureGroup, tuple[ModuleFeature, ...]]:
    """Return the exact typed entities partitioned by their IR collection."""

    grouped: dict[ModuleFeatureGroup, list[ModuleFeature]] = {
        group: [] for group in ModuleFeatureGroup
    }

    def add(
        group: ModuleFeatureGroup,
        kind: ModuleFeatureKind,
        identity: str,
    ) -> None:
        grouped[group].append(ModuleFeature(kind, identity))

    for index, assignment in enumerate(module.assignments):
        add(
            ModuleFeatureGroup.ASSIGNMENTS,
            ModuleFeatureKind.ASSIGNMENT,
            _assignment_identity(index, assignment),
        )
    for local in module.locals:
        add(
            ModuleFeatureGroup.LOCALS,
            ModuleFeatureKind.LOCAL,
            local.semantic_identity or local.name,
        )
    for register in module.registers:
        add(
            ModuleFeatureGroup.REGISTERS,
            ModuleFeatureKind.REGISTER,
            register.name,
        )
    for index, assignment in enumerate(module.next_assignments):
        add(
            ModuleFeatureGroup.NEXT_ASSIGNMENTS,
            ModuleFeatureKind.NEXT_ASSIGNMENT,
            f"{index}:{getattr(assignment.target, 'name', 'unknown')}",
        )
    for rule in module.rules:
        add(ModuleFeatureGroup.RULES, ModuleFeatureKind.RULE, rule.name)
    for fifo in module.fifos:
        add(ModuleFeatureGroup.FIFOS, ModuleFeatureKind.FIFO, fifo.name)
    for memory in module.memories:
        add(
            ModuleFeatureGroup.MEMORIES,
            ModuleFeatureKind.MEMORY,
            memory.semantic_id,
        )
    for rom in module.roms:
        add(ModuleFeatureGroup.ROMS, ModuleFeatureKind.ROM, rom.semantic_id)
    for index, block in enumerate(module.csr_blocks):
        identity = getattr(block, "identity", None)
        add(
            ModuleFeatureGroup.CSR_BLOCKS,
            ModuleFeatureKind.CSR,
            str(identity or f"{index}:{block.name}"),
        )
    for interface in module.request_responses:
        add(
            ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
            ModuleFeatureKind.REQUEST_RESPONSE,
            interface.name,
        )
        for channel in ("request", "response"):
            add(
                ModuleFeatureGroup.REQUEST_RESPONSE_INTERFACES,
                ModuleFeatureKind.PROTOCOL_ENDPOINT,
                f"request_response:{interface.name}:{channel}",
            )
    for port in module.ports:
        if port.protocol is not InterfaceProtocol.WIRE:
            add(
                ModuleFeatureGroup.PROTOCOL_PORTS,
                ModuleFeatureKind.PROTOCOL_ENDPOINT,
                _protocol_endpoint_identity("port", port),
            )
        if port.protocol is InterfaceProtocol.CREDIT:
            add(
                ModuleFeatureGroup.CREDIT_PORTS,
                ModuleFeatureKind.CREDIT,
                port.name,
            )
        elif port.protocol is InterfaceProtocol.VC_CREDIT:
            add(
                ModuleFeatureGroup.VC_CREDIT_PORTS,
                ModuleFeatureKind.VC_CREDIT,
                port.name,
            )
    for endpoint in module.protocol_endpoints:
        add(
            ModuleFeatureGroup.HIERARCHICAL_PROTOCOL_ENDPOINTS,
            ModuleFeatureKind.PROTOCOL_ENDPOINT,
            _protocol_endpoint_identity("hierarchical", endpoint),
        )
    for endpoint in module.aggregate_protocol_endpoints:
        identity = endpoint.specialization_identity or endpoint.protocol
        add(
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
            ModuleFeatureKind.AGGREGATE_ENDPOINT,
            f"{endpoint.name}:{identity}:{endpoint.role}",
        )
        for member in endpoint.members:
            add(
                ModuleFeatureGroup.AGGREGATE_PROTOCOL_ENDPOINTS,
                ModuleFeatureKind.PROTOCOL_ENDPOINT,
                f"aggregate:{endpoint.name}.{member.name}:"
                f"{member.protocol.value}:{endpoint.role}",
            )
    for index, arbiter in enumerate(module.arbiters):
        destination = getattr(arbiter.destination, "name", arbiter.destination)
        add(
            ModuleFeatureGroup.ARBITERS,
            ModuleFeatureKind.ARBITER,
            f"{index}:{destination}",
        )
    for index, instance in enumerate(module.elaborated_instances):
        add(
            ModuleFeatureGroup.INSTANCES,
            ModuleFeatureKind.INSTANCE,
            instance.instance_identity or f"{index}:{instance.instance.name}",
        )
    for index, connection in enumerate(module.connections):
        add(
            ModuleFeatureGroup.CONNECTIONS,
            ModuleFeatureKind.CONNECTION,
            f"port:{index}:{connection.source.name}->{connection.destination.name}",
        )
    for index, connection in enumerate(module.hierarchical_connections):
        add(
            ModuleFeatureGroup.HIERARCHICAL_CONNECTIONS,
            ModuleFeatureKind.CONNECTION,
            f"hierarchical:{index}:"
            f"{connection.source.owner}.{connection.source.name}"
            f"->{connection.destination.owner}.{connection.destination.name}:"
            f"{getattr(connection.source.channel, 'value', '')}",
        )
    for index, connection in enumerate(module.aggregate_protocol_connections):
        add(
            ModuleFeatureGroup.AGGREGATE_PROTOCOL_CONNECTIONS,
            ModuleFeatureKind.AGGREGATE_CONNECTION,
            f"{index}:{connection.source}->{connection.destination}:"
            f"{connection.protocol}:{connection.specialization_identity or ''}",
        )
    for descriptor in module.request_response_connections:
        add(
            ModuleFeatureGroup.REQUEST_RESPONSE_LEDGERS,
            ModuleFeatureKind.REQUEST_RESPONSE_LEDGER,
            descriptor.semantic_id,
        )
    for region in module.elastic_pipeline_regions:
        add(
            ModuleFeatureGroup.ELASTIC_PIPELINE_REGIONS,
            ModuleFeatureKind.ELASTIC_PIPELINE,
            region.semantic_id,
        )
    return {
        group: tuple(sorted(features))
        for group, features in grouped.items()
    }


def module_feature_inventory(module: Module) -> ModuleFeatureInventory:
    """Return a deterministic inventory for backend capability accounting."""

    groups = module_feature_groups(module)
    features = tuple(
        sorted(feature for values in groups.values() for feature in values)
    )
    return ModuleFeatureInventory(module.name, features)


_SPECIALIZED_STATE_KINDS = (
    ModuleFeatureKind.ARBITER,
    ModuleFeatureKind.CREDIT,
    ModuleFeatureKind.VC_CREDIT,
)


def unsupported_legacy_state_mix(module: Module) -> tuple[str, ...]:
    """Describe state engines that cannot safely own user state together.

    Scheduled storage and composed hierarchy use the unified transition path and
    are intentionally excluded.  A globally controlled FIFO still selects the
    legacy FIFO emitter, so it participates in this preflight.
    """

    inventory = module_feature_inventory(module)
    if not inventory.has_user_state:
        return ()
    has_unified_storage = (
        any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or bool(module.roms and module.resolved_transition is not None)
    )
    if module.elaborated_instances or has_unified_storage:
        return ()
    engines: list[str] = []
    if any(not fifo.scheduled for fifo in module.fifos):
        engines.append("globally controlled FIFO")
    for kind in _SPECIALIZED_STATE_KINDS:
        if inventory.of_kind(kind):
            engines.append(kind.value.replace("_", " "))
    return tuple(engines)


__all__ = [
    "ModuleFeatureAccountingError",
    "ModuleFeatureClaim",
    "ModuleFeatureGroup",
    "ModuleFeature",
    "ModuleFeatureInventory",
    "ModuleFeatureKind",
    "module_feature_inventory",
    "claims_for_kinds",
    "claims_for_groups",
    "module_feature_groups",
    "unsupported_legacy_state_mix",
    "validate_feature_claims",
    "validate_plan_kinds",
]
