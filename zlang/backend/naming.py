"""Deterministic, hierarchy-scoped physical names, separate from IR identity.

The plan consumes typed names and identities, never generated HDL.  Callers own
and reuse plans for one emission; there is deliberately no process-global cache.
Public leaf names and source register names are reserved, not rewritten.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
import hashlib
import re
from types import MappingProxyType

from zlang.backend.identifiers import rtl_identifier
from zlang.ir import expressions as expr
from zlang.ir.hierarchy import HierarchyIndex, HierarchySpecializationKey
from zlang.ir.interfaces import (
    CreditSignal, InterfaceProtocol, PacketSignal, ReadyValidSignal,
    VirtualChannelCreditSignal,
)
from zlang.ir.module import Module
from zlang.ir.top_abi import build_top_physical_abi
from zlang.ir.traversal import ExpressionTraversalPolicy, expression_children


RTL_NAMING_SCHEMA = "zlang-hierarchy-local-names-v1"
Identifier = Callable[[str], str]


class RtlNamingError(ValueError):
    """A physical namespace cannot represent distinct semantic objects safely."""


def _digest(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


def _identity_token(identity: str) -> str:
    # Preserve the stored specialization prefix, including leading zeroes.
    # Non-hex identity formats use a digest only for their physical spelling.
    return identity.lower() if re.fullmatch(r"[0-9a-fA-F]{8,}", identity) else _digest(identity)


def _private_base(name: str) -> str:
    """Normalize generated separators only; never call on public/source leaves."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", name))


@dataclass(frozen=True)
class ComponentPhysicalName:
    key: HierarchySpecializationKey
    physical_name: str
    suffix: str


@dataclass(frozen=True)
class ComponentNamePlan:
    root_name: str
    entries: tuple[ComponentPhysicalName, ...]
    schema: str = RTL_NAMING_SCHEMA
    _lookup: Mapping[HierarchySpecializationKey, ComponentPhysicalName] = field(
        init=False, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "_lookup", MappingProxyType({item.key: item for item in self.entries}))

    def component(self, module_name: str, specialization_identity: str) -> str:
        return self._entry(module_name, specialization_identity).physical_name

    def suffix(self, module_name: str, specialization_identity: str) -> str:
        return self._entry(module_name, specialization_identity).suffix

    def _entry(self, module_name: str, specialization_identity: str) -> ComponentPhysicalName:
        key = HierarchySpecializationKey(module_name, specialization_identity)
        try:
            return self._lookup[key]
        except KeyError as error:
            raise RtlNamingError(f"component naming key is absent: {module_name}@{specialization_identity}") from error


def build_component_name_plan(
    hierarchy: HierarchyIndex,
    *,
    identifier: Identifier = rtl_identifier,
    reserved: Iterable[str] = (),
) -> ComponentNamePlan:
    """Allocate stable short specialization names for the entire HDL unit.

    All colliding prefixes extend together, independently of traversal order.
    Full keys remain in the records; truncated tokens never establish equality.
    """
    keys = sorted(
        {item.key for item in hierarchy.specializations},
        key=lambda item: (item.module_name, item.specialization_identity),
    )
    top_name = identifier(hierarchy.root.module.name)
    unavailable = set(reserved) | {top_name}
    tokens = {key: _identity_token(key.specialization_identity) for key in keys}
    widths = {key: min(8, len(tokens[key])) for key in keys}
    while True:
        proposed = {
            key: f"{identifier(key.module_name)}_s{tokens[key][:widths[key]]}"
            for key in keys
        }
        groups: dict[str, list[HierarchySpecializationKey]] = {}
        for key, name in proposed.items():
            groups.setdefault(name, []).append(key)
        collisions = {
            key for name, members in groups.items()
            if len(members) > 1 or name in unavailable
            for key in members
        }
        if not collisions:
            return ComponentNamePlan(top_name, tuple(
                ComponentPhysicalName(key, proposed[key], f"s{tokens[key][:widths[key]]}")
                for key in keys
            ))
        for key in sorted(collisions, key=lambda item: (item.module_name, item.specialization_identity)):
            if widths[key] >= len(tokens[key]):
                raise RtlNamingError(
                    "distinct component identities exhaust their physical-name prefix: "
                    f"{key.module_name}@{key.specialization_identity} -> {proposed[key]}"
                )
            widths[key] = min(widths[key] + 4, len(tokens[key]))


