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
from zlang.ir.module import ElaboratedInstance, Module, PortDirection


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

    return stable_digest({
        "schema": "zlang-typed-module-specialization-v1",
        "content": _specialization_value(module),
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


def validate_hierarchical_connections(
    module: Module,
    *,
    cache: HierarchyTraversalCache | None = None,
) -> None:
    """Validate every hierarchical endpoint against exact typed ownership.

    Semantic analysis and canonical restoration share this check so a restored
    graph cannot invent an indexed child, reinterpret a port, or introduce a
    second physical driver/consumer.  Connections in the current bounded
    hierarchy always select the top or one direct physical child; deeper
    hierarchy is represented inside that child's own :class:`Module`.
    """

    hierarchy = build_hierarchy_index(module, cache=cache)
    root_path = hierarchy.root_path

    def owner_module(owner: str) -> tuple[Module, bool]:
        if owner == module.name:
            return module, True
        try:
            return hierarchy.child(root_path, owner).module, False
        except HierarchyError as error:
            raise HierarchyError(
                f"hierarchical connection references unknown physical owner '{owner}'"
            ) from error

    def validate_endpoint(endpoint: object, *, source: bool) -> None:
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
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "does not name a request/response interface"
                )
            requester = interface.role is RequestResponseRole.REQUESTER
            transaction_output = (
                requester
                if endpoint.channel is RequestResponseChannel.REQUEST
                else not requester
            )
            if transaction_output != source:
                raise HierarchyError(
                    f"hierarchical request/response endpoint "
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
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "does not name a protocol port"
                )
            if port.protocol is InterfaceProtocol.WIRE:
                # Aggregate schemas may contain an ordinary scalar member in
                # either direction (for example an interrupt).  Semantic
                # elaboration publishes that leaf as an explicit typed wire
                # edge alongside the ready/valid members.  Accept only such a
                # schema-owned leaf here; arbitrary scalar child wiring still
                # belongs to InstancePortBinding and cannot be forged as a
                # hierarchical protocol connection during canonical restore.
                aggregate_members = tuple(
                    member
                    for aggregate in current.aggregate_protocol_endpoints
                    for member in aggregate.members
                    if (
                        member.protocol is InterfaceProtocol.WIRE
                        and endpoint.name == f"{aggregate.name}__{member.name}"
                    )
                )
                if len(aggregate_members) != 1:
                    raise HierarchyError(
                        f"hierarchical endpoint '{endpoint.owner}."
                        f"{endpoint.name}' does not name an aggregate scalar "
                        "protocol member"
                    )
            if port.direction is not expected_direction:
                raise HierarchyError(
                    f"hierarchical endpoint '{endpoint.owner}.{endpoint.name}' "
                    "has wrong physical direction"
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
    for connection in module.hierarchical_connections:
        validate_endpoint(connection.source, source=True)
        validate_endpoint(connection.destination, source=False)
        if connection.source.protocol is not connection.destination.protocol:
            raise HierarchyError("hierarchical connection protocol types do not match")
        if connection.source.payload_type != connection.destination.payload_type:
            raise HierarchyError("hierarchical connection payload types do not match")
        if connection.source.domain != connection.destination.domain:
            raise HierarchyError("hierarchical connection domains do not match")
        if connection.source.channel is not connection.destination.channel:
            raise HierarchyError("hierarchical connection channels do not match")
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
]
