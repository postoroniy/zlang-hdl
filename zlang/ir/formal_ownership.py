"""Hierarchy-aware ownership for already-existing recursive assumptions.

The source module that defines an M35 property sees only its local port
direction.  After elaboration, however, a locally environment-owned input may
be driven by a parent port, an internal sibling, or a connection-owned buffer.
This module resolves that physical ownership from typed hierarchy metadata.  It
does not inspect RTL names and it does not create a compositional proof rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.ir.expressions import InputRef
from zlang.ir.hierarchy import HierarchyIndex, build_hierarchy_index
from zlang.ir.interfaces import (
    CreditSignal,
    InterfaceProtocol,
    PacketSignal,
    ReadyValidSignal,
    VirtualChannelCreditSignal,
)
from zlang.ir.module import HierarchicalConnection, Module, Port, PortDirection


class RecursiveAssumptionDisposition(str, Enum):
    """How one child-local assumption participates in a whole-design proof."""

    EXTERNAL = "external"
    INTERNAL_GUARANTEED = "internal_guaranteed"
    INTERNAL_UNRESOLVED = "internal_unresolved"


@dataclass(frozen=True)
class RecursiveAssumptionOwnership:
    disposition: RecursiveAssumptionDisposition
    controlled_leaves: tuple[str, ...]
    root_leaves: tuple[str, ...] = ()
    guarantee_path: tuple[str, ...] | None = None
    guarantee_port: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.controlled_leaves:
            raise ValueError("recursive assumption ownership requires a controlled leaf")
        if self.disposition is RecursiveAssumptionDisposition.EXTERNAL:
            if not self.root_leaves or self.reason is not None:
                raise ValueError("external recursive ownership requires exact root leaves")
        elif self.disposition is RecursiveAssumptionDisposition.INTERNAL_GUARANTEED:
            if self.guarantee_path is None or self.guarantee_port is None:
                raise ValueError("discharged recursive ownership requires a guarantee endpoint")
            if self.reason is not None:
                raise ValueError("discharged recursive ownership cannot carry a failure reason")
        elif not self.reason:
            raise ValueError("unresolved recursive ownership requires a reason")


@dataclass(frozen=True)
class _LeafResolution:
    disposition: RecursiveAssumptionDisposition
    root_leaf: str | None = None
    guarantee_path: tuple[str, ...] | None = None
    guarantee_port: str | None = None
    reason: str | None = None


def _port_leaf(module: Module, local_id: str) -> tuple[Port, str | None] | None:
    matches = tuple(
        (port, local_id[len(f"port:{port.name}") + 1 :] or None)
        for port in module.ports
        if local_id == f"port:{port.name}"
        or local_id.startswith(f"port:{port.name}.")
    )
    if len(matches) != 1:
        return None
    return matches[0]


def _root_environment_controls(port: Port, signal: str | None) -> bool:
    if port.protocol is InterfaceProtocol.WIRE:
        return signal is None and port.direction is PortDirection.INPUT
    if port.protocol is InterfaceProtocol.READY_VALID:
        forward = {ReadyValidSignal.PAYLOAD.value, ReadyValidSignal.VALID.value}
        return (
            signal in forward
            if port.direction is PortDirection.INPUT
            else signal == ReadyValidSignal.READY.value
        )
    if port.protocol is InterfaceProtocol.PACKET:
        forward = {
            PacketSignal.PAYLOAD.value,
            PacketSignal.VALID.value,
            PacketSignal.LAST.value,
        }
        return (
            signal in forward
            if port.direction is PortDirection.INPUT
            else signal == PacketSignal.READY.value
        )
    if port.protocol is InterfaceProtocol.CREDIT:
        forward = {CreditSignal.PAYLOAD.value, CreditSignal.SEND.value}
        return (
            signal in forward
            if port.direction is PortDirection.INPUT
            else signal == CreditSignal.RETURN.value
        )
    if port.protocol is InterfaceProtocol.VC_CREDIT:
        forward = {
            VirtualChannelCreditSignal.PAYLOAD.value,
            VirtualChannelCreditSignal.VC.value,
            VirtualChannelCreditSignal.SEND.value,
        }
        backward = {
            VirtualChannelCreditSignal.RETURN.value,
            VirtualChannelCreditSignal.RETURN_VC.value,
        }
        return signal in (
            forward if port.direction is PortDirection.INPUT else backward
        )
    return False


def _is_forward(port: Port, signal: str | None) -> bool | None:
    if port.protocol is InterfaceProtocol.READY_VALID:
        if signal in {ReadyValidSignal.PAYLOAD.value, ReadyValidSignal.VALID.value}:
            return True
        if signal == ReadyValidSignal.READY.value:
            return False
    elif port.protocol is InterfaceProtocol.PACKET:
        if signal in {
            PacketSignal.PAYLOAD.value,
            PacketSignal.VALID.value,
            PacketSignal.LAST.value,
        }:
            return True
        if signal == PacketSignal.READY.value:
            return False
    elif port.protocol is InterfaceProtocol.CREDIT:
        if signal in {CreditSignal.PAYLOAD.value, CreditSignal.SEND.value}:
            return True
        if signal == CreditSignal.RETURN.value:
            return False
    elif port.protocol is InterfaceProtocol.VC_CREDIT:
        if signal in {
            VirtualChannelCreditSignal.PAYLOAD.value,
            VirtualChannelCreditSignal.VC.value,
            VirtualChannelCreditSignal.SEND.value,
        }:
            return True
        if signal in {
            VirtualChannelCreditSignal.RETURN.value,
            VirtualChannelCreditSignal.RETURN_VC.value,
        }:
            return False
    return None


def _connection_driver(
    connection: HierarchicalConnection,
    *,
    owner: str,
    port: str,
    forward: bool,
) -> object | None:
    local = connection.destination if forward else connection.source
    if local.owner != owner or local.name != port:
        return None
    return connection.source if forward else connection.destination


def _trace_protocol_leaf(
    hierarchy: HierarchyIndex,
    path: tuple[str, ...],
    port: Port,
    signal: str,
) -> _LeafResolution:
    current_path = path
    current_port = port
    current_signal = signal
    while True:
        if current_path == hierarchy.root_path:
            if _root_environment_controls(current_port, current_signal):
                return _LeafResolution(
                    RecursiveAssumptionDisposition.EXTERNAL,
                    root_leaf=f"port:{current_port.name}.{current_signal}",
                )
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    f"root protocol leaf '{current_port.name}.{current_signal}' "
                    "is implementation-owned"
                ),
            )

        forward = _is_forward(current_port, current_signal)
        if forward is None:
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    f"protocol leaf '{current_port.name}.{current_signal}' has no "
                    "typed physical direction"
                ),
            )
        parent_path = current_path[:-1]
        owner = current_path[-1]
        parent = hierarchy.at(parent_path).module
        matches = tuple(
            (connection, _connection_driver(
                connection,
                owner=owner,
                port=current_port.name,
                forward=forward,
            ))
            for connection in parent.hierarchical_connections
        )
        matches = tuple((connection, driver) for connection, driver in matches if driver is not None)
        if len(matches) != 1:
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    "typed hierarchy has no unique driver for protocol leaf "
                    f"'{'.'.join(current_path)}.{current_port.name}.{current_signal}'"
                ),
            )
        connection, driver = matches[0]
        if (
            connection.buffer_depth
            or connection.request_buffer_depth
            or connection.response_buffer_depth
            or connection.adapter is not None
            or connection.crossing is not None
        ):
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    "connection-owned buffering, adaptation, or crossing drives "
                    f"'{'.'.join(current_path)}.{current_port.name}.{current_signal}' "
                    "without an existing exact M35 endpoint guarantee"
                ),
            )

        if driver.owner == parent.name:
            parent_port = next(
                (item for item in parent.ports if item.name == driver.name), None
            )
            if parent_port is None or parent_port.protocol is not current_port.protocol:
                return _LeafResolution(
                    RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                    reason=(
                        f"parent endpoint '{parent.name}.{driver.name}' has no exact "
                        "typed protocol port"
                    ),
                )
            current_path = parent_path
            current_port = parent_port
            continue

        try:
            sibling = hierarchy.child(parent_path, driver.owner)
        except ValueError as error:
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=str(error),
            )
        sibling_port = next(
            (item for item in sibling.module.ports if item.name == driver.name), None
        )
        if sibling_port is None:
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    f"internal endpoint '{driver.owner}.{driver.name}' has no "
                    "typed source port"
                ),
            )
        if (
            current_port.protocol is InterfaceProtocol.READY_VALID
            and forward
            and sibling_port.direction is PortDirection.OUTPUT
        ):
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_GUARANTEED,
                guarantee_path=sibling.physical_path,
                guarantee_port=sibling_port.name,
            )
        return _LeafResolution(
            RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
            reason=(
                "internally driven protocol leaf has no exact existing M35 "
                f"guarantee: {'.'.join(sibling.physical_path)}.{sibling_port.name}"
            ),
        )


def _trace_wire_leaf(
    hierarchy: HierarchyIndex,
    path: tuple[str, ...],
    port: Port,
) -> _LeafResolution:
    current_path = path
    current_port = port
    while True:
        if current_path == hierarchy.root_path:
            if _root_environment_controls(current_port, None):
                return _LeafResolution(
                    RecursiveAssumptionDisposition.EXTERNAL,
                    root_leaf=f"port:{current_port.name}",
                )
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=f"root scalar port '{current_port.name}' is implementation-owned",
            )
        parent_path = current_path[:-1]
        owner = current_path[-1]
        parent = hierarchy.at(parent_path).module
        bindings = tuple(
            item for item in parent.instance_bindings
            if item.instance == owner and item.port == current_port.name
        )
        if len(bindings) != 1:
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    "typed hierarchy has no unique scalar binding for "
                    f"'{'.'.join(current_path)}.{current_port.name}'"
                ),
            )
        expression = bindings[0].expression
        if not isinstance(expression, InputRef):
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    "scalar assumption is driven by an implementation expression, "
                    "not one exact parent input"
                ),
            )
        parent_port = next(
            (item for item in parent.ports if item.name == expression.name), None
        )
        if (
            parent_port is None
            or parent_port.protocol is not InterfaceProtocol.WIRE
            or parent_port.type != current_port.type
        ):
            return _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=(
                    f"scalar binding '{expression.name}' is not an exact typed "
                    "parent wire port"
                ),
            )
        current_path = parent_path
        current_port = parent_port


def resolve_recursive_assumption_ownership(
    module: Module,
    physical_path: tuple[str, ...],
    controlled_local_ids: tuple[str, ...],
    *,
    automatic_protocol: bool,
    hierarchy: HierarchyIndex | None = None,
) -> RecursiveAssumptionOwnership:
    """Resolve one assumption's controlled leaves to the root physical ABI.

    ``controlled_local_ids`` deliberately excludes clock/reset and, for an
    automatic endpoint property, implementation-owned condition signals such
    as ready.  User-authored assumptions pass every referenced local leaf and
    therefore cannot be silently discharged by an internal endpoint guarantee.
    """

    index = hierarchy or build_hierarchy_index(module)
    entry = index.at(physical_path)
    resolutions: list[_LeafResolution] = []
    for local_id in controlled_local_ids:
        resolved_port = _port_leaf(entry.module, local_id)
        if resolved_port is None:
            resolutions.append(_LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=f"assumption leaf '{local_id}' is not a typed port leaf",
            ))
            continue
        port, signal = resolved_port
        if port.protocol is InterfaceProtocol.WIRE:
            resolution = _trace_wire_leaf(index, physical_path, port)
        elif signal is None:
            resolution = _LeafResolution(
                RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
                reason=f"protocol assumption leaf '{local_id}' omits its member signal",
            )
        else:
            resolution = _trace_protocol_leaf(
                index, physical_path, port, signal
            )
        resolutions.append(resolution)

    if all(
        item.disposition is RecursiveAssumptionDisposition.EXTERNAL
        for item in resolutions
    ):
        return RecursiveAssumptionOwnership(
            RecursiveAssumptionDisposition.EXTERNAL,
            controlled_local_ids,
            tuple(dict.fromkeys(
                item.root_leaf for item in resolutions if item.root_leaf is not None
            )),
        )

    guaranteed = tuple(
        item for item in resolutions
        if item.disposition is RecursiveAssumptionDisposition.INTERNAL_GUARANTEED
    )
    unresolved = tuple(
        item for item in resolutions
        if item.disposition is RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED
    )
    if (
        automatic_protocol
        and guaranteed
        and not unresolved
        and len(guaranteed) == len(resolutions)
        and len({(item.guarantee_path, item.guarantee_port) for item in guaranteed}) == 1
    ):
        endpoint = guaranteed[0]
        return RecursiveAssumptionOwnership(
            RecursiveAssumptionDisposition.INTERNAL_GUARANTEED,
            controlled_local_ids,
            guarantee_path=endpoint.guarantee_path,
            guarantee_port=endpoint.guarantee_port,
        )

    reasons = tuple(dict.fromkeys(
        item.reason
        or (
            "user-authored assumptions cannot be discharged by an internal "
            "protocol guarantee"
        )
        for item in resolutions
        if item.disposition is not RecursiveAssumptionDisposition.EXTERNAL
    ))
    return RecursiveAssumptionOwnership(
        RecursiveAssumptionDisposition.INTERNAL_UNRESOLVED,
        controlled_local_ids,
        reason="; ".join(reasons),
    )


__all__ = [
    "RecursiveAssumptionDisposition",
    "RecursiveAssumptionOwnership",
    "resolve_recursive_assumption_ownership",
]
