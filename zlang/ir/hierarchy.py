"""Validated physical hierarchy traversal for backend consumers.

The semantic IR keeps ``children`` and ``elaborated_instances`` aligned.  A
backend must preserve that exact relation rather than resolving a child later
by source-module name, which is not unique across specializations.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from pathlib import Path

from zlang.common import stable_digest, stable_json
from zlang.ir.interfaces import (
    InterfaceProtocol,
    RequestResponseChannel,
    RequestResponseRole,
)
from zlang.ir.module import (
    AggregateProtocolEndpoint,
    ElaboratedInstance,
    Module,
    Port,
    PortDirection,
    ProtocolEndpoint,
    ProtocolMember,
)


class HierarchyError(ValueError):
    """Typed child/elaboration metadata does not describe one exact hierarchy."""


_CANDIDATE_OWNER_SCHEMA = "zlang-candidate-owner-specialization-v1"


def candidate_specialization_identity(
    module_name: str,
    parameters: tuple[tuple[str, str, int | str | None], ...],
) -> str:
    """Identify one typed specialization for selection-owned site location.

    This identity intentionally excludes physical instance paths: repeated
    instances of the same specialization share candidate selection.  Exact
    compile-time parameter records keep distinct specializations separate.
    """

    if not module_name:
        raise HierarchyError("candidate-site module name must not be empty")
    if any(
        not name or kind not in {"type", "value", "constant", "callable"}
        for name, kind, _value in parameters
    ):
        raise HierarchyError("candidate-site module parameters are invalid")
    return "candidate-owner:" + stable_digest({
        "schema": _CANDIDATE_OWNER_SCHEMA,
        "module": module_name,
        "parameters": parameters,
    })


class HierarchyTraversalCache:
    """Compilation-local memoization for immutable typed hierarchy objects.

    Module IR is frozen.  During recursive elaboration the same exact child
    objects are validated repeatedly as their parents are completed.  Keeping
    their already-computed specialization digests and hierarchy indexes avoids
    re-walking large functional bodies without changing the digest algorithm.

    Object identity is only a private lookup accelerator: each entry retains
    the object and verifies ``is`` on lookup, while every published/serialized
    identity remains the existing content digest.  A caller owns this cache for
    one compilation stage; no process-global state is involved.
    """

    def __init__(self) -> None:
        self._fingerprints: dict[int, tuple[Module, str]] = {}
        self._indexes: dict[int, tuple[Module, HierarchyIndex]] = {}

    def fingerprint(self, module: Module) -> str:
        cached = self._fingerprints.get(id(module))
        if cached is not None and cached[0] is module:
            return cached[1]
        fingerprint = specialization_fingerprint(module)
        self._fingerprints[id(module)] = (module, fingerprint)
        return fingerprint

    def index(self, module: Module) -> HierarchyIndex | None:
        cached = self._indexes.get(id(module))
        if cached is not None and cached[0] is module:
            return cached[1]
        return None

    def remember_index(self, module: Module, index: HierarchyIndex) -> None:
        self._indexes[id(module)] = (module, index)


_SPECIALIZATION_ORIGIN_FIELDS = frozenset({
    "origin", "origins", "source_origin", "source_identity", "source_hash",
    "source_path", "root_module_identity", "dependency_closure",
})

_SPECIALIZATION_APPLICATION_FIELDS = frozenset({
    # A specialization describes the reusable child component.  Physical
    # applications, paths, and recursively retained child objects are checked
    # separately by HierarchyIndex and must not make two equivalent component
    # definitions appear different.
    "instances", "instance_bindings", "children", "elaborated_instances",
    "protocol_endpoints", "hierarchical_connections",
    "request_response_connections", "aggregate_protocol_connections",
    "instance_output_timings",
})

_SPECIALIZATION_VISIBLE_CATALOG_FIELDS = frozenset({
    # Semantic analysis gives each child the declarations visible in its
    # current compilation context.  That catalog is a name-resolution input,
    # not reusable component content: two parents may contribute unrelated
    # helpers or discover the same helpers in a different order.  The exact
    # executable closure is added explicitly by specialization_fingerprint().
    "functions", "callable_definitions",
})


def _specialization_value(value: object) -> object:
    """Return source- and application-independent typed semantic content."""

    if isinstance(value, Enum):
        return {
            "$enum": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": value.value,
        }
    if is_dataclass(value) and not isinstance(value, type):
        excluded = set(_SPECIALIZATION_ORIGIN_FIELDS)
        if isinstance(value, Module):
            excluded.update(_SPECIALIZATION_APPLICATION_FIELDS)
            excluded.update(_SPECIALIZATION_VISIBLE_CATALOG_FIELDS)
        # A ROM semantic ID historically contains its source-unit identity;
        # immutable contents, type, depth, address behavior, and content hash
        # below are the actual reusable specialization semantics.
        if type(value).__module__ == "zlang.ir.storage" and type(value).__name__ == "Rom":
            excluded.add("semantic_id")
        return {
            "$type": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": [
                [
                    item.name,
                    _specialization_field_value(
                        item.name, getattr(value, item.name)
                    ),
                ]
                for item in fields(value)
                if item.name not in excluded
            ],
        }
    if isinstance(value, Mapping):
        entries = [
            (_specialization_value(key), _specialization_value(item))
            for key, item in value.items()
        ]
        entries.sort(key=lambda pair: stable_json(pair[0]))
        return {"$mapping": [[key, item] for key, item in entries]}
    if isinstance(value, (tuple, list)):
        return [_specialization_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_specialization_value(item) for item in value]
        items.sort(key=stable_json)
        return {"$set": items}
    if isinstance(value, Path):
        return {"$path": "omitted"}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        "typed specialization fingerprint cannot serialize "
        f"{type(value).__module__}.{type(value).__qualname__}"
    )


def _specialization_field_value(name: str, value: object) -> object:
    if name == "declaration_identity" and isinstance(value, str):
        source, separator, declaration = value.partition("::")
        if separator and Path(source).is_absolute():
            value = "$source::" + declaration
    return _specialization_value(value)


def specialization_fingerprint(module: Module) -> str:
    """Fingerprint one reusable typed module specialization.

    The digest covers the complete reusable semantic component definition but
    deliberately excludes diagnostics provenance and physical applications.
    Backends can therefore deduplicate deep-copied equivalent specializations,
    while canonical restoration cannot reuse one identity for incompatible
    ports, state, storage, rules, timing, or expressions.
    """

    # Import locally to keep this low-level hierarchy module independent of
    # callable implementation details at import time.  The reachability API
    # validates identities, bodies, dependencies, and cycles while producing
    # a deterministic dependency-first closure.
    from zlang.ir.callables import (
        CallableReachabilityError,
        reachable_module_callables,
    )

    try:
        reachable_callables = reachable_module_callables(
            module,
            include_hierarchy=False,
        )
    except CallableReachabilityError as error:
        raise HierarchyError(
            f"typed specialization callable graph is invalid: {error}"
        ) from error

    return stable_digest({
        "schema": "zlang-typed-module-specialization-v2",
        "content": _specialization_value(module),
        "reachable_callables": _specialization_value(reachable_callables),
    })


@dataclass(frozen=True)
class HierarchyEntry:
    """One exact physical module instance in a validated hierarchy."""

    physical_path: tuple[str, ...]
    module: Module
    elaborated: ElaboratedInstance | None = None

    @property
    def specialization_identity(self) -> str | None:
        return (
            None
            if self.elaborated is None
            else self.elaborated.specialization_identity
        )

    @property
    def instance_identity(self) -> str | None:
        return (
            None
            if self.elaborated is None
            else self.elaborated.instance_identity
        )

    @property
    def parent_path(self) -> tuple[str, ...] | None:
        return None if self.elaborated is None else self.physical_path[:-1]

    @property
    def physical_name(self) -> str:
        return self.physical_path[-1]


@dataclass(frozen=True)
class HierarchySpecializationKey:
    """Exact backend-neutral identity of one typed specialization."""

    module_name: str
    specialization_identity: str


@dataclass(frozen=True)
class HierarchySpecialization:
    """Stable typed specialization catalog record.

    The key mirrors the semantic relation already used to validate reused
    specializations.  Paths, rather than module object references, make the
    record stable across compilation sessions and deep copies.
    """

    key: HierarchySpecializationKey
    representative_path: tuple[str, ...]
    occurrence_paths: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class HierarchyIndex:
    """Immutable, exact lookup table keyed by physical instance path."""

    entries: tuple[HierarchyEntry, ...]

    @property
    def root(self) -> HierarchyEntry:
        if not self.entries:
            raise HierarchyError("typed hierarchy index has no root entry")
        return self.entries[0]

    @property
    def root_path(self) -> tuple[str, ...]:
        return self.root.physical_path

    def at(self, physical_path: tuple[str, ...]) -> HierarchyEntry:
        match = next(
            (item for item in self.entries if item.physical_path == physical_path),
            None,
        )
        if match is None:
            raise HierarchyError(
                "physical instance path is absent from typed hierarchy: "
                + ".".join(physical_path)
            )
        return match

    def child(
        self,
        parent_path: tuple[str, ...],
        instance_name: str,
    ) -> HierarchyEntry:
        """Return one exact direct child by its typed physical name."""

        parent = self.at(parent_path)
        elaborated = next(
            (
                item
                for item in parent.module.elaborated_instances
                if item.instance.name == instance_name
            ),
            None,
        )
        if elaborated is None:
            raise HierarchyError(
                "physical child instance is absent from typed hierarchy: "
                + ".".join(parent_path + (instance_name,))
            )
        child = self.at(parent_path + (instance_name,))
        if child.elaborated != elaborated:
            raise HierarchyError(
                "physical child entry does not match parent elaboration: "
                + ".".join(child.physical_path)
            )
        return child

    def children_of(
        self,
        parent_path: tuple[str, ...],
    ) -> tuple[HierarchyEntry, ...]:
        """Return direct children in authoritative elaboration order."""

        parent = self.at(parent_path)
        return tuple(
            self.child(parent_path, elaborated.instance.name)
            for elaborated in parent.module.elaborated_instances
        )

    def child_by_instance_identity(
        self,
        parent_path: tuple[str, ...],
        instance_identity: str,
    ) -> HierarchyEntry:
        """Resolve a stable semantic instance identity under one parent path."""

        matches = tuple(
            child
            for child in self.children_of(parent_path)
            if child.instance_identity == instance_identity
        )
        if not matches:
            raise HierarchyError(
                f"semantic instance identity '{instance_identity}' is absent under "
                f"physical parent {'.'.join(parent_path)}"
            )
        if len(matches) != 1:
            raise HierarchyError(
                f"semantic instance identity '{instance_identity}' is ambiguous under "
                f"physical parent {'.'.join(parent_path)}"
            )
        return matches[0]

    @property
    def specializations(self) -> tuple[HierarchySpecialization, ...]:
        """Return exact child specializations in first depth-first order."""

        order: list[HierarchySpecializationKey] = []
        occurrences: dict[
            HierarchySpecializationKey,
            list[HierarchyEntry],
        ] = {}
        for entry in self.entries[1:]:
            specialization = entry.specialization_identity
            if specialization is None:
                raise HierarchyError(
                    "physical child has no specialization identity: "
                    + ".".join(entry.physical_path)
                )
            key = HierarchySpecializationKey(entry.module.name, specialization)
            if key not in occurrences:
                order.append(key)
                occurrences[key] = []
            occurrences[key].append(entry)
        return tuple(
            HierarchySpecialization(
                key=key,
                representative_path=occurrences[key][0].physical_path,
                occurrence_paths=tuple(
                    entry.physical_path for entry in occurrences[key]
                ),
            )
            for key in order
        )

    def specialization(
        self,
        key: HierarchySpecializationKey,
    ) -> HierarchySpecialization:
        """Return one exact typed specialization catalog record."""

        match = next(
            (item for item in self.specializations if item.key == key),
            None,
        )
        if match is None:
            raise HierarchyError(
                "typed specialization is absent from hierarchy: "
                f"{key.module_name}@{key.specialization_identity}"
            )
        return match

    def specialization_catalog(self) -> tuple[HierarchySpecialization, ...]:
        """Compatibility spelling for the immutable specialization view."""

        return self.specializations


def build_hierarchy_index(
    module: Module,
    *,
    cache: HierarchyTraversalCache | None = None,
) -> HierarchyIndex:
    """Validate and index every exact physical child in ``module``."""

    selected_cache = cache or HierarchyTraversalCache()
    cached_index = selected_cache.index(module)
    if cached_index is not None:
        return cached_index

    entries: list[HierarchyEntry] = []
    paths: set[tuple[str, ...]] = set()
    active_modules: set[int] = set()
    specializations: dict[HierarchySpecializationKey, str] = {}

    def visit(
        current: Module,
        path: tuple[str, ...],
        elaborated_from_parent: ElaboratedInstance | None,
    ) -> None:
        if path in paths:
            raise HierarchyError(
                "duplicate physical instance path in typed hierarchy: "
                + ".".join(path)
            )
        if id(current) in active_modules:
            raise HierarchyError(
                "cyclic typed module hierarchy at physical path: "
                + ".".join(path)
            )
        paths.add(path)
        entries.append(HierarchyEntry(path, current, elaborated_from_parent))
        active_modules.add(id(current))
        try:
            if len(current.children) != len(current.elaborated_instances):
                raise HierarchyError(
                    f"module '{current.name}' has {len(current.children)} "
                    "typed children "
                    f"but {len(current.elaborated_instances)} elaborated instances"
                )
            names: set[str] = set()
            instance_identities: set[str] = set()
            for child, elaborated in zip(
                current.children, current.elaborated_instances, strict=True
            ):
                instance_name = elaborated.instance.name
                if not instance_name:
                    raise HierarchyError(
                        f"module '{current.name}' has an elaborated child with "
                        "no physical name"
                    )
                if instance_name in names:
                    raise HierarchyError(
                        f"module '{current.name}' has duplicate physical child "
                        f"'{instance_name}'"
                    )
                names.add(instance_name)
                if child.name != elaborated.child_module:
                    raise HierarchyError(
                        f"elaborated instance '{instance_name}' names child module "
                        f"'{elaborated.child_module}', not typed child '{child.name}'"
                    )
                if elaborated.instance.module != elaborated.child_module:
                    raise HierarchyError(
                        f"elaborated instance '{instance_name}' source module "
                        f"'{elaborated.instance.module}' does not match child "
                        f"'{elaborated.child_module}'"
                    )
                if not elaborated.instance_identity:
                    raise HierarchyError(
                        f"elaborated instance '{instance_name}' has no semantic "
                        "instance identity"
                    )
                if elaborated.instance_identity in instance_identities:
                    raise HierarchyError(
                        f"module '{current.name}' has duplicate semantic instance "
                        f"identity '{elaborated.instance_identity}'"
                    )
                instance_identities.add(elaborated.instance_identity)
                if not elaborated.specialization_identity:
                    raise HierarchyError(
                        f"elaborated instance '{instance_name}' has no "
                        "specialization identity"
                    )
                expected_semantic_path = (current.name, instance_name)
                if elaborated.semantic_path != expected_semantic_path:
                    raise HierarchyError(
                        f"elaborated instance '{instance_name}' semantic path "
                        f"{elaborated.semantic_path!r} does not match "
                        f"{expected_semantic_path!r}"
                    )
                if child.clock_domains:
                    if len(current.clock_domains) != 1 or len(child.clock_domains) != 1:
                        raise HierarchyError(
                            f"child '{instance_name}' physical clock/reset contract "
                            "requires one exact parent domain"
                        )
                    parent_domain = current.clock_domains[0]
                    child_domain = child.clock_domains[0]
                    if (
                        elaborated.clock != parent_domain.clock
                        or elaborated.reset != parent_domain.reset
                        or child.clock != child_domain.clock
                        or child.reset != child_domain.reset
                        or child_domain != parent_domain
                    ):
                        raise HierarchyError(
                            f"child '{instance_name}' physical clock/reset contract "
                            "does not exactly match its elaborated parent domain"
                        )
                elif elaborated.clock is not None or elaborated.reset is not None:
                    raise HierarchyError(
                        f"combinational child '{instance_name}' cannot reference "
                        "a physical clock/reset domain"
                    )
                specialization_key = HierarchySpecializationKey(
                    child.name,
                    elaborated.specialization_identity,
                )
                fingerprint = selected_cache.fingerprint(child)
                previous_fingerprint = specializations.get(specialization_key)
                if (
                    previous_fingerprint is not None
                    and previous_fingerprint != fingerprint
                ):
                    raise HierarchyError(
                        "specialization identity "
                        f"'{elaborated.specialization_identity}' "
                        f"is reused for incompatible '{child.name}'; "
                        "incompatible typed specialization content"
                    )
                specializations[specialization_key] = fingerprint
                visit(child, path + (instance_name,), elaborated)
        finally:
            active_modules.remove(id(current))

    visit(module, (module.name,), None)
    result = HierarchyIndex(tuple(entries))
    selected_cache.remember_index(module, result)
    return result


def validate_instance_port_bindings(
    module: Module,
    *,
    cache: HierarchyTraversalCache | None = None,
) -> None:
    """Validate complete, single-driver scalar wiring throughout hierarchy.

    ``InstancePortBinding`` is the authoritative scalar child-input relation.
    Aggregate protocol schemas may instead publish an ordinary wire member as
    a typed hierarchical connection.  Every child wire input must use exactly
    one of those two representations; canonical restoration must not be able
    to invent an owner/port, change its type, or silently omit it at any depth.
    """

    hierarchy = build_hierarchy_index(module, cache=cache)
    validated_modules: set[int] = set()

    def aggregate_member_port(
        owner: Module,
        endpoint: AggregateProtocolEndpoint,
        member: ProtocolMember,
    ) -> Port:
        """Resolve one aggregate member to its exact typed physical port."""

        expected_direction = (
            PortDirection.OUTPUT
            if endpoint.role == member.source_role
            else PortDirection.INPUT
            if endpoint.role == member.sink_role
            else None
        )
        if expected_direction is None:
            raise HierarchyError(
                f"aggregate endpoint '{endpoint.name}' role '{endpoint.role}' "
                f"does not own member '{member.name}'"
            )
        expected_name = f"{endpoint.name}__{member.name}"
        port = next(
            (item for item in owner.ports if item.name == expected_name),
            None,
        )
        if port is None:
            raise HierarchyError(
                f"aggregate endpoint '{endpoint.name}' member '{member.name}' "
                "has no exact physical port"
            )
        if (
            port.direction is not expected_direction
            or port.protocol is not member.protocol
            or port.type != member.payload_type
            or port.domain != member.domain
        ):
            raise HierarchyError(
                f"aggregate endpoint '{endpoint.name}' member '{member.name}' "
                "physical port metadata disagrees"
            )
        return port

    for parent_entry in hierarchy.entries:
        current = parent_entry.module
        current_key = id(current)
        if current_key in validated_modules:
            continue
        validated_modules.add(current_key)
        child_owners = {
            elaborated.instance.name: (elaborated, child)
            for elaborated, child in zip(
                current.elaborated_instances,
                current.children,
                strict=True,
            )
        }
        children = {
            owner: child
            for owner, (_elaborated, child) in child_owners.items()
        }

        hierarchical_drivers = {
            (connection.destination.owner, connection.destination.name)
            for connection in current.hierarchical_connections
            if (
                connection.destination.owner in children
                and connection.destination.channel is None
                and connection.destination.protocol is InterfaceProtocol.WIRE
            )
        }
        delegation_pairs: set[tuple[str, str]] = set()
        delegation_destinations: dict[
            tuple[ElaboratedInstance, AggregateProtocolEndpoint],
            str,
        ] = {}
        delegation_sources: dict[AggregateProtocolEndpoint, str] = {}
        for connection in current.aggregate_protocol_connections:
            if not connection.delegation:
                continue
            source_parts = connection.source.split(".")
            destination_parts = connection.destination.split(".")
            if len(source_parts) != 1 or len(destination_parts) != 2:
                raise HierarchyError(
                    "aggregate protocol delegation must connect one top endpoint "
                    "to one direct physical child endpoint"
                )
            top_endpoint = next(
                (
                    item
                    for item in current.aggregate_protocol_endpoints
                    if item.name == source_parts[0]
                ),
                None,
            )
            child_owner = child_owners.get(destination_parts[0])
            child = child_owner[1] if child_owner is not None else None
            child_endpoint = next(
                (
                    item
                    for item in child.aggregate_protocol_endpoints
                    if item.name == destination_parts[1]
                ),
                None,
            ) if child is not None else None
            if (
                top_endpoint is None
                or child_owner is None
                or child_endpoint is None
            ):
                raise HierarchyError(
                    f"aggregate protocol delegation '{connection.source} -> "
                    f"{connection.destination}' does not resolve exact endpoints"
                )
            if connection.crossing is not None:
                raise HierarchyError(
                    f"aggregate protocol delegation '{connection.source} -> "
                    f"{connection.destination}' cannot carry crossing metadata"
                )
            if (
                top_endpoint.protocol != child_endpoint.protocol
                or top_endpoint.protocol != connection.protocol
                or top_endpoint.role != child_endpoint.role
                or top_endpoint.specialization_identity
                != child_endpoint.specialization_identity
                or top_endpoint.specialization_identity
                != connection.specialization_identity
            ):
                raise HierarchyError(
                    f"aggregate protocol delegation '{connection.source} -> "
                    f"{connection.destination}' metadata disagrees with its endpoints"
                )
            child_members = {item.name: item for item in child_endpoint.members}
            if {
                item.name: item for item in top_endpoint.members
            } != child_members:
                raise HierarchyError(
                    f"aggregate protocol delegation '{connection.source} -> "
                    f"{connection.destination}' member metadata disagrees"
                )
            pair = (connection.source, connection.destination)
            if pair in delegation_pairs:
                raise HierarchyError(
                    f"duplicate aggregate protocol delegation "
                    f"'{connection.source} -> {connection.destination}'"
                )
            destination_owner = (child_owner[0], child_endpoint)
            previous_source = delegation_destinations.get(destination_owner)
            if previous_source is not None:
                raise HierarchyError(
                    f"aggregate protocol destination '{connection.destination}' "
                    "has multiple delegation drivers "
                    f"('{previous_source}' and '{connection.source}')"
                )
            previous_destination = delegation_sources.get(top_endpoint)
            if previous_destination is not None:
                raise HierarchyError(
                    f"aggregate protocol source '{connection.source}' has multiple "
                    "delegation destinations "
                    f"('{previous_destination}' and '{connection.destination}')"
                )
            delegation_pairs.add(pair)
            delegation_destinations[destination_owner] = connection.source
            delegation_sources[top_endpoint] = connection.destination

            top_ports = {
                aggregate_member_port(current, top_endpoint, member)
                for member in top_endpoint.members
            }
            locally_driven = {
                assignment.target for assignment in current.assignments
            }
            locally_driven.update(
                assignment.target for assignment in current.next_assignments
            )
            locally_driven.update(
                action.target
                for rule in current.rules
                for action in rule.actions
            )
            if top_ports & locally_driven:
                raise HierarchyError(
                    f"aggregate protocol source '{connection.source}' has both "
                    "local output assignment and child delegation"
                )

            for member in child_members.values():
                port = aggregate_member_port(child, child_endpoint, member)
                if (
                    member.protocol is InterfaceProtocol.WIRE
                    and port.direction is PortDirection.INPUT
                ):
                    hierarchical_drivers.add(
                        (destination_parts[0], port.name)
                    )

        scalar_drivers: set[tuple[str, str]] = set()
        for binding in current.instance_bindings:
            child = children.get(binding.instance)
            if child is None:
                raise HierarchyError(
                    "scalar instance binding references unknown physical child "
                    f"'{binding.instance}'"
                )
            port = next(
                (item for item in child.ports if item.name == binding.port),
                None,
            )
            if port is None:
                raise HierarchyError(
                    f"scalar instance binding '{binding.instance}.{binding.port}' "
                    "does not name a child port"
                )
            if port.direction is not PortDirection.INPUT:
                raise HierarchyError(
                    f"scalar instance binding '{binding.instance}.{binding.port}' "
                    "does not name a child input"
                )
            if port.protocol is not InterfaceProtocol.WIRE:
                raise HierarchyError(
                    f"scalar instance binding '{binding.instance}.{binding.port}' "
                    "does not name a wire input"
                )
            if binding.expression.type != port.type:
                raise HierarchyError(
                    f"scalar instance binding '{binding.instance}.{binding.port}' "
                    f"has type {binding.expression.type}, expected {port.type}"
                )
            key = (binding.instance, binding.port)
            if key in scalar_drivers:
                raise HierarchyError(
                    f"scalar instance input '{binding.instance}.{binding.port}' "
                    "has multiple bindings"
                )
            if key in hierarchical_drivers:
                raise HierarchyError(
                    f"child wire input '{binding.instance}.{binding.port}' has both "
                    "a scalar binding and hierarchical connection"
                )
            scalar_drivers.add(key)

        for owner, child in children.items():
            for port in child.inputs:
                if port.protocol is not InterfaceProtocol.WIRE:
                    continue
                key = (owner, port.name)
                if key not in scalar_drivers and key not in hierarchical_drivers:
                    raise HierarchyError(
                        f"child wire input '{owner}.{port.name}' has no driver"
                    )


def validate_hierarchical_connections(
    module: Module,
    *,
    cache: HierarchyTraversalCache | None = None,
) -> None:
    """Validate every hierarchical endpoint against exact typed ownership.

    Semantic analysis and canonical restoration share this check so a restored
    graph cannot invent an indexed child, reinterpret a port, or introduce a
    second physical driver/consumer.  Each connection selects its owning
    module or one direct physical child; validation repeats for every exact
    child module retained by the typed hierarchy.
    """

    hierarchy = build_hierarchy_index(module, cache=cache)
    validated_modules: set[int] = set()

    for parent_entry in hierarchy.entries:
        current_module = parent_entry.module
        current_key = id(current_module)
        if current_key in validated_modules:
            continue
        validated_modules.add(current_key)
        child_modules = {
            elaborated.instance.name: child
            for elaborated, child in zip(
                current_module.elaborated_instances,
                current_module.children,
                strict=True,
            )
        }

        def owner_module(owner: str) -> tuple[Module, bool]:
            if owner == current_module.name:
                return current_module, True
            child = child_modules.get(owner)
            if child is None:
                raise HierarchyError(
                    "hierarchical connection references unknown physical owner "
                    f"'{owner}'"
                )
            return child, False

        def validate_endpoint(
            endpoint: ProtocolEndpoint,
            *,
            source: bool,
        ) -> None:
            current, top = owner_module(endpoint.owner)
            expected_direction = (
                PortDirection.INPUT if source and top
                else PortDirection.OUTPUT if source
                else PortDirection.OUTPUT if top
                else PortDirection.INPUT
            )

            if endpoint.channel is not None:
                interface = next(
                    (
                        item for item in current.request_responses
                        if item.name == endpoint.name
                    ),
                    None,
                )
                if interface is None:
                    raise HierarchyError(
                        f"hierarchical endpoint '{endpoint.owner}."
                        f"{endpoint.name}' does not name a request/response "
                        "interface"
                    )
                requester = interface.role is RequestResponseRole.REQUESTER
                transaction_output = (
                    requester
                    if endpoint.channel is RequestResponseChannel.REQUEST
                    else not requester
                )
                if transaction_output != source:
                    raise HierarchyError(
                        "hierarchical request/response endpoint "
                        f"'{endpoint.owner}.{endpoint.name}' has wrong role for "
                        f"{endpoint.channel.value} direction"
                    )
                payload_type = (
                    interface.request_type
                    if endpoint.channel is RequestResponseChannel.REQUEST
                    else interface.response_type
                )
                protocol = InterfaceProtocol.READY_VALID
                capacity = None
                domain = current.clock
            else:
                port = next(
                    (item for item in current.ports if item.name == endpoint.name),
                    None,
                )
                if port is None:
                    raise HierarchyError(
                        f"hierarchical endpoint '{endpoint.owner}."
                        f"{endpoint.name}' does not name a protocol port"
                    )
                if port.protocol is InterfaceProtocol.WIRE:
                    # Aggregate schemas may contain an ordinary scalar member
                    # in either direction (for example an interrupt).  Only a
                    # schema-owned typed leaf may occupy this relation.
                    aggregate_members = tuple(
                        member
                        for aggregate in current.aggregate_protocol_endpoints
                        for member in aggregate.members
                        if (
                            member.protocol is InterfaceProtocol.WIRE
                            and endpoint.name
                            == f"{aggregate.name}__{member.name}"
                        )
                    )
                    if len(aggregate_members) != 1:
                        raise HierarchyError(
                            f"hierarchical endpoint '{endpoint.owner}."
                            f"{endpoint.name}' does not name an aggregate "
                            "scalar protocol member"
                        )
                if port.direction is not expected_direction:
                    raise HierarchyError(
                        f"hierarchical endpoint '{endpoint.owner}."
                        f"{endpoint.name}' has wrong physical direction"
                    )
                payload_type = port.type
                protocol = port.protocol
                capacity = port.capacity
                domain = port.domain if top else current.clock

            if endpoint.direction is not expected_direction:
                raise HierarchyError(
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "direction metadata disagrees with its position"
                )
            if endpoint.protocol is not protocol:
                raise HierarchyError(
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "protocol metadata disagrees with its declaration"
                )
            if endpoint.payload_type != payload_type:
                raise HierarchyError(
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "payload type disagrees with its declaration"
                )
            if endpoint.capacity != capacity:
                raise HierarchyError(
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "capacity metadata disagrees with its declaration"
                )
            if endpoint.domain != domain:
                raise HierarchyError(
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "domain metadata disagrees with its declaration"
                )

        sources: set[tuple[str, str, RequestResponseChannel | None]] = set()
        destinations: set[tuple[str, str, RequestResponseChannel | None]] = set()
        for connection in current_module.hierarchical_connections:
            validate_endpoint(connection.source, source=True)
            validate_endpoint(connection.destination, source=False)
            if connection.source.protocol is not connection.destination.protocol:
                raise HierarchyError(
                    "hierarchical connection protocol types do not match"
                )
            if connection.source.payload_type != connection.destination.payload_type:
                raise HierarchyError(
                    "hierarchical connection payload types do not match"
                )
            if connection.source.domain != connection.destination.domain:
                raise HierarchyError(
                    "hierarchical connection domains do not match"
                )
            if connection.source.channel is not connection.destination.channel:
                raise HierarchyError(
                    "hierarchical connection channels do not match"
                )
            source_key = (
                connection.source.owner,
                connection.source.name,
                connection.source.channel,
            )
            destination_key = (
                connection.destination.owner,
                connection.destination.name,
                connection.destination.channel,
            )
            if source_key in sources:
                raise HierarchyError(
                    f"hierarchical source '{connection.source.owner}."
                    f"{connection.source.name}' has multiple consumers"
                )
            if destination_key in destinations:
                raise HierarchyError(
                    f"hierarchical destination '{connection.destination.owner}."
                    f"{connection.destination.name}' has multiple drivers"
                )
            sources.add(source_key)
            destinations.add(destination_key)


__all__ = [
    "HierarchyEntry",
    "HierarchyError",
    "HierarchyIndex",
    "HierarchySpecialization",
    "HierarchySpecializationKey",
    "HierarchyTraversalCache",
    "build_hierarchy_index",
    "candidate_specialization_identity",
    "validate_hierarchical_connections",
    "validate_instance_port_bindings",
]
