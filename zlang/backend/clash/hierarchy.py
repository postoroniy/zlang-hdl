"""Closed-component ABI planning for Clash protocol hierarchies.

This module owns only Clash-specific naming, specialization planning, and the
small expressions used to apply/project closed child components.  The actual
protocol and state renderers remain in :mod:`zlang.backend.clash.emitter` and
are supplied only with these already-typed plans.  In particular, this module
does not inspect syntax or infer connections from generated RTL names.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from typing import Literal

from zlang.backend.clash.syntax import apply_argument
from zlang.ir.hierarchy import (
    HierarchyEntry,
    HierarchyError,
    HierarchyIndex,
    HierarchySpecializationKey,
    build_hierarchy_index,
)
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, Port, PortDirection


ErrorFactory = Callable[[str], Exception]

__all__ = (
    "RecursiveProtocolComponent",
    "RecursiveProtocolCatalog",
    "ProtocolApplicationKey",
    "ProtocolComponentOwner",
    "closed_protocol_child_call",
    "closed_protocol_child_projection",
    "component_accessor",
    "component_type_name",
    "elaborated_child_for_instance",
    "protocol_child_name",
    "protocol_component_application_key",
    "protocol_instance_child_name",
    "protocol_physical_instance_key",
    "protocol_root_owner",
    "recursive_protocol_components",
    "specialized_protocol_children",
    "uses_bundled_component_abi",
)


def protocol_child_name(
    child: Module, specialization_identity: str | None = None,
) -> str:
    base = f"protocol_{child.name[0].lower()}{child.name[1:]}"
    if specialization_identity is None:
        return base
    # The complete semantic specialization identity avoids relying on a hash
    # prefix being unique among siblings.  It is already a backend-safe hex
    # token and does not encode the physical instance name.
    return f"{base}_{specialization_identity}"


def specialized_protocol_children(
    module: Module,
) -> tuple[tuple[Module, str], ...]:
    """Return one stable Clash component name per typed specialization.

    Preserve the historical unsuffixed helper for a module with only one
    specialization.  When sibling instances specialize the same source module
    differently, disambiguate definitions by semantic specialization identity,
    never by physical instance name.
    """
    entries: list[tuple[Module, str]] = []
    seen: set[HierarchySpecializationKey] = set()
    identities_by_module: dict[str, set[str]] = {}
    for index, child in enumerate(module.children):
        specialization = (
            module.elaborated_instances[index].specialization_identity
            if index < len(module.elaborated_instances)
            else child.source_hash or child.name
        ) or child.name
        identities_by_module.setdefault(child.name, set()).add(specialization)
        key = (child.name, specialization)
        if key not in seen:
            seen.add(key)
            entries.append((child, specialization))
    return tuple(
        (
            child,
            protocol_child_name(
                child,
                specialization
                if len(identities_by_module[child.name]) > 1 else None,
            ),
        )
        for child, specialization in entries
    )


@dataclass(frozen=True)
class ProtocolComponentOwner:
    """Stable owner of child applications in one reusable component body."""

    kind: Literal["root", "specialization"]
    module_name: str
    specialization_identity: str | None = None


@dataclass(frozen=True)
class ProtocolApplicationKey:
    """Stable child application key, independent of Python object identity."""

    parent: ProtocolComponentOwner
    instance_identity: str


@dataclass(frozen=True)
class RecursiveProtocolComponent:
    """One reusable Clash helper selected by semantic specialization."""

    child: Module
    specialization_identity: str
    component_name: str
    owner: ProtocolComponentOwner
    representative_path: tuple[str, ...]


@dataclass(frozen=True)
class RecursiveProtocolCatalog:
    """Typed Clash component catalog derived from one validated hierarchy."""

    hierarchy: HierarchyIndex
    root_owner: ProtocolComponentOwner
    components: tuple[RecursiveProtocolComponent, ...]
    applications: tuple[tuple[ProtocolApplicationKey, str], ...]

    def get(self, key: ProtocolApplicationKey) -> str | None:
        return next(
            (name for candidate, name in self.applications if candidate == key),
            None,
        )

    def child(
        self,
        parent_path: tuple[str, ...],
        instance_name: str,
    ) -> HierarchyEntry:
        return self.hierarchy.child(parent_path, instance_name)

    def application_key(
        self,
        parent_path: tuple[str, ...],
        instance_name: str,
        parent_owner: ProtocolComponentOwner,
    ) -> ProtocolApplicationKey:
        """Resolve one application entirely through the stable hierarchy API."""

        child = self.child(parent_path, instance_name)
        if child.instance_identity is None:
            raise HierarchyError(
                "typed hierarchy child has no semantic instance identity: "
                + ".".join(child.physical_path)
            )
        return ProtocolApplicationKey(parent_owner, child.instance_identity)


def protocol_root_owner(module: Module) -> ProtocolComponentOwner:
    """Return the stable tagged owner for applications in the emitted top."""

    return ProtocolComponentOwner("root", module.name)


def _protocol_specialization_owner(
    child: Module,
    specialization_identity: str,
) -> ProtocolComponentOwner:
    return ProtocolComponentOwner(
        "specialization",
        child.name,
        specialization_identity,
    )


def recursive_protocol_components(
    module: Module,
    *,
    error: ErrorFactory,
) -> RecursiveProtocolCatalog:
    """Plan one closed helper per transitive typed specialization.

    The shared hierarchy index identifies both the concrete module body and
    physical instance metadata.  Clash then keys reusable application sites by
    the parent's semantic specialization and the child's semantic instance
    identity.  No process-local object identity participates in the catalog.
    """

    def validate_recursive_metadata(parent: Module) -> None:
        # Preserve the established Clash diagnostics before the common index
        # performs its stricter whole-hierarchy validation.
        if len(parent.children) != len(parent.elaborated_instances):
            raise error(
                f"hierarchical child '{parent.name}' has incomplete elaborated "
                "instance metadata"
            )
        for child, elaborated in zip(
            parent.children, parent.elaborated_instances, strict=True
        ):
            if child.name != elaborated.child_module:
                raise error(
                    f"elaborated instance '{elaborated.instance.name}' names "
                    f"'{elaborated.child_module}', not typed child '{child.name}'"
                )
            if child.hierarchical_connections:
                validate_recursive_metadata(child)

    validate_recursive_metadata(module)
    try:
        hierarchy = build_hierarchy_index(module)
    except HierarchyError as exc:
        raise error(str(exc)) from exc

    root_owner = protocol_root_owner(module)
    entries: list[
        tuple[HierarchySpecializationKey, Module, ProtocolComponentOwner]
    ] = []
    seen: set[HierarchySpecializationKey] = set()
    identities_by_module: dict[str, set[str]] = {}
    instance_specializations: list[
        tuple[ProtocolApplicationKey, HierarchySpecializationKey]
    ] = []
    application_targets: dict[
        ProtocolApplicationKey,
        HierarchySpecializationKey,
    ] = {}

    def visit(
        parent_path: tuple[str, ...],
        parent_owner: ProtocolComponentOwner,
    ) -> None:
        for child_entry in hierarchy.children_of(parent_path):
            child = child_entry.module
            elaborated = child_entry.elaborated
            if elaborated is None:
                raise error(
                    "typed hierarchy child is missing elaborated metadata at "
                    + ".".join(child_entry.physical_path)
                )
            specialization = child_entry.specialization_identity
            if specialization is None:
                raise error(
                    "typed hierarchy child is missing specialization identity at "
                    + ".".join(child_entry.physical_path)
                )
            key = HierarchySpecializationKey(child.name, specialization)
            instance_identity = child_entry.instance_identity
            if instance_identity is None:
                raise error(
                    "typed hierarchy child is missing semantic instance identity at "
                    + ".".join(child_entry.physical_path)
                )
            application = ProtocolApplicationKey(
                parent_owner,
                instance_identity,
            )
            previous = application_targets.get(application)
            if previous is not None and previous != key:
                raise error(
                    f"semantic instance identity '{application.instance_identity}' "
                    f"under reusable component '{parent_owner.module_name}' maps "
                    "to incompatible specializations"
                )
            if previous is None:
                application_targets[application] = key
                instance_specializations.append((application, key))
            identities_by_module.setdefault(child.name, set()).add(
                specialization
            )
            if key in seen:
                continue
            seen.add(key)
            owner = _protocol_specialization_owner(child, specialization)
            entries.append((key, child, owner))
            if child.hierarchical_connections:
                visit(child_entry.physical_path, owner)

    visit(hierarchy.root_path, root_owner)
    names = {
        key: protocol_child_name(
            child,
            key.specialization_identity
            if len(identities_by_module[child.name]) > 1
            else None,
        )
        for key, child, _owner in entries
    }
    components = tuple(
        RecursiveProtocolComponent(
            child=child,
            specialization_identity=key.specialization_identity,
            component_name=names[key],
            owner=owner,
            representative_path=hierarchy.specialization(key).representative_path,
        )
        for key, child, owner in entries
    )
    applications = tuple(
        (application, names[specialization])
        for application, specialization in instance_specializations
    )
    catalog = RecursiveProtocolCatalog(
        hierarchy=hierarchy,
        root_owner=root_owner,
        components=components,
        applications=applications,
    )
    return catalog


def protocol_component_application_key(
    module: Module,
    instance_name: str,
    *,
    error: ErrorFactory,
    parent_owner: ProtocolComponentOwner | None = None,
) -> ProtocolApplicationKey:
    """Return the stable application identity used by the component catalog.

    The historical helper name remains as an emitter compatibility alias, but
    the key no longer contains ``id(module)`` or any other process-local value.
    """

    elaborated = next(
        (
            item for item in module.elaborated_instances
            if item.instance.name == instance_name
        ),
        None,
    )
    if elaborated is None:
        raise error(
            f"missing elaborated metadata for physical instance "
            f"'{module.name}.{instance_name}'"
        )
    if not elaborated.instance_identity:
        raise error(
            f"elaborated instance '{module.name}.{instance_name}' has no semantic "
            "instance identity"
        )
    return ProtocolApplicationKey(
        parent_owner or protocol_root_owner(module),
        elaborated.instance_identity,
    )


# Compatibility alias for the name used by the extracted emitter before the
# stable catalog existed.  Its value is now a semantic application key, not a
# physical or process-local object key.
protocol_physical_instance_key = protocol_component_application_key


def protocol_instance_child_name(
    module: Module, instance_name: str, child: Module,
) -> str:
    elaborated = next(
        (
            item for item in module.elaborated_instances
            if item.instance.name == instance_name
        ),
        None,
    )
    if elaborated is None:
        return protocol_child_name(child)
    identities = {
        item.specialization_identity or child.name
        for item in module.elaborated_instances
        if item.child_module == child.name
    }
    return protocol_child_name(
        child,
        elaborated.specialization_identity if len(identities) > 1 else None,
    )


def elaborated_child_for_instance(
    module: Module, instance_name: str,
) -> Module | None:
    """Resolve a physical instance by its elaboration index.

    Child source-module names are not unique once one parent contains multiple
    specializations.  The semantic IR deliberately keeps the child list and
    elaborated-instance list aligned, so never collapse that relation into a
    name-keyed lookup.
    """
    for index, elaborated in enumerate(module.elaborated_instances):
        if elaborated.instance.name != instance_name:
            continue
        if index >= len(module.children):
            return None
        child = module.children[index]
        if child.name != elaborated.child_module:
            return None
        return child
    return None


def uses_bundled_component_abi(child: Module) -> bool:
    """Stateful mixed children use a closed, single-Signal component ABI."""
    return bool(
        (child.registers or child.rules or child.next_assignments)
        and any(
            port.protocol is InterfaceProtocol.READY_VALID
            for port in child.ports
        )
        and any(
            port.protocol is InterfaceProtocol.WIRE for port in child.ports
        )
        and len(child.registers) == 1
        and not child.connections
        and not child.hierarchical_connections
        and not child.children
    )


def _component_abi_specialization_suffix(
    child: Module, component_name: str | None,
) -> str:
    base = protocol_child_name(child)
    if component_name is None or component_name == base:
        return ""
    if component_name.startswith(base + "_"):
        return component_name[len(base):]
    return "_" + hashlib.sha256(component_name.encode()).hexdigest()[:16]


def component_type_name(
    child: Module,
    suffix: str,
    component_name: str | None = None,
) -> str:
    identity = _component_abi_specialization_suffix(child, component_name)
    return f"{child.name}Component{suffix}{identity}"


def component_accessor(
    child: Module,
    port: str,
    suffix: str = "",
    component_name: str | None = None,
) -> str:
    prefix = child.name[0].lower() + child.name[1:]
    identity = _component_abi_specialization_suffix(child, component_name)
    return (
        f"{prefix}Component{port[0].upper()}{port[1:]}{suffix}{identity}"
    )


def closed_protocol_child_call(
    child: Module,
    function_name: str,
    arguments: list[str],
    *,
    error: ErrorFactory,
) -> str:
    """Apply one closed child ABI without inspecting generated source text."""

    if not uses_bundled_component_abi(child):
        if not arguments:
            return function_name
        return function_name + " " + " ".join(
            apply_argument(argument) for argument in arguments
        )
    if not arguments:
        raise error(
            f"bundled protocol child '{child.name}' has no explicit inputs"
        )
    bundled = (
        f"{component_type_name(child, 'Input', function_name)} <$> "
        + " <*> ".join(apply_argument(argument) for argument in arguments)
    )
    return f"{function_name} ({bundled})"


def closed_protocol_child_projection(
    child: Module,
    port: Port,
    result_name: str,
    position: int,
    port_count: int,
    function_name: str,
) -> str:
    """Project a typed port from the tuple or bundled closed child result."""

    if uses_bundled_component_abi(child):
        if port.protocol is InterfaceProtocol.READY_VALID:
            suffix = (
                "Backward"
                if port.direction is PortDirection.INPUT
                else "Forward"
            )
        else:
            suffix = ""
        return (
            f"{component_accessor(child, port.name, suffix, function_name)} <$> "
            f"{result_name}"
        )
    if port_count == 1:
        return result_name
    if port_count == 2 and position == 0:
        return f"fst {result_name}"
    if port_count == 2 and position == 1:
        return f"snd {result_name}"
    pattern = ["_" for _ in range(port_count)]
    pattern[position] = f"result{position}"
    return (
        f"let ({','.join(pattern)}) = {result_name} "
        f"in result{position}"
    )