def validate_component_name_plans(*plans: ComponentNamePlan) -> None:
    """Reject incompatible same-token definitions before combined HDL use.

    Identical full child keys may be deduplicated by the caller.  This function
    does not concatenate RTL or infer equality by reading a short HDL token.
    """
    definitions: dict[str, HierarchySpecializationKey] = {}
    roots = {plan.root_name for plan in plans}
    if len(roots) != len(plans):
        raise RtlNamingError("separately emitted plans repeat a frozen public top name")
    for plan in plans:
        for item in plan.entries:
            previous = definitions.get(item.physical_name)
            if item.physical_name in roots or (previous is not None and previous != item.key):
                raise RtlNamingError(
                    f"combined HDL component name '{item.physical_name}' has conflicting full identities"
                )
            definitions[item.physical_name] = item.key


@dataclass(frozen=True)
class LocalPhysicalName:
    kind: str
    key: tuple[str, ...]
    semantic_identity: str
    preferred: str
    physical_name: str


@dataclass(frozen=True)
class ModuleRtlNames:
    module_name: str
    entries: tuple[LocalPhysicalName, ...]
    reserved: frozenset[str]
    schema: str = RTL_NAMING_SCHEMA
    _lookup: Mapping[tuple[str, ...], str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_lookup", MappingProxyType({
            (item.kind, *item.key): item.physical_name for item in self.entries
        }))

    def _name(self, kind: str, *key: str) -> str:
        try:
            return self._lookup[(kind, *key)]
        except KeyError as error:
            raise RtlNamingError(f"local naming key is absent in '{self.module_name}': {kind}:{key}") from error

    @property
    def allocated_names(self) -> frozenset[str]:
        return self.reserved | frozenset(item.physical_name for item in self.entries)

    def instance(self, source_name: str) -> str:
        return self._name("instance", source_name)

    def child_signal(self, owner: str, port: str, signal: str | None = None) -> str:
        return self._name("child_signal", owner, port if signal is None else f"{port}_{signal}")

    def instance_helper(self, owner: str, role: str) -> str:
        return self._name("instance_helper", owner, role)

    def rule(self, rule_name: str, role: str = "fire") -> str:
        return self._name("rule", rule_name, role)

    def stage(self, kind: str, instance: int, index: int) -> str:
        try:
            return self._name("stage", kind, str(instance), str(index))
        except RtlNamingError:
            # A trivial fixed pipeline is represented as one public N-cycle
            # node but is emitted with one physical stage per historical
            # instance.  Resolve that compatibility spelling on demand.
            if kind == "pipeline" and index > 1:
                return self._name(
                    "stage", kind, str(instance + index - 1), "1"
                )
            raise


@dataclass(frozen=True)
class _Request:
    kind: str
    key: tuple[str, ...]
    identity: str
    preferred: str


def _allocate_requests(
    requests: Iterable[_Request], used: set[str], identifier: Identifier,
) -> tuple[LocalPhysicalName, ...]:
    ordered = sorted(requests, key=lambda item: (item.kind, item.key))
    preferred = {item: identifier(item.preferred) for item in ordered}
    multiplicity: dict[str, int] = {}
    for candidate in preferred.values():
        multiplicity[candidate] = multiplicity.get(candidate, 0) + 1
    suffixed = {item for item in ordered if preferred[item] in used or multiplicity[preferred[item]] > 1}
    widths = {item: 8 for item in suffixed}
    digests = {item: _digest(item.identity) for item in suffixed}
    while True:
        candidates = {
            item: preferred[item] if item not in suffixed else f"{preferred[item]}_{digests[item][:widths[item]]}"
            for item in ordered
        }
        counts: dict[str, int] = {}
        for candidate in candidates.values():
            counts[candidate] = counts.get(candidate, 0) + 1
        conflicts = {item for item in ordered if candidates[item] in used or counts[candidates[item]] > 1}
        if not conflicts:
            used.update(candidates.values())
            return tuple(LocalPhysicalName(item.kind, item.key, item.identity, item.preferred, candidates[item]) for item in ordered)
        for item in conflicts:
            if item not in suffixed:
                suffixed.add(item)
                widths[item] = 8
                digests[item] = _digest(item.identity)
            elif widths[item] < len(digests[item]):
                widths[item] = min(widths[item] + 4, len(digests[item]))
            else:
                raise RtlNamingError(f"distinct local objects exhaust physical-name collision resolution: {item.key}")


