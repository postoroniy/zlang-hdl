"""Backend route selection for typed state and storage emission."""

from __future__ import annotations

from zlang.ir import interfaces as ir_interfaces
from zlang.ir import module as ir_module
from zlang.ir import state as ir_state


def uses_dedicated_legacy_storage(module: ir_module.Module) -> bool:
    """Return whether the frozen global-control storage emitter owns state."""

    transition = module.resolved_transition
    if (
        transition is None
        or transition.action_groups
        or module.registers
        or module.rules
        or module.next_assignments
        or module.elaborated_instances
        or module.children
        or module.connections
        or module.hierarchical_connections
        or module.request_response_connections
        or module.aggregate_protocol_connections
    ):
        return False
    resource_kinds = {resource.kind for resource in transition.resources}
    if (
        module.fifos
        and all(not fifo.scheduled for fifo in module.fifos)
        and not module.memories
        and not module.roms
    ):
        return resource_kinds <= {ir_state.StateResourceKind.FIFO}
    if (
        module.memories
        and all(not memory.scheduled for memory in module.memories)
        and not module.fifos
        and not module.roms
    ):
        return resource_kinds <= {ir_state.StateResourceKind.MEMORY}
    return False


def requires_unified_state(module: ir_module.Module) -> bool:
    """Return whether typed state needs the exact unified scheduler."""

    transition = module.resolved_transition
    if transition is None:
        return False
    if uses_dedicated_legacy_storage(module):
        return False
    if not (
        transition.resources
        or transition.action_groups
        or module.registers
        or module.rules
        or module.next_assignments
        or any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or module.roms
    ):
        return False
    if (
        not module.rules
        and not transition.action_groups
        and (module.registers or module.next_assignments)
    ):
        return True
    if (
        any(fifo.scheduled for fifo in module.fifos)
        or any(memory.scheduled for memory in module.memories)
        or bool(module.roms)
        or bool(module.request_responses)
        or bool(module.aggregate_protocol_endpoints)
        or any(
            port.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            for port in module.ports
        )
        or any(
            endpoint.protocol is not ir_interfaces.InterfaceProtocol.WIRE
            for endpoint in module.protocol_endpoints
        )
        or any(
            resource.kind is not ir_state.StateResourceKind.REGISTER
            for resource in transition.resources
        )
        or any(
            action.kind is not ir_state.StateActionKind.REGISTER_WRITE
            or action.activation is not None
            for group in transition.action_groups
            for action in group.actions
        )
    ):
        return True
    groups = transition.action_groups
    return any(
        ir_state.groups_may_conflict(left, right)
        and (len(left.actions) != 1 or len(right.actions) != 1)
        for index, left in enumerate(groups)
        for right in groups[index + 1 :]
    )


__all__ = ["requires_unified_state", "uses_dedicated_legacy_storage"]
