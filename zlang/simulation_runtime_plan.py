# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Runtime port, register, clock-domain, staged, and ROM plan construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zlang.ir import state as ir_state
from zlang.ir import types as ir_types
from zlang.opt.ir import ExpressionOp, TargetKind
from zlang.simulation_plan_encoding import (
    origin_payload as _origin_payload,
    type_payload as _type_payload,
    u64_limbs as _u64_limbs,
)
from zlang.simulation_plan_policy import (
    JitUnsupportedFeatureError,
    SimulationPlanError,
)


@dataclass(frozen=True)
class RuntimeStatePlanProduct:
    ports: list[dict[str, Any]]
    outputs: list[dict[str, Any]]
    registers: list[dict[str, Any]]
    domains: list[dict[str, Any]]
    direct_next: list[dict[str, Any]]


@dataclass
class RuntimeStatePlanBuilder:
    """Own public ports, scalar state, staged expressions, and ROM state."""

    _canonical: object
    _nodes: list[dict[str, Any]]
    _staged_expressions: list[dict[str, Any]]
    _rom_result_names: dict[str, str]
    _packed_register_initials: dict[str, int]

    def _ports_and_outputs(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        canonical = self._canonical
        ports = [
            {
                "name": port.name,
                "direction": port.direction.value,
                "type": _type_payload(port.type),
                "domain": port.domain,
            }
            for port in canonical.ports
        ]
        outputs = []
        for assignment in canonical.assignments:
            if assignment.target_kind is not TargetKind.PORT:
                raise JitUnsupportedFeatureError(
                    "native simulation supports only direct public-port assignments"
                )
            outputs.append(
                {"name": assignment.target_name, "node": assignment.expression}
            )
        assigned_outputs = {item["name"] for item in outputs}
        if canonical.resolved_transition is not None:
            for resource in canonical.resolved_transition.resources:
                if (
                    resource.kind is not ir_state.StateResourceKind.OUTPUT
                    or resource.name in assigned_outputs
                ):
                    continue
                zero_node = len(self._nodes)
                self._nodes.append(
                    {
                        "id": zero_node,
                        "op": ExpressionOp.CONSTANT.value,
                        "type": _type_payload(resource.type),
                        "operands": [],
                        "attributes": {
                            "limbs": _u64_limbs(0, resource.type.width)
                        },
                        "origins": [],
                    }
                )
                outputs.append({"name": resource.name, "node": zero_node})
                assigned_outputs.add(resource.name)
        return ports, outputs

    def _domains(self) -> list[dict[str, Any]]:
        canonical = self._canonical
        domains = [
            {
                "clock": domain.clock,
                "reset": domain.reset,
                "edge": domain.edge.value,
                "reset_mode": domain.reset_mode.value,
                "reset_polarity": domain.reset_polarity.value,
                "reset_release_mode": (
                    "synchronized"
                    if domain.reset_release_mode.value == "synchronized"
                    else "native"
                ),
                "reset_release_cycles": (
                    domain.reset_release_cycles
                    if domain.reset_release_mode.value == "synchronized"
                    else 0
                ),
            }
            for domain in canonical.clock_domains
        ]
        if canonical.clock and not domains:
            domains.append(
                {
                    "clock": canonical.clock,
                    "reset": canonical.reset,
                    "edge": "rising",
                    "reset_mode": "synchronous",
                    "reset_polarity": "active_high",
                    "reset_release_mode": "native",
                    "reset_release_cycles": 0,
                }
            )
        return domains

    def _append_staged_state(
        self,
        registers: list[dict[str, Any]],
        direct_next: list[dict[str, Any]],
    ) -> None:
        for staged in self._staged_expressions:
            type_ = staged["type"]
            assert isinstance(type_, ir_types.HardwareType)
            names = staged["names"]
            assert isinstance(names, list)
            zero_node = len(self._nodes)
            self._nodes.append(
                {
                    "id": zero_node,
                    "op": ExpressionOp.CONSTANT.value,
                    "type": _type_payload(type_),
                    "operands": [],
                    "attributes": {"limbs": _u64_limbs(0, type_.width)},
                    "origins": [],
                }
            )
            predecessor_nodes: list[int] = []
            for stage, name in enumerate(names):
                if stage == len(names) - 1:
                    reference_node = staged["final_node"]
                    assert isinstance(reference_node, int)
                else:
                    reference_node = len(self._nodes)
                    self._nodes.append(
                        {
                            "id": reference_node,
                            "op": ExpressionOp.REGISTER_REF.value,
                            "type": _type_payload(type_),
                            "operands": [],
                            "attributes": {"name": name},
                            "origins": [],
                        }
                    )
                predecessor_nodes.append(reference_node)
                registers.append(
                    {
                        "name": name,
                        "type": _type_payload(type_),
                        "initial": zero_node,
                        "initial_limbs": _u64_limbs(0, type_.width),
                        "domain": staged["domain"],
                    }
                )
                direct_next.append(
                    {
                        "target": name,
                        "node": (
                            staged["source"]
                            if stage == 0
                            else predecessor_nodes[stage - 1]
                        ),
                        "activation": None,
                        "domain": staged["domain"],
                    }
                )

    def _append_rom_state(
        self,
        registers: list[dict[str, Any]],
        direct_next: list[dict[str, Any]],
    ) -> None:
        canonical = self._canonical
        for rom in canonical.roms:
            domain = rom.domain or canonical.clock
            if not isinstance(domain, str) or not domain:
                raise SimulationPlanError(
                    f"ROM '{rom.name}' has no owning clock domain"
                )
            zero_node = len(self._nodes)
            self._nodes.append(
                {
                    "id": zero_node,
                    "op": ExpressionOp.CONSTANT.value,
                    "type": _type_payload(rom.element_type),
                    "operands": [],
                    "attributes": {"limbs": _u64_limbs(0, rom.element_type.width)},
                    "origins": [],
                }
            )
            lookup_node = len(self._nodes)
            self._nodes.append(
                {
                    "id": lookup_node,
                    "op": "rom_lookup",
                    "type": _type_payload(rom.element_type),
                    "operands": [rom.read_address, *rom.contents],
                    "attributes": {"depth": rom.depth},
                    "origins": [
                        encoded
                        for origin in (rom.source_origin,)
                        if (encoded := _origin_payload(origin)) is not None
                    ],
                }
            )
            result_name = self._rom_result_names[rom.name]
            registers.append(
                {
                    "name": result_name,
                    "type": _type_payload(rom.element_type),
                    "initial": zero_node,
                    "initial_limbs": _u64_limbs(0, rom.element_type.width),
                    "domain": domain,
                }
            )
            direct_next.append(
                {
                    "target": result_name,
                    "node": lookup_node,
                    "activation": None,
                    "domain": domain,
                }
            )

    def build(self) -> RuntimeStatePlanProduct:
        canonical = self._canonical
        ports, outputs = self._ports_and_outputs()
        registers = [
            {
                "name": register.name,
                "type": _type_payload(register.type),
                "initial": register.initial,
                "initial_limbs": _u64_limbs(
                    self._packed_register_initials[register.name], register.type.width
                ),
                "domain": register.domain or canonical.clock,
                "resettable": register.initial is not None,
            }
            for register in canonical.registers
        ]
        domains = self._domains()
        direct_next = [
            {
                "target": item.target_name,
                "node": item.expression,
                "activation": item.activation,
                "domain": next(
                    (
                        register.domain or canonical.clock
                        for register in canonical.registers
                        if register.name == item.target_name
                    ),
                    canonical.clock,
                ),
            }
            for item in canonical.next_assignments
            if item.target_kind is TargetKind.REGISTER
        ]
        self._append_staged_state(registers, direct_next)
        self._append_rom_state(registers, direct_next)
        return RuntimeStatePlanProduct(
            ports, outputs, registers, domains, direct_next
        )
