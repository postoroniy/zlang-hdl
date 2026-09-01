"""Backend-independent, always-leaf public top-level ABI.

Semantic modules retain their nominal structs, vectors, and protocol objects.
This module projects only the *physical public boundary*: structs become named
field leaves, while vectors remain typed unpacked-array shapes. The packed
slice metadata is the exact bridge to backends which keep a private packed
core representation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import re

from zlang.ir.equivalence import signedness
from zlang.ir.expressions import Expression, StructConstruct, TupleConstruct
from zlang.ir.formal_observations import port_observation_id
from zlang.ir.interfaces import InterfaceProtocol, RequestResponseRole
from zlang.ir.module import (
    AggregateProtocolEndpoint,
    Module,
    Port,
    PortDirection,
    RequestResponseInterface,
)
from zlang.ir.packing import is_bit_packable, packed_width
from zlang.ir.physical_types import physical_width
from zlang.ir.types import (
    BitType,
    HardwareType,
    StructType,
    TupleType,
    UIntType,
    VecType,
)
from zlang.source import SourceOrigin


class TopAggregateABIError(ValueError):
    """A top-level value cannot be represented by the public leaf ABI."""


@dataclass(frozen=True)
class PackedElementSlice:
    """One public array element's inclusive range in its private packed root."""

    indices: tuple[int, ...]
    msb: int
    lsb: int

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1


@dataclass(frozen=True)
class ExternalProtocolLeaf:
    """One typed public top leaf.

    The historical name is retained as a compatibility API. Ordinary ports,
    clocks, resets, request/response channels, and aggregate protocol members
    now use the same representation. ``aggregate_id`` is the containing
    semantic object identity; it is not necessarily an aggregate protocol.
    """

    aggregate_id: str
    protocol_specialization_id: str | None
    role: str | None
    member_path: tuple[str, ...]
    leaf_semantic_id: str
    signal_kind: str
    canonical_type: HardwareType
    ownership: str | None
    direction: PortDirection
    clock_domain: str | None
    reset_domain: str | None
    external_name: str
    source_origin: SourceOrigin | None = None
    category: str = "aggregate"
    packed_root_semantic_id: str | None = None
    packed_root_external_name: str | None = None
    packed_root_type: HardwareType | None = None
    packed_msb: int | None = None
    packed_lsb: int | None = None
    array_dimensions: tuple[int, ...] = ()
    packed_element_slices: tuple[PackedElementSlice, ...] = ()

    @property
    def width(self) -> int:
        return _width(self.canonical_type)

    @property
    def signedness(self) -> str:
        return signedness(self.canonical_type)

    @property
    def element_type(self) -> HardwareType:
        type_ = self.canonical_type
        while isinstance(type_, VecType):
            type_ = type_.element_type
        return type_

    @property
    def element_width(self) -> int:
        return _width(self.element_type)


# The clearer general name is preferred by new callers. Keeping a true alias
# makes isinstance checks and the old aggregate-only API remain compatible.
ExternalTopLeaf = ExternalProtocolLeaf


@dataclass(frozen=True)
class TopAggregateABI:
    """Compatibility view containing aggregate-protocol leaves only."""

    module: str
    leaves: tuple[ExternalProtocolLeaf, ...]

    @property
    def endpoints(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(leaf.aggregate_id for leaf in self.leaves))

    @property
    def inputs(self) -> tuple[ExternalProtocolLeaf, ...]:
        return tuple(leaf for leaf in self.leaves if leaf.direction is PortDirection.INPUT)

    @property
    def outputs(self) -> tuple[ExternalProtocolLeaf, ...]:
        return tuple(leaf for leaf in self.leaves if leaf.direction is PortDirection.OUTPUT)


@dataclass(frozen=True)
class TopPhysicalABI:
    """Complete, deterministic public top-level leaf contract."""

    module: str
    leaves: tuple[ExternalTopLeaf, ...]

    @property
    def inputs(self) -> tuple[ExternalTopLeaf, ...]:
        return tuple(leaf for leaf in self.leaves if leaf.direction is PortDirection.INPUT)

    @property
    def outputs(self) -> tuple[ExternalTopLeaf, ...]:
        return tuple(leaf for leaf in self.leaves if leaf.direction is PortDirection.OUTPUT)

    @property
    def aggregate_leaves(self) -> tuple[ExternalTopLeaf, ...]:
        return tuple(leaf for leaf in self.leaves if leaf.category == "aggregate")