def _module_reserved_names(module: Module, identifier: Identifier) -> set[str]:
    names = {identifier(item.name) for group in (module.ports, module.registers, module.locals, module.functions, module.callable_definitions) for item in group}
    for leaf in build_top_physical_abi(module).leaves:
        names.add(identifier(leaf.external_name))
        if leaf.packed_root_external_name is not None:
            names.add(identifier(leaf.packed_root_external_name))
    for storage in (*module.memories, *module.roms):
        base = identifier(storage.name)
        names.update(f"{base}_{suffix}" for suffix in (
            "cells", "read_data", "read_fire", "write_fire", "read_address",
            "write_address", "write_data", "write_mask", "write_mask_expanded",
            "write_merged", "reset_index",
        ))
    for fifo in module.fifos:
        base = identifier(fifo.name)
        names.update(f"{base}_{suffix}" for suffix in (
            "storage", "count", "rd", "wr", "push_request", "pop_request",
            "push", "pop", "push_data", "front", "empty", "full", "valid",
            "ready", "overflow", "underflow",
        ))
    if module.memories:
        names.add("zlang_memory_reset_index")
    return names


def _child_port_names(module: Module) -> tuple[str, ...]:
    names: set[str] = {port.name for port in module.ports}
    for leaf in build_top_physical_abi(module).leaves:
        # Children retain the packed component ABI. Public top leaves are
        # projections of those roots, not additional component interconnects:
        # aggregate bus__irq and public bus_irq denote the same packed port.
        # Preserve distinct actual port names and let allocation resolve real
        # spelling collisions, rather than reserving both views of one port.
        names.add(leaf.packed_root_external_name or leaf.external_name)
    signals = {
        InterfaceProtocol.READY_VALID: ReadyValidSignal,
        InterfaceProtocol.CREDIT: CreditSignal,
        InterfaceProtocol.PACKET: PacketSignal,
        InterfaceProtocol.VC_CREDIT: VirtualChannelCreditSignal,
    }
    for port in module.ports:
        names.update(f"{port.name}_{signal.value}" for signal in signals.get(port.protocol, ()))
    for interface in module.request_responses:
        names.update(f"{interface.name}_{channel}" for channel in ("request", "response"))
        names.update(f"{interface.name}_{channel}_{signal}" for channel in ("request", "response") for signal in ("payload", "valid", "ready", "transfer"))
    return tuple(sorted(names))


def _local_owner_identity(module: Module) -> str:
    # The same reusable parent can occur below several physical ancestors.
    # Application-specific instance IDs must therefore not enter its local
    # collision suffixes.  Exact typed bindings identify its local namespace
    # without fingerprinting expressions or traversing descendant modules.
    return repr((
        module.name,
        module.source_identity,
        module.parameters,
        tuple((item.name, item.kind.value, item.content_hash) for item in module.specialization_bindings),
    ))


def _stage_requests(module: Module) -> tuple[_Request, ...]:
    # Shared roots, but STRUCTURAL traversal: never expand a functional template
    # or fingerprint a potentially large child graph merely to choose a name.
    from zlang.backend.expression_materialization import module_expression_roots

    hints: dict[tuple[str, int], str] = {}
    for name, expression in (
        *((local.name, local.expression) for local in module.locals),
        *((assignment.target.name, assignment.expression) for assignment in module.assignments),
    ):
        if isinstance(expression, (expr.Delay, expr.Pipeline)):
            kind = "delay" if isinstance(expression, expr.Delay) else "pipeline"
            hints.setdefault((kind, expression.instance), name)
    requests: dict[tuple[str, int, int], _Request] = {}
    owner_identity = _local_owner_identity(module)
    visited: set[int] = set()
    pending = list(reversed(module_expression_roots(module)))
    while pending:
        value = pending.pop()
        if id(value) in visited:
            continue
        visited.add(id(value))
        if isinstance(value, (expr.Delay, expr.Pipeline)):
            kind = "delay" if isinstance(value, expr.Delay) else "pipeline"
            count = value.cycles if isinstance(value, expr.Delay) else value.stages
            short_kind = "pipe" if kind == "pipeline" else kind
            base = hints.get((kind, value.instance))
            for index in range(1, count + 1):
                physical_instance = (
                    value.instance + index - 1
                    if (
                        kind == "pipeline"
                        and value.pipeline_plan is None
                        and count > 1
                    )
                    else value.instance
                )
                physical_index = 1 if physical_instance != value.instance else index
                key = (kind, physical_instance, physical_index)
                preferred = (
                    f"{base}_{short_kind}_s{index}"
                    if base is not None
                    else f"zlang_{short_kind}_{physical_instance}_s{physical_index}"
                )
                requests[key] = _Request("stage", tuple(map(str, key)), f"{owner_identity}:stage:{key}", _private_base(preferred))
        pending.extend(reversed(expression_children(value, policy=ExpressionTraversalPolicy.STRUCTURAL)))
    return tuple(requests.values())


