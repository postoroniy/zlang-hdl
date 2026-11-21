# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bit-exact references and bound sources for hierarchy dependency queries."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import NamedTuple
from zlang.ir import expressions as ir_expr
from zlang.ir import storage as ir_storage
from zlang.ir.interfaces import ReadyValidSignal, ready_valid_field_name, request_response_field_name
from zlang.ir.traversal import expression_children
from zlang.ir.static_projection import StaticView, project_static_bits, static_parts, static_view
from zlang.ir.types import UIntType
from zlang.source import SourceOrigin
from .errors import SemanticError


class _DependencyNode(NamedTuple):
    kind: str
    name: str
    field: str
    read_bits: tuple[int, ...] = ()


class _InputDependency(NamedTuple):
    name: str
    bits: tuple[int, ...] = ()


class _StateClock(NamedTuple):
    """A clocked leaf, with no current-cycle path to the state writer."""

    domain: str


_OutputSource = _InputDependency | _StateClock


def _project_dependency(
    expression: ir_expr.Expression,
    bits: tuple[int, ...],
    locals_: dict[str, ir_expr.Expression],
) -> ir_expr.Expression:
    if not bits:
        return expression
    msb, lsb = bits
    return project_static_bits(StaticView(expression, msb, lsb), locals_, UIntType(msb - lsb + 1))


def references(
    expression: ir_expr.Expression | _StateClock,
    *,
    local_expressions: dict[str, ir_expr.Expression],
    storage_controls: dict[_DependencyNode, tuple[ir_expr.Expression, ...]],
    known_inputs: set[str] | None = None,
    signal_sources: dict[str, tuple[ir_expr.Expression, ...]] | None = None,
    instance_inputs: Callable[
        [_DependencyNode], Iterable[ir_expr.Expression | _StateClock]
    ] | None = None,
    state_clock: Callable[[object], str | None] | None = None,
    active_locals: frozenset[str] = frozenset(),
) -> tuple[set[_OutputSource], dict[_DependencyNode, SourceOrigin | None]]:
    input_names: set[_OutputSource] = set()
    local_signals = signal_sources or {}
    outputs: dict[_DependencyNode, SourceOrigin | None] = {}
    def visit(
        value: object,
        active: frozenset[str],
        active_storage: frozenset[_DependencyNode] = frozenset(),
    ) -> None:
        if isinstance(value, _StateClock):
            input_names.add(value)
            return
        if state_clock is not None and (domain := state_clock(value)) is not None:
            input_names.add(_StateClock(domain))
        selected = static_view(value)
        if selected is not None:
            for item, msb, lsb in static_parts(selected, local_expressions, active):
                bits = () if lsb == 0 and msb == item.type.width - 1 else (msb, lsb)
                if isinstance(item, ir_expr.InstanceOutputRef):
                    visit_child_output(item, bits, active, active_storage)
                elif isinstance(item, ir_expr.InputRef):
                    visit_name(item.name, active, active_storage, bits=bits)
                elif isinstance(item, (ir_expr.ReadyValidRef, ir_expr.RequestResponseRef)):
                    visit_protocol(item, active, active_storage, bits=bits)
                else:
                    visit(item, active, active_storage)
            return
        if isinstance(value, ir_expr.InputRef):
            visit_name(value.name, active, active_storage)
            return
        if isinstance(value, (ir_expr.ReadyValidRef, ir_expr.RequestResponseRef)):
            visit_protocol(value, active, active_storage)
            return
        if isinstance(value, ir_expr.InstanceOutputRef):
            visit_child_output(value, (), active, active_storage)
            return
        if isinstance(value, (ir_expr.MemoryRef, ir_expr.FifoRef)):
            if isinstance(value, ir_expr.MemoryRef):
                if value.signal is not ir_storage.MemorySignal.READ_DATA:
                    return
                node = _DependencyNode("memory", value.memory, value.port or "")
            else:
                node = _DependencyNode("fifo", value.fifo, value.signal.value)
            if node in storage_controls:
                if instance_inputs is None:
                    outputs[node] = value.origin
                elif node not in active_storage:
                    for control in storage_controls[node]:
                        visit(control, active, active_storage | {node})
            return
        if isinstance(
            value,
            (
                ir_expr.RegisterRef,
                ir_expr.RomRef,
                ir_expr.Delay,
                ir_expr.Pipeline,
                ir_expr.Constant,
                ir_expr.ParameterRef,
                ir_expr.FunctionalCaptureRef,
                ir_expr.FunctionalValue,
                ir_expr.FunctionalTableLookup,
            ),
        ):
            return
        if isinstance(value, ir_expr.Expression):
            for child in expression_children(value):
                visit(
                    child,
                    active,
                    active_storage,
                )

    def visit_name(
        name: str,
        active: frozenset[str],
        active_storage: frozenset[_DependencyNode],
        *,
        bits: tuple[int, ...] = (),
    ) -> None:
        replacement = local_expressions.get(name)
        controls = (
            (replacement,) if replacement is not None
            else local_signals.get(name, ())
        )
        if controls:
            if name in active:
                raise SemanticError(
                    f"cyclic local dependency '{name}' in child dependency analysis"
                )
            for control in controls:
                visit(_project_dependency(control, bits, {}), active | {name}, active_storage)
        elif known_inputs is None or name in known_inputs:
            input_names.add(_InputDependency(name, bits))

    def visit_protocol(
        value: ir_expr.ReadyValidRef | ir_expr.RequestResponseRef,
        active: frozenset[str],
        active_storage: frozenset[_DependencyNode],
        *,
        bits: tuple[int, ...] = (),
    ) -> None:
        signals = (
            (ReadyValidSignal.VALID, ReadyValidSignal.READY)
            if value.signal is ReadyValidSignal.TRANSFER else (value.signal,)
        )
        for signal in signals:
            name = (
                request_response_field_name(value.interface, value.channel, signal)
                if isinstance(value, ir_expr.RequestResponseRef)
                else ready_valid_field_name(value.interface, signal)
            )
            visit_name(name, active, active_storage, bits=bits)

    def visit_child_output(
        reference: ir_expr.InstanceOutputRef,
        bits: tuple[int, ...],
        active: frozenset[str],
        active_storage: frozenset[_DependencyNode],
    ) -> None:
        node = _DependencyNode("child", reference.instance, reference.port, bits)
        if state_clock is not None and reference.domain is not None:
            input_names.add(_StateClock(reference.domain))
        if instance_inputs is None:
            outputs[node] = reference.origin
        else:
            for binding in instance_inputs(node):
                visit(binding, active, active_storage)

    visit(expression, active_locals)
    return input_names, outputs


def bound_sources(
    output: _DependencyNode,
    summary: dict[str, frozenset[_OutputSource]],
    bindings: dict[tuple[str, str], ir_expr.Expression],
    locals_: dict[str, ir_expr.Expression],
) -> Iterable[ir_expr.Expression | _StateClock]:
    for source in summary.get(output.field, ()):
        if isinstance(source, _StateClock):
            yield source
        elif (binding := bindings.get((output.name, source.name))) is not None:
            yield _project_dependency(binding, source.bits, locals_)
