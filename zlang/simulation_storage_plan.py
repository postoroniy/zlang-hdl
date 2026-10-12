# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""FIFO and memory record construction for native simulation plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir import packing as ir_packing
from zlang.ir import storage as ir_storage
from zlang.ir import types as ir_types
from zlang.opt.ir import ExpressionOp
from zlang.simulation_plan_encoding import (
    pack_initial as _pack_initial,
    type_payload as _type_payload,
    u64_limbs as _u64_limbs,
)
from zlang.simulation_plan_policy import (
    JitUnsupportedFeatureError,
    SimulationPlanError,
)


@dataclass(frozen=True)
class StoragePlanProduct:
    memories: list[dict[str, Any]]
    fifos: list[dict[str, Any]]


@dataclass
class StoragePlanBuilder:
    """Own native FIFO and memory records plus their hidden scalar state."""

    _module: object
    _canonical: object
    _nodes: list[dict[str, Any]]
    _registers: list[dict[str, Any]]
    _domains: list[dict[str, Any]]
    _fifo_state: dict[str, dict[str, object]]
    _memory_read_registers: dict[tuple[str, str | None], list[str]]

    def _reset_for(self, domain: str) -> object:
        return next(
            (item["reset"] for item in self._domains if item["clock"] == domain),
            None,
        )

    def _append_zero_node(self, type_: ir_types.HardwareType) -> int:
        identifier = len(self._nodes)
        self._nodes.append(
            {
                "id": identifier,
                "op": ExpressionOp.CONSTANT.value,
                "type": _type_payload(type_),
                "operands": [],
                "attributes": {"limbs": _u64_limbs(0, type_.width)},
                "origins": [],
            }
        )
        return identifier

    def _append_read_registers(
        self,
        names: list[str],
        type_: ir_types.HardwareType,
        domain: str,
        zero_node: int,
    ) -> None:
        self._registers.extend(
            {
                "name": name,
                "type": _type_payload(type_),
                "initial": zero_node,
                "initial_limbs": _u64_limbs(0, type_.width),
                "domain": domain,
            }
            for name in names
        )

    def _build_fifos(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        canonical = self._canonical
        memories: list[dict[str, Any]] = []
        fifos: list[dict[str, Any]] = []
        for fifo in canonical.fifos:
            domain = fifo.domain or canonical.clock
            if not isinstance(domain, str) or not domain:
                raise SimulationPlanError(
                    f"FIFO '{fifo.name}' has no owning clock domain"
                )
            state = self._fifo_state[fifo.name]
            prefix = state["prefix"]
            count_width = int(state["count_width"])
            pointer_width = int(state["pointer_width"])
            count_name = f"{prefix}_count"
            read_pointer = f"{prefix}_read_pointer"
            write_pointer = f"{prefix}_write_pointer"
            memory_name = f"{prefix}_storage"
            for name, width in (
                (count_name, count_width),
                (read_pointer, pointer_width),
                (write_pointer, pointer_width),
            ):
                type_ = ir_types.UIntType(width)
                zero_node = self._append_zero_node(type_)
                self._append_read_registers([name], type_, domain, zero_node)
            memories.append(
                {
                    "name": memory_name,
                    "type": _type_payload(fifo.element_type),
                    "depth": fifo.depth,
                    "domain": domain,
                    "collision": "read_first",
                    "write_mask_width": None,
                    "contents_reset": "preserve",
                    "read_data_reset": "preserve",
                    "initial_limbs": _u64_limbs(0, fifo.element_type.width),
                    "ports": [],
                    "write_priority": [],
                    "managed_by": "fifo",
                }
            )
            fifos.append(
                {
                    "name": fifo.name,
                    "type": _type_payload(fifo.element_type),
                    "depth": fifo.depth,
                    "domain": domain,
                    "scheduled": fifo.data is None,
                    "data": fifo.data,
                    "push": fifo.push,
                    "pop": fifo.pop,
                    "memory": memory_name,
                    "count": count_name,
                    "read_pointer": read_pointer,
                    "write_pointer": write_pointer,
                    "count_width": count_width,
                    "pointer_width": pointer_width,
                    "reset": self._reset_for(domain),
                }
            )
        return memories, fifos

    def _memory_ports(
        self,
        memory: object,
        domain: str,
        zero_node: int,
    ) -> tuple[list[dict[str, Any]], list[str], bool]:
        scheduled = not memory.ports and memory.read_address is None
        if scheduled:
            read_registers = self._memory_read_registers[(memory.name, None)]
            self._append_read_registers(
                read_registers, memory.element_type, domain, zero_node
            )
            return [], read_registers, True
        if memory.ports:
            port_records = []
            for port in memory.ports:
                read_registers = self._memory_read_registers.get(
                    (memory.name, port.name), []
                )
                self._append_read_registers(
                    read_registers, memory.element_type, port.domain, zero_node
                )
                port_records.append(
                    {
                        "name": port.name,
                        "kind": port.kind.value,
                        "domain": port.domain,
                        "address": port.address,
                        "write_address": port.address,
                        "read_enable": port.read_enable,
                        "write_enable": port.write_enable,
                        "write_data": port.write_data,
                        "write_mask": port.write_mask,
                        "read_registers": read_registers,
                    }
                )
            return port_records, [], False

        read_registers = self._memory_read_registers[(memory.name, None)]
        self._append_read_registers(
            read_registers, memory.element_type, domain, zero_node
        )
        assert memory.read_address is not None
        assert memory.write_enable is not None
        assert memory.write_address is not None
        assert memory.write_data is not None
        return [
            {
                "name": None,
                "kind": ir_storage.MemoryPortKind.READ_WRITE.value,
                "domain": domain,
                "address": memory.read_address,
                "write_address": memory.write_address,
                "read_enable": None,
                "write_enable": memory.write_enable,
                "write_data": memory.write_data,
                "write_mask": memory.write_mask,
                "read_registers": read_registers,
            }
        ], [], False

    def build(self) -> StoragePlanProduct:
        canonical = self._canonical
        memories, fifos = self._build_fifos()
        semantic_memories = {memory.name: memory for memory in self._module.memories}
        for memory in canonical.memories:
            domain = memory.domain or canonical.clock
            if memory.async_memory:
                domain = next(
                    (
                        port.domain
                        for port in memory.ports
                        if port.kind is ir_storage.MemoryPortKind.WRITE
                    ),
                    None,
                )
            if not isinstance(domain, str) or not domain:
                raise SimulationPlanError(
                    f"memory '{memory.name}' has no owning clock domain"
                )
            semantic_memory = semantic_memories[memory.name]
            try:
                initial_value = (
                    constant_runtime_value(semantic_memory.initial_value)
                    if semantic_memory.initial_value is not None
                    else 0
                )
                packed_initial = _pack_initial(memory.element_type, initial_value)
            except (ConstantExpressionError, ir_packing.PackingError) as error:
                raise JitUnsupportedFeatureError(
                    "native simulation requires a constant initial value for memory "
                    f"'{memory.name}': {error}"
                ) from error
            zero_node = self._append_zero_node(memory.element_type)
            port_records, scheduled_read_registers, scheduled = self._memory_ports(
                memory, domain, zero_node
            )
            memories.append(
                {
                    "name": memory.name,
                    "type": _type_payload(memory.element_type),
                    "depth": memory.depth,
                    "domain": domain,
                    "async_memory": memory.async_memory,
                    "reset": self._reset_for(domain),
                    "collision": memory.collision.value,
                    "write_mask_width": memory.write_mask_width,
                    "contents_reset": memory.contents_reset.value,
                    "read_data_reset": memory.read_data_reset.value,
                    "initial_limbs": _u64_limbs(
                        packed_initial, memory.element_type.width
                    ),
                    "ports": port_records,
                    "write_priority": list(memory.write_priority),
                    "managed_by": "scheduled_memory" if scheduled else "memory",
                    "scheduled_read_registers": scheduled_read_registers,
                }
            )
        return StoragePlanProduct(memories, fifos)