def build_top_physical_abi(module: Module) -> TopPhysicalABI:
    """Project every public top object into deterministic typed leaves."""

    leaves: list[ExternalTopLeaf] = []

    # A legacy module has one implicit domain. Multi-domain modules carry all
    # physical clocks/resets in ``clock_domains`` instead.
    domains: list[tuple[str, str, SourceOrigin | None]] = []
    if module.clock is not None and module.reset is not None:
        domains.append((module.clock, module.reset, None))
    for domain in module.clock_domains:
        item = (domain.clock, domain.reset, domain.source_origin)
        if not any(clock == item[0] and reset == item[1] for clock, reset, _ in domains):
            domains.append(item)
    seen_clocks: set[str] = set()
    seen_resets: set[str] = set()
    for clock, reset, origin in domains:
        if clock not in seen_clocks:
            leaves.append(_simple_leaf(
                semantic_id="clock" if clock == module.clock else f"clock:{clock}",
                path=(clock,), kind="clock", type_=BitType(),
                direction=PortDirection.INPUT, external_name=clock,
                clock_domain=clock, reset_domain=reset, origin=origin,
            ))
            seen_clocks.add(clock)
        if reset not in seen_resets:
            leaves.append(_simple_leaf(
                semantic_id="reset" if reset == module.reset else f"reset:{reset}",
                path=(reset,), kind="reset", type_=BitType(),
                direction=PortDirection.INPUT, external_name=reset,
                clock_domain=clock, reset_domain=reset, origin=origin,
            ))
            seen_resets.add(reset)

    synthetic_aggregate_ports = {
        f"{endpoint.name}__{member.name}"
        for endpoint in module.aggregate_protocol_endpoints
        for member in endpoint.members
    }
    for port in module.ports:
        if port.name in synthetic_aggregate_ports:
            continue
        leaves.extend(_port_leaves(module, port))

    for interface in module.request_responses:
        leaves.extend(_request_response_leaves(module, interface))

    for endpoint in module.aggregate_protocol_endpoints:
        leaves.extend(_aggregate_leaves(module, endpoint))

    _validate_external_names(leaves)
    return TopPhysicalABI(module.name, tuple(leaves))


def build_top_aggregate_abi(module: Module) -> TopAggregateABI:
    """Compatibility projection of explicitly declared aggregate endpoints."""

    leaves = tuple(
        leaf for leaf in build_top_physical_abi(module).leaves
        if leaf.category == "aggregate"
    )
    return TopAggregateABI(module.name, leaves)


def _port_leaves(module: Module, port: Port) -> tuple[ExternalTopLeaf, ...]:
    semantic = port_observation_id(port.name)
    forward = port.direction
    reverse = _reverse(forward)
    domain = port.domain or module.clock
    root_expression = next(
        (
            assignment.expression
            for assignment in module.assignments
            if isinstance(assignment.target, Port)
            and assignment.target.name == port.name
            and assignment.signal is None
        ),
        None,
    )
    common = dict(
        container_id=semantic, category="port", role=None,
        specialization=port.protocol.value,
        clock_domain=domain, reset_domain=module.reset,
    )
    if port.protocol is InterfaceProtocol.WIRE:
        leaves = _typed_signal_leaves(
            type_=port.type, path=(port.name,), semantic_root=semantic,
            signal_kind="wire", direction=forward,
            ownership=_direction_owner(forward),
            packed_root_external_name=port.name,
            origin=(root_expression.origin if root_expression is not None else None),
            **common,
        )
        if root_expression is None:
            return leaves
        return tuple(
            replace(
                leaf,
                source_origin=_expression_member_origin(
                    root_expression, leaf.member_path[1:]
                ),
            )
            for leaf in leaves
        )

    signals: list[tuple[str, HardwareType, PortDirection]]
    if port.protocol is InterfaceProtocol.READY_VALID:
        signals = [("payload", port.type, forward), ("valid", BitType(), forward),
                   ("ready", BitType(), reverse)]
    elif port.protocol is InterfaceProtocol.CREDIT:
        signals = [("payload", port.type, forward), ("send", BitType(), forward),
                   ("return", BitType(), reverse)]
    elif port.protocol is InterfaceProtocol.PACKET:
        signals = [("payload", port.type, forward), ("valid", BitType(), forward),
                   ("last", BitType(), forward), ("ready", BitType(), reverse)]
    elif port.protocol is InterfaceProtocol.VC_CREDIT:
        vc_width = max(1, ((port.virtual_channels or 1) - 1).bit_length())
        signals = [("payload", port.type, forward), ("vc", UIntType(vc_width), forward),
                   ("send", BitType(), forward), ("return", BitType(), reverse),
                   ("return_vc", UIntType(vc_width), reverse)]
    else:  # pragma: no cover - closed enum, retained for defensive restoration
        raise TopAggregateABIError(
            f"top port '{port.name}' protocol '{port.protocol.value}' is unsupported"
        )
    result: list[ExternalTopLeaf] = []
    for signal, type_, direction in signals:
        signal_semantic = port_observation_id(port.name, signal)
        signal_expression = next(
            (
                assignment.expression
                for assignment in module.assignments
                if isinstance(assignment.target, Port)
                and assignment.target.name == port.name
                and assignment.signal is not None
                and assignment.signal.value == signal
            ),
            None,
        )
        signal_leaves = _typed_signal_leaves(
            type_=type_, path=(port.name, signal), semantic_root=signal_semantic,
            signal_kind=signal, direction=direction,
            ownership=_direction_owner(direction),
            packed_root_external_name=f"{port.name}_{signal}",
            origin=(
                signal_expression.origin if signal_expression is not None else None
            ),
            **common,
        )
        if signal_expression is not None:
            signal_leaves = tuple(
                replace(
                    leaf,
                    source_origin=_expression_member_origin(
                        signal_expression, leaf.member_path[2:]
                    ),
                )
                for leaf in signal_leaves
            )
        result.extend(signal_leaves)
    return tuple(result)


