# SPDX-License-Identifier: Apache-2.0
"""Derived, read-only observation ports for simulation I/O execution.

The original selected DUT is never changed. Probes are compiler expressions
executed by the existing native VM and by the ordinary Direct-SV backend.
"""

from dataclasses import dataclass, replace

from zlang.ir import expressions as ex
from zlang.ir.expressions import Pack
from zlang.ir.module import Assignment, Module, Port, PortDirection
from zlang.ir.types import BitType, BitsType
from zlang.simulation_io_model import IODrive, IOFinish, IORead, IOWrite, IOWhen
from zlang.simulation_io_targets import input_location
from zlang.simulation_plan_policy import MAX_PLAN_EVENTS
from zlang.simulation_rewrite import SimulationExpressionRewriter
from zlang.simulation_values import SimulationRuntimeError


@dataclass(frozen=True)
class SimulationIOProduct:
    block: object
    module: Module
    probes: tuple[tuple[object, str], ...]
    inputs: tuple[tuple[str, object], ...]
    outputs: tuple[tuple[str, tuple], ...]
    locations: tuple[tuple[object, object], ...]


class _ObservationResolver(SimulationExpressionRewriter):
    """Bind observations to selected output definitions, never input slots."""

    def __init__(self, module, aliases, source_locals):
        super().__init__()
        self._assignments = {
            (a.target.name, a.channel, a.signal): a.expression
            for a in module.assignments
        }
        for value in (*source_locals, *module.locals):
            self._assignments.setdefault((value.name, None, None), value.expression)
        self._registers = {r.name: r for r in module.registers}
        self._visiting = set()
        self._aliases = aliases

    def rewrite_special(self, value):
        key = None
        if isinstance(value, ex.InputRef):
            if value.name in self._aliases:
                return ex.InputRef(
                    self._aliases[value.name], value.type, origin=value.origin
                )
            if value.name in self._registers:
                return ex.RegisterRef(value.name, value.type, origin=value.origin)
            key = value.name, None, None
        if key in self._assignments:
            if key in self._visiting:
                raise SimulationRuntimeError("cyclic simulation observation")
            self._visiting.add(key)
            result = self.expression(self._assignments[key])
            self._visiting.remove(key)
            return result
        return None


def build_io_product(
    module: Module, blocks, name: str, source_locals=()
) -> SimulationIOProduct:
    if module.arbiters:
        raise SimulationRuntimeError(
            "simulation I/O activation is unsupported for packet-arbiter tops; "
            "their specialized backend accepts only arbiter endpoints"
        )
    matches = [b for b in blocks if b.name == name]
    if len(matches) != 1:
        raise SimulationRuntimeError(f"unknown simulation block '{name}'")
    block = matches[0]
    ports = list(module.ports)
    assignments = list(module.assignments)
    used = {p.name for p in ports} | {v.name for v in module.locals}
    used.update(r.name for r in module.registers)
    used.update(leaf.external_name for leaf in module.top_physical_abi.leaves)
    reset_aliases = {}
    resets = []
    for reset, original in block.reset_bindings:
        name = original
        while name in used:
            name += "_"
        used.add(name)
        reset_aliases[original] = name
        resets.append((reset, name))
    block = replace(block, reset_bindings=tuple(resets))
    probes = []
    inputs = []
    outputs = {}
    locations = []
    port_map = {p.name: p for p in module.ports}
    rr_map = {p.name: p for p in module.request_responses}
    count = 0
    resolver = _ObservationResolver(module, reset_aliases, source_locals)

    def observe(expression):
        name = f"zlang_simulation_probe_{len(probes)}"
        while name in used:
            name += "_"
        used.add(name)
        port = Port(PortDirection.OUTPUT, name, BitsType(expression.type.width))
        ports.append(port)
        assignments.append(
            Assignment(port, Pack(resolver.expression(expression), port.type))
        )
        probes.append((expression, name))

    def visit(commands):
        nonlocal count
        for item in commands:
            count += 1
            if count > MAX_PLAN_EVENTS:
                raise SimulationRuntimeError(
                    "simulation block exceeds instrumentation event limit"
                )
            if isinstance(item, IOWhen):
                observe(item.guard)
                visit(item.body)
            elif isinstance(item, IORead):
                if not inputs:
                    inputs.append((item.channel, item.bindings))
                for _, expression in item.bindings:
                    locations.append(
                        (expression, input_location(expression, port_map, rr_map))
                    )
                visit(item.eof)
            elif isinstance(item, IOWrite):
                outputs[item.channel] = item.bindings
                for _, expression in item.bindings:
                    observe(expression)
            elif isinstance(item, IODrive):
                observe(item.value)
                locations.append(
                    (item.target, input_location(item.target, port_map, rr_map))
                )
            elif not isinstance(item, IOFinish):
                raise SimulationRuntimeError("unknown simulation command")

    visit(block.commands)
    for _, name in block.reset_bindings:
        ports.append(Port(PortDirection.INPUT, name, BitType()))
    derived = replace(
        module, ports=tuple(ports), assignments=tuple(assignments), simulation_blocks=()
    )
    return SimulationIOProduct(
        block,
        derived,
        tuple(probes),
        tuple(inputs),
        tuple(outputs.items()),
        tuple(locations),
    )
