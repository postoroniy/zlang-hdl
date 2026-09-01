"""Exact bounded materialization of pure hierarchical scalar values.

This module does not flatten implementation RTL.  It derives an independent
semantic value expression for the deliberately small whole-root equivalence
surface: one combinational scalar child, connected through typed
``InstancePortBinding`` records, with no state, protocol, storage, array, or
nested hierarchy.  Formal consumers can therefore reuse the existing M36
same-cycle value relation without inventing a hierarchical refinement model.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace

from zlang.ir import expressions as expr
from zlang.ir.hierarchy import HierarchyEntry, HierarchyError, build_hierarchy_index
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Assignment, InstancePortBinding, Module, Port
from zlang.ir.types import (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    SIntType,
    UFixedType,
    UIntType,
)


class HierarchicalValueError(ValueError):
    """A module lies outside pure one-level scalar value materialization."""


_SCALAR_TYPES = (
    BitType,
    BitsType,
    EnumType,
    FixedType,
    SIntType,
    UFixedType,
    UIntType,
)


@dataclass(frozen=True)
class MaterializedHierarchicalValue:
    """One exact public output expression and its public scalar boundary."""

    output: Port
    inputs: tuple[Port, ...]
    expression: expr.Expression


@dataclass(frozen=True)
class _Frame:
    entry: HierarchyEntry
    parent: "_Frame | None" = None
    bindings: tuple[InstancePortBinding, ...] = ()


def _unsupported(module: Module, detail: str) -> HierarchicalValueError:
    return HierarchicalValueError(
        f"pure hierarchical value equivalence does not support module "
        f"'{module.name}': {detail}"
    )


def _validate_pure_module(module: Module, *, root: bool) -> None:
    if module.clock is not None or module.reset is not None or module.clock_domains:
        raise _unsupported(module, "clock/reset domains")
    if any(port.protocol is not InterfaceProtocol.WIRE for port in module.ports):
        raise _unsupported(module, "protocol ports")
    if any(not isinstance(port.type, _SCALAR_TYPES) for port in module.ports):
        raise _unsupported(module, "aggregate ports")
    if any(
        (
            module.registers,
            module.next_assignments,
            module.rules,
            module.rule_priorities,
            module.fifos,
            module.memories,
            module.roms,
            module.csr_blocks,
            module.request_responses,
            module.arbiters,
            module.connections,
            module.protocol_endpoints,
            module.hierarchical_connections,
            module.request_response_connections,
            module.aggregate_protocol_endpoints,
            module.aggregate_protocol_connections,
            module.elastic_pipeline_regions,
        )
    ):
        raise _unsupported(module, "state, storage, protocol, or connection entities")
    transition = module.resolved_transition
    if transition is not None and (
        transition.resources or transition.action_groups or transition.priorities
    ):
        raise _unsupported(module, "scheduled state transitions")
    if module.pipeline_explorations or module.architecture_explorations:
        raise _unsupported(module, "unresolved implementation exploration")
    if module.timing_contract is not None or module.output_timings:
        raise _unsupported(module, "public timing contracts")
    if any(
        assignment.signal is not None or assignment.channel is not None
        for assignment in module.assignments
    ):
        raise _unsupported(module, "non-wire assignments")
    if root:
        if len(module.elaborated_instances) != 1 or len(module.children) != 1:
            raise _unsupported(module, "requires exactly one physical child")
        instance = module.elaborated_instances[0].instance
        if instance.array_length is not None:
            raise _unsupported(module, "instance arrays")
    elif module.elaborated_instances or module.children or module.instances:
        raise _unsupported(module, "nested hierarchy")


def _unique_output_assignment(module: Module, output: str) -> Assignment:
    assignments = tuple(
        item
        for item in module.assignments
        if item.target.name == output
        and item.signal is None
        and item.channel is None
    )
    if len(assignments) != 1:
        raise _unsupported(
            module,
            f"output '{output}' requires exactly one scalar assignment",
        )
    return assignments[0]


def materialize_pure_hierarchical_output(
    module: Module,
    output: str,
    *,
    max_nodes: int = 100_000,
) -> MaterializedHierarchicalValue:
    """Resolve one public output through one exact pure scalar child.

    Resolution follows the validated hierarchy index and typed instance
    bindings.  Source names are never used to rediscover a specialization or a
    physical child.  The returned graph contains no ``InstanceOutputRef`` and
    is suitable for the existing independent M36 value emitter.
    """

    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or max_nodes < 1:
        raise HierarchicalValueError("hierarchical value node budget must be positive")
    try:
        hierarchy = build_hierarchy_index(module)
    except HierarchyError as error:
        raise HierarchicalValueError(str(error)) from error
    _validate_pure_module(module, root=True)
    child_entry = hierarchy.children_of(hierarchy.root_path)[0]
    _validate_pure_module(child_entry.module, root=False)

    output_port = next(
        (
            port
            for port in module.outputs
            if port.name == output and port.protocol is InterfaceProtocol.WIRE
        ),
        None,
    )
    if output_port is None:
        raise _unsupported(module, f"unknown public scalar output '{output}'")
    if not isinstance(output_port.type, _SCALAR_TYPES):
        raise _unsupported(module, f"output '{output}' is not scalar")

    root_frame = _Frame(hierarchy.root)
    child_name = child_entry.physical_name
    child_inputs = {port.name: port for port in child_entry.module.inputs}
    bindings = tuple(
        item for item in module.instance_bindings if item.instance == child_name
    )
    binding_names = tuple(item.port for item in bindings)
    if len(binding_names) != len(set(binding_names)):
        raise _unsupported(module, f"child '{child_name}' has duplicate input bindings")
    missing = tuple(sorted(set(child_inputs) - set(binding_names)))
    extra = tuple(sorted(set(binding_names) - set(child_inputs)))
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unknown " + ", ".join(extra))
        raise _unsupported(
            module,
            f"child '{child_name}' input bindings are incomplete: "
            + "; ".join(details),
        )
    child_frame = _Frame(child_entry, root_frame, bindings)
    frame_for_child = {child_name: child_frame}
    node_count = 0
    active_locals: set[tuple[tuple[str, ...], str]] = set()
    active_outputs: set[tuple[tuple[str, ...], str]] = set()

    def visit_value(value: object, frame: _Frame) -> object:
        if isinstance(value, expr.Expression):
            return visit(value, frame)
        if isinstance(value, tuple):
            return tuple(visit_value(item, frame) for item in value)
        if is_dataclass(value) and not isinstance(value, type):
            updates = {
                item.name: visit_value(getattr(value, item.name), frame)
                for item in fields(value)
                if item.init and item.name not in {"type", "origin"}
            }
            return replace(value, **updates) if updates else value
        return value

    def visit(value: expr.Expression, frame: _Frame) -> expr.Expression:
        nonlocal node_count
        node_count += 1
        if node_count > max_nodes:
            raise HierarchicalValueError(
                f"pure hierarchical value materialization exceeds {max_nodes} nodes"
            )
        current = frame.entry.module
        if isinstance(value, expr.InputRef):
            local = next((item for item in current.locals if item.name == value.name), None)
            if local is not None:
                key = (frame.entry.physical_path, value.name)
                if key in active_locals:
                    raise _unsupported(current, f"cyclic immutable local '{value.name}'")
                active_locals.add(key)
                try:
                    return visit(local.expression, frame)
                finally:
                    active_locals.remove(key)
            if frame.parent is not None:
                binding = next(
                    (item for item in frame.bindings if item.port == value.name),
                    None,
                )
                if binding is None:
                    raise _unsupported(
                        current,
                        f"child input '{value.name}' has no typed parent binding",
                    )
                return visit(binding.expression, frame.parent)
            if any(port.name == value.name for port in current.inputs):
                return value
            raise _unsupported(current, f"unresolved value '{value.name}'")
        if isinstance(value, expr.InstanceOutputRef):
            child = frame_for_child.get(value.instance)
            if frame is not root_frame or child is None:
                raise _unsupported(current, "nested or unknown child output reference")
            child_port = next(
                (
                    port
                    for port in child.entry.module.outputs
                    if port.name == value.port
                    and port.protocol is InterfaceProtocol.WIRE
                ),
                None,
            )
            if child_port is None or child_port.type != value.type:
                raise _unsupported(
                    current,
                    f"child output '{value.instance}.{value.port}' is missing or mistyped",
                )
            key = (child.entry.physical_path, value.port)
            if key in active_outputs:
                raise _unsupported(current, "cyclic child output dependency")
            active_outputs.add(key)
            try:
                assignment = _unique_output_assignment(child.entry.module, value.port)
                return visit(assignment.expression, child)
            finally:
                active_outputs.remove(key)
        if isinstance(
            value,
            (
                expr.ParameterRef,
                expr.RegisterRef,
                expr.ReadyValidRef,
                expr.CreditRef,
                expr.PacketRef,
                expr.VirtualChannelCreditRef,
                expr.RequestResponseRef,
                expr.FifoRef,
                expr.MemoryRef,
                expr.RomRef,
                expr.Delay,
                expr.Pipeline,
            ),
        ):
            raise _unsupported(
                current,
                f"expression '{type(value).__name__}' is not pure same-cycle scalar logic",
            )
        updates = {
            item.name: visit_value(getattr(value, item.name), frame)
            for item in fields(value)
            if item.init and item.name not in {"type", "origin"}
        }
        return replace(value, **updates) if updates else value

    assignment = _unique_output_assignment(module, output)
    expression = visit(assignment.expression, root_frame)
    if expression.type != output_port.type:
        raise _unsupported(module, f"materialized output '{output}' changed type")
    return MaterializedHierarchicalValue(output_port, module.inputs, expression)


__all__ = [
    "HierarchicalValueError",
    "MaterializedHierarchicalValue",
    "materialize_pure_hierarchical_output",
]