def _expression_member_origin(
    expression: Expression,
    path: tuple[str, ...],
) -> SourceOrigin | None:
    """Return the narrowest source origin represented by one public leaf."""

    current = expression
    remaining = path
    while remaining:
        segment = remaining[0]
        if isinstance(current, TupleConstruct) and segment.startswith("item"):
            suffix = segment[4:]
            if suffix.isdigit() and int(suffix) < len(current.elements):
                current = current.elements[int(suffix)]
                remaining = remaining[1:]
                continue
        if isinstance(current, StructConstruct):
            field = next(
                (value for name, value in current.fields if name == segment),
                None,
            )
            if field is not None:
                current = field
                remaining = remaining[1:]
                continue
        break
    return current.origin or expression.origin


def _request_response_leaves(
    module: Module, interface: RequestResponseInterface
) -> tuple[ExternalTopLeaf, ...]:
    requester = interface.role is RequestResponseRole.REQUESTER
    interface_id = port_observation_id(interface.name)
    result: list[ExternalTopLeaf] = []
    for channel, signal, type_, implementation_owned in (
        ("request", "payload", interface.request_type, requester),
        ("request", "valid", BitType(), requester),
        ("request", "ready", BitType(), not requester),
        ("response", "payload", interface.response_type, not requester),
        ("response", "valid", BitType(), not requester),
        ("response", "ready", BitType(), requester),
    ):
        direction = PortDirection.OUTPUT if implementation_owned else PortDirection.INPUT
        semantic = port_observation_id(interface.name, f"{channel}.{signal}")
        result.extend(_typed_signal_leaves(
            container_id=interface_id, category="request_response",
            specialization="request_response", role=interface.role.value,
            ownership=interface.role.value if implementation_owned else (
                RequestResponseRole.RESPONDER.value if requester else RequestResponseRole.REQUESTER.value
            ),
            type_=type_, path=(interface.name, channel, signal),
            semantic_root=semantic, signal_kind=signal,
            direction=direction,
            packed_root_external_name=f"{interface.name}_{channel}_{signal}",
            clock_domain=module.clock, reset_domain=module.reset, origin=None,
        ))
    return tuple(result)