def module_rtl_names(
    module: Module,
    *,
    identifier: Identifier = rtl_identifier,
    reserved: Iterable[str] = (),
) -> ModuleRtlNames:
    """Allocate one containing module's private names with source priority."""
    used = _module_reserved_names(module, identifier) | set(reserved)
    frozen_reserved = frozenset(used)
    owner_identity = _local_owner_identity(module)
    scalar_instances: list[_Request] = []
    array_instances: list[_Request] = []
    for item in module.elaborated_instances:
        source_name = item.instance.name
        identity = f"{owner_identity}:instance:{source_name}:{item.specialization_identity}"
        is_array = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\[[0-9]+\]", source_name) is not None
        preferred = re.sub(r"\[([0-9]+)\]", r"_\1", source_name) if is_array else source_name
        request = _Request("instance", (source_name,), identity, preferred)
        (array_instances if is_array else scalar_instances).append(request)
    # Literal source instance `lane_0` wins over generated array spelling.
    entries = list(_allocate_requests(scalar_instances, used, identifier))
    entries.extend(_allocate_requests(array_instances, used, identifier))
    instance_names = {item.key[0]: item.physical_name for item in entries}
    requests: list[_Request] = []
    for child, item in zip(module.children, module.elaborated_instances, strict=True):
        owner = item.instance.name
        identity = f"{owner_identity}:instance:{owner}:{item.specialization_identity}"
        for role in ("result", "component_output", "component_input", "csr_result"):
            requests.append(_Request("instance_helper", (owner, role), f"{identity}:helper:{role}", _private_base(f"{instance_names[owner]}_{role}")))
        for port in _child_port_names(child):
            requests.append(_Request("child_signal", (owner, port), f"{identity}:port:{port}", _private_base(f"{instance_names[owner]}_{port}")))
    anonymous_index = 0
    for rule in module.rules:
        if re.fullmatch(r"__anonymous_rule_[0-9a-f]{16}", rule.name):
            label = f"when_{anonymous_index:02d}"
            anonymous_index += 1
        else:
            label = rule.name
        for role in ("guard", "fire"):
            requests.append(_Request("rule", (rule.name, role), f"{owner_identity}:rule:{rule.name}:{role}", _private_base(f"rule_{label}_{role}")))
    requests.extend(_stage_requests(module))
    entries.extend(_allocate_requests(requests, used, identifier))
    return ModuleRtlNames(module.name, tuple(entries), frozen_reserved)


def rtl_hierarchy_instance_path(
    hierarchy: HierarchyIndex,
    physical_path: tuple[str, ...],
    *,
    plans: Mapping[tuple[str, ...], ModuleRtlNames] | None = None,
    identifier: Identifier = rtl_identifier,
    reserved: Iterable[str] = (),
) -> tuple[str, ...]:
    """Resolve a semantic path through each actual parent namespace.

    Return the relative physical path below the public top.  A caller may pass
    its emission-local plans so manifests and VPI reuse the exact allocation.
    """
    hierarchy.at(physical_path)
    result: list[str] = []
    for length in range(len(hierarchy.root_path), len(physical_path)):
        parent_path = physical_path[:length]
        child_name = physical_path[length]
        hierarchy.child(parent_path, child_name)
        plan = plans[parent_path] if plans is not None else module_rtl_names(
            hierarchy.at(parent_path).module, identifier=identifier, reserved=reserved,
        )
        result.append(plan.instance(child_name))
    return tuple(result)


__all__ = [
    "RTL_NAMING_SCHEMA", "RtlNamingError", "ComponentPhysicalName",
    "ComponentNamePlan", "LocalPhysicalName", "ModuleRtlNames",
    "build_component_name_plan", "validate_component_name_plans",
    "module_rtl_names", "rtl_hierarchy_instance_path",
]