def _aggregate_leaves(module: Module, endpoint: AggregateProtocolEndpoint) -> tuple[ExternalTopLeaf, ...]:
    aggregate_id = f"aggregate:{module.name}.{endpoint.name}"
    result: list[ExternalTopLeaf] = []
    for member in endpoint.members:
        member_path = (endpoint.name, member.name)
        source_direction = (
            PortDirection.OUTPUT if endpoint.role == member.source_role
            else PortDirection.INPUT
        )
        sink_direction = _reverse(source_direction)
        common = dict(
            container_id=aggregate_id, category="aggregate",
            specialization=endpoint.specialization_identity or endpoint.protocol,
            role=endpoint.role, clock_domain=endpoint.domain or member.domain or module.clock,
            reset_domain=module.reset, origin=None,
        )
        if member.protocol is InterfaceProtocol.READY_VALID:
            for signal, type_, direction, owner in (
                ("payload", member.payload_type, source_direction, member.source_role),
                ("valid", BitType(), source_direction, member.source_role),
                ("ready", BitType(), sink_direction, member.sink_role),
            ):
                semantic = aggregate_id + "." + member.name + "." + signal
                result.extend(_typed_signal_leaves(
                    type_=type_, path=member_path + (signal,), semantic_root=semantic,
                    signal_kind=signal, direction=direction, ownership=owner,
                    packed_root_external_name=f"{endpoint.name}__{member.name}_{signal}",
                    **common,
                ))
        elif member.protocol is InterfaceProtocol.WIRE:
            semantic = aggregate_id + "." + member.name
            result.extend(_typed_signal_leaves(
                type_=member.payload_type, path=member_path, semantic_root=semantic,
                signal_kind="wire", direction=source_direction,
                ownership=member.source_role,
                packed_root_external_name=f"{endpoint.name}__{member.name}", **common,
            ))
        else:
            raise TopAggregateABIError(
                f"top aggregate '{endpoint.name}.{member.name}' protocol "
                f"'{member.protocol.value}' is unsupported by the public ABI"
            )
    return tuple(result)


def _typed_signal_leaves(
    *, container_id: str, category: str, specialization: str | None,
    role: str | None, ownership: str | None, type_: HardwareType,
    path: tuple[str, ...], semantic_root: str, signal_kind: str,
    direction: PortDirection, packed_root_external_name: str,
    clock_domain: str | None, reset_domain: str | None,
    origin: SourceOrigin | None,
) -> tuple[ExternalTopLeaf, ...]:
    result: list[ExternalTopLeaf] = []
    for leaf_path, public_type, dimensions, slices in _public_layout(type_, path):
        suffix = leaf_path[len(path):]
        leaf_semantic = semantic_root + ("." + ".".join(suffix) if suffix else "")
        msb, lsb = _contiguous_bounds(slices)
        result.append(ExternalTopLeaf(
            aggregate_id=container_id,
            protocol_specialization_id=specialization,
            role=role,
            member_path=leaf_path,
            leaf_semantic_id=leaf_semantic,
            signal_kind=signal_kind,
            canonical_type=public_type,
            ownership=ownership,
            direction=direction,
            clock_domain=clock_domain,
            reset_domain=reset_domain,
            external_name=_external_leaf_name(type_, path, leaf_path),
            source_origin=origin,
            category=category,
            packed_root_semantic_id=semantic_root,
            packed_root_external_name=packed_root_external_name,
            packed_root_type=type_,
            packed_msb=msb,
            packed_lsb=lsb,
            array_dimensions=dimensions,
            packed_element_slices=slices,
        ))
    return tuple(result)


def _public_layout(
    type_: HardwareType, path: tuple[str, ...]
) -> tuple[tuple[tuple[str, ...], HardwareType, tuple[int, ...], tuple[PackedElementSlice, ...]], ...]:
    """Return source-order leaves with the exact packing.py MSB layout."""

    if is_bit_packable(type_):
        if packed_width(type_) != _width(type_):  # pragma: no cover - invariant
            raise TopAggregateABIError("public packing width disagrees with canonical type")

    groups: dict[tuple[tuple[str, ...], HardwareType, tuple[int, ...]], list[PackedElementSlice]] = {}
    order: list[tuple[tuple[str, ...], HardwareType, tuple[int, ...]]] = []

    def visit(current: HardwareType, current_path: tuple[str, ...], base_lsb: int,
              dimensions: tuple[int, ...], indices: tuple[int, ...]) -> None:
        if isinstance(current, StructType):
            if not current.fields:
                raise TopAggregateABIError(f"empty public struct at '{'.'.join(current_path)}'")
            cursor = base_lsb + _width(current)
            for field in current.fields:
                cursor -= _width(field.type)
                visit(field.type, current_path + (field.name,), cursor, dimensions, indices)
            return
        if isinstance(current, TupleType):
            cursor = base_lsb + _width(current)
            for index, element in enumerate(current.elements):
                cursor -= _width(element)
                visit(
                    element,
                    current_path + (f"item{index}",),
                    cursor,
                    dimensions,
                    indices,
                )
            return
        if isinstance(current, VecType):
            cursor = base_lsb + _width(current)
            for index in range(current.length):
                cursor -= _width(current.element_type)
                visit(current.element_type, current_path, cursor,
                      dimensions + (current.length,), indices + (index,))
            return
        key = (current_path, current, dimensions)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(PackedElementSlice(
            indices, base_lsb + _width(current) - 1, base_lsb
        ))

    visit(type_, path, 0, (), ())
    result = []
    for leaf_path, element_type, dimensions in order:
        public_type = element_type
        for length in reversed(dimensions):
            public_type = VecType(length, public_type)
        result.append((leaf_path, public_type, dimensions, tuple(groups[(leaf_path, element_type, dimensions)])))
    return tuple(result)


def _contiguous_bounds(slices: tuple[PackedElementSlice, ...]) -> tuple[int | None, int | None]:
    if not slices:
        return None, None
    ordered = sorted(slices, key=lambda item: item.lsb)
    if any(left.msb + 1 != right.lsb for left, right in zip(ordered, ordered[1:])):
        return None, None
    return max(item.msb for item in slices), min(item.lsb for item in slices)


def _simple_leaf(
    *, semantic_id: str, path: tuple[str, ...], kind: str,
    type_: HardwareType, direction: PortDirection, external_name: str,
    clock_domain: str | None, reset_domain: str | None,
    origin: SourceOrigin | None,
) -> ExternalTopLeaf:
    width = _width(type_)
    return ExternalTopLeaf(
        semantic_id, None, None, path, semantic_id, kind, type_, "environment",
        direction, clock_domain, reset_domain, external_name, origin, kind,
        semantic_id, external_name, type_, width - 1, 0, (),
        (PackedElementSlice((), width - 1, 0),),
    )


def _reverse(direction: PortDirection) -> PortDirection:
    return PortDirection.OUTPUT if direction is PortDirection.INPUT else PortDirection.INPUT


def _direction_owner(direction: PortDirection) -> str:
    return "environment" if direction is PortDirection.INPUT else "module"


def _validate_external_names(leaves: list[ExternalTopLeaf]) -> None:
    names: dict[str, ExternalTopLeaf] = {}
    for leaf in leaves:
        previous = names.get(leaf.external_name)
        if previous is not None:
            raise TopAggregateABIError(
                f"top public leaf name collision '{leaf.external_name}' between "
                f"'{previous.leaf_semantic_id}' and '{leaf.leaf_semantic_id}'"
            )
        names[leaf.external_name] = leaf


def _encode_segment(segment: str) -> str:
    encoded = re.sub(r"[^A-Za-z0-9_]", "_", segment)
    encoded = re.sub(r"_+", "_", encoded).strip("_") or "_"
    if encoded[0].isdigit():
        encoded = "_" + encoded
    return encoded


def _external_leaf_name(
    root_type: HardwareType,
    root_path: tuple[str, ...],
    leaf_path: tuple[str, ...],
) -> str:
    """Render typed path separators without guessing positional field names."""

    if leaf_path[:len(root_path)] != root_path:
        raise TopAggregateABIError("public leaf path does not extend its root path")
    rendered = "_".join(_encode_segment(part) for part in root_path)
    current = root_type
    for part in leaf_path[len(root_path):]:
        while isinstance(current, VecType):
            current = current.element_type
        if isinstance(current, StructType):
            field = current.field(part)
            if field is None:
                raise TopAggregateABIError(
                    f"unknown public struct field '{part}' in {current}"
                )
            rendered += "_" + _encode_segment(part)
            current = field.type
            continue
        if isinstance(current, TupleType):
            match = re.fullmatch(r"item([0-9]+)", part)
            if match is None or int(match.group(1)) >= len(current.elements):
                raise TopAggregateABIError(
                    f"invalid public tuple component '{part}' in {current}"
                )
            index = int(match.group(1))
            rendered += "__" + _encode_segment(part)
            current = current.elements[index]
            continue
        raise TopAggregateABIError(
            f"public leaf path '{'.'.join(leaf_path)}' traverses scalar {current}"
        )
    return rendered


def _width(type_: HardwareType) -> int:
    try:
        return physical_width(type_)
    except ValueError as error:
        raise TopAggregateABIError(
            f"unsupported top public leaf type: {type_!r}"
        ) from error


__all__ = [
    "ExternalProtocolLeaf",
    "ExternalTopLeaf",
    "PackedElementSlice",
    "TopAggregateABI",
    "TopPhysicalABI",
    "TopAggregateABIError",
    "build_top_aggregate_abi",
    "build_top_physical_abi",
]
