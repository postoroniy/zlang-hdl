# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Canonical-expression construction for deterministic simulation plans."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from zlang.ir import functional_regions
from zlang.ir.constants import ConstantExpressionError, constant_runtime_value
from zlang.ir.runtime_values import zero_runtime_value
from zlang.ir import packing as ir_packing
from zlang.ir import storage as ir_storage
from zlang.opt.ir import ExpressionOp
from zlang.simulation_plan_encoding import (
    compile_time_expression_binders as _compile_time_expression_binders,
    compile_time_expression_payload as _compile_time_expression_payload,
    expression_node_payload as _expression_node_payload,
    json_attribute as _json_attribute,
    pack_initial as _pack_initial,
    type_payload as _type_payload,
    u64_limbs as _u64_limbs,
)
from zlang.simulation_plan_policy import (
    JitUnsupportedFeatureError,
    SimulationPlanError,
)


_NATIVE_EXPRESSION_OPS = {
    ExpressionOp.INPUT,
    ExpressionOp.REGISTER_REF,
    ExpressionOp.CONSTANT,
    ExpressionOp.ENUM_ENCODE,
    ExpressionOp.ENUM_VALID,
    ExpressionOp.ENUM_DECODE,
    ExpressionOp.UNION_CONSTRUCT,
    ExpressionOp.UNION_TAG,
    ExpressionOp.UNION_FIELD,
    ExpressionOp.ADD,
    ExpressionOp.BINARY,
    ExpressionOp.EXTEND,
    ExpressionOp.TRUNCATE,
    ExpressionOp.FIXED_CONVERT,
    ExpressionOp.MUX,
    ExpressionOp.SWITCH,
    ExpressionOp.FIELD,
    ExpressionOp.STRUCT_CONSTRUCT,
    ExpressionOp.TUPLE_CONSTRUCT,
    ExpressionOp.TUPLE_PROJECT,
    ExpressionOp.VECTOR_INDEX,
    ExpressionOp.RUNTIME_INDEX,
    ExpressionOp.VECTOR_UPDATE,
    ExpressionOp.SLICE,
    ExpressionOp.CONCAT,
    ExpressionOp.VECTOR_CONCAT,
    ExpressionOp.RESHAPE,
    ExpressionOp.BITCAST,
    ExpressionOp.PACK,
    ExpressionOp.UNPACK,
    ExpressionOp.GENERATE,
    ExpressionOp.MAP,
    ExpressionOp.DOT,
    ExpressionOp.REDUCE,
    ExpressionOp.FUNCTIONAL_CAPTURE,
    ExpressionOp.FUNCTIONAL_VALUE,
    ExpressionOp.FUNCTIONAL_TABLE_LOOKUP,
    ExpressionOp.FUNCTIONAL_REGION,
}

def validate_native_expression_node(
    node: object,
    max_width: int,
    *,
    verification: bool = False,
) -> None:
    surface = "verification" if verification else "simulation"
    if node.op not in _NATIVE_EXPRESSION_OPS:
        raise JitUnsupportedFeatureError(
            f"native {surface} does not yet support expression '{node.op.value}'"
        )
    if node.type.width > max_width:
        qualifier = " verification" if verification else ""
        raise JitUnsupportedFeatureError(
            f"simulation{qualifier} currently supports packed values through "
            f"{max_width} bits; expression %{node.id} has width {node.type.width}"
        )


def native_expression_attributes(
    node: object,
    *,
    verification: bool = False,
) -> dict[str, object]:
    """Encode attributes shared by hardware and verification expression DAGs."""

    attributes = {name: _json_attribute(value) for name, value in node.attributes}
    if node.op is ExpressionOp.ENUM_DECODE:
        attributes.setdefault("enum_type", {"hardware_type": _type_payload(node.type)})
    if (
        node.op is ExpressionOp.FIXED_CONVERT
        and attributes.get("rational_denominator") is not None
    ):
        surface = "verification" if verification else "simulation"
        raise JitUnsupportedFeatureError(
            f"native {surface} requires rational fixed-point constants to "
            "be normalized before plan construction"
        )
    if node.op is ExpressionOp.CONSTANT:
        value = attributes.pop("value", None)
        label = "verification constant" if verification else "constant"
        if isinstance(value, bool) or not isinstance(value, int):
            raise SimulationPlanError(
                f"{label} node %{node.id} has no exact integer value"
            )
        try:
            packed_value = _pack_initial(node.type, value)
        except ir_packing.PackingError as error:
            raise SimulationPlanError(
                f"{label} node %{node.id} cannot be packed: {error}"
            ) from error
        attributes["limbs"] = _u64_limbs(packed_value, node.type.width)
    return attributes



def _memory_read_register_name(
    semantic_id: str, port_name: str | None, stage: int
) -> str:
    """Name the exact native state holding one compiler-owned memory read stage."""

    suffix = hashlib.sha256(
        f"{semantic_id}:{port_name or 'legacy'}".encode("utf-8")
    ).hexdigest()[:16]
    return f"$zlang_jit_memory_{suffix}_read_stage_{stage}"


@dataclass(frozen=True)
class ExpressionPlanProduct:
    nodes: list[dict[str, Any]]
    staged_expressions: list[dict[str, Any]]
    rom_result_names: dict[str, str]
    fifo_state: dict[str, dict[str, object]]
    memory_read_registers: dict[tuple[str, str | None], list[str]]
    packed_register_initials: dict[str, int]


@dataclass
class PrimitiveExpressionPlanBuilder:
    """Own canonical-expression validation and primitive-node encoding."""

    _module: object
    _canonical: object
    _max_memory_width: int
    _max_memory_bits: int
    _max_width: int
    _register_names: dict[str, str]

    def build(self) -> ExpressionPlanProduct:
        module = self._module
        canonical = self._canonical
        for memory in canonical.memories:
            if memory.element_type.width > self._max_memory_width:
                raise JitUnsupportedFeatureError(
                    f"memory '{memory.name}' has {memory.element_type.width}-bit cells; "
                    "simulation currently supports cells through "
                    f"{self._max_memory_width} bits"
                )
            scheduled = not memory.ports and memory.read_address is None
            port_domains = {port.domain for port in memory.ports}
            if (
                not 0 <= memory.read_latency <= 16
                or (
                    memory.ports
                    and not memory.async_memory
                    and len(port_domains) != 1
                )
                or (scheduled and memory.read_latency != 1)
            ):
                raise JitUnsupportedFeatureError(
                    "native simulation requires memory read_latency 0..16 and "
                    "exact same-clock or 1W1R asynchronous ports"
                )
            if memory.depth * memory.element_type.width > self._max_memory_bits:
                raise JitUnsupportedFeatureError(
                    f"memory '{memory.name}' exceeds the native simulation bound of "
                    f"{self._max_memory_bits} storage bits"
                )

        for fifo in canonical.fifos:
            if fifo.element_type.width > self._max_memory_width:
                raise JitUnsupportedFeatureError(
                    f"FIFO '{fifo.name}' has {fifo.element_type.width}-bit cells; "
                    f"simulation currently supports cells through "
                    f"{self._max_memory_width} bits"
                )
            if fifo.depth * fifo.element_type.width > self._max_memory_bits:
                raise JitUnsupportedFeatureError(
                    f"FIFO '{fifo.name}' exceeds the native simulation bound of "
                    f"{self._max_memory_bits} storage bits"
                )

        nodes: list[dict[str, Any]] = []
        staged_expressions: list[dict[str, Any]] = []
        rom_result_names = {
            rom.name: "$zlang_jit_rom_"
            + hashlib.sha256(rom.semantic_id.encode("utf-8")).hexdigest()[:16]
            + "_read_data"
            for rom in canonical.roms
        }
        fifo_state = {
            fifo.name: {
                "prefix": "$zlang_jit_fifo_"
                + hashlib.sha256(
                    f"{canonical.name}:{fifo.name}".encode("utf-8")
                ).hexdigest()[:16],
                "count_width": max(1, fifo.depth.bit_length()),
                "pointer_width": max(1, (fifo.depth - 1).bit_length()),
            }
            for fifo in canonical.fifos
        }
        memory_read_registers = {
            (memory.name, port_name): [
                _memory_read_register_name(memory.semantic_id, port_name, stage)
                for stage in range(memory.read_latency)
            ]
            for memory in canonical.memories
            for port_name in (
                tuple(
                    port.name
                    for port in memory.ports
                    if port.kind in {ir_storage.MemoryPortKind.READ, ir_storage.MemoryPortKind.READ_WRITE}
                )
                if memory.ports
                else (None,)
            )
        }
        semantic_registers = {register.name: register for register in module.registers}
        packed_register_initials: dict[str, int] = {}
        initial_node_values: dict[int, int] = {}
        for register in canonical.registers:
            try:
                semantic_initial = semantic_registers[register.name].initial
                initial_value = (
                    zero_runtime_value(register.type)
                    if semantic_initial is None
                    else constant_runtime_value(semantic_initial)
                )
                packed_initial = _pack_initial(register.type, initial_value)
            except (
                ConstantExpressionError,
                ir_packing.PackingError,
                ValueError,
            ) as error:
                raise JitUnsupportedFeatureError(
                    f"native simulation requires a constant initial value for register "
                    f"'{register.name}': {error}"
                ) from error
            packed_register_initials[register.name] = packed_initial
            if register.initial is not None:
                previous = initial_node_values.setdefault(register.initial, packed_initial)
                if previous != packed_initial:
                    raise SimulationPlanError(
                        f"canonical initial node %{register.initial} has conflicting values"
                    )
        for expected, node in enumerate(canonical.expressions):
            if node.id != expected:
                raise SimulationPlanError("canonical expression IDs are not contiguous")
            if (
                node.op is ExpressionOp.FUNCTIONAL_REGION
                and node.id in initial_node_values
            ):
                # The exact register initializer is stored separately in the plan.
                # Keep the canonical node ID stable without asking the executable
                # machine to interpret a compile-time functional region.
                nodes.append(
                    _expression_node_payload(
                        node,
                        op=ExpressionOp.CONSTANT.value,
                        operands=[],
                        attributes={
                            "limbs": _u64_limbs(
                                initial_node_values[node.id], node.type.width
                            )
                        },
                    )
                )
                continue
            if node.op is ExpressionOp.ROM_REF:
                attributes = dict(node.attributes)
                rom_name = attributes.get("rom")
                signal = attributes.get("signal")
                if (
                    not isinstance(rom_name, str)
                    or rom_name not in rom_result_names
                    or signal is not ir_storage.RomSignal.READ_DATA
                    or node.operands
                ):
                    raise SimulationPlanError(
                        f"ROM reference %{node.id} has invalid metadata"
                    )
                nodes.append(
                    _expression_node_payload(
                        node,
                        op=ExpressionOp.REGISTER_REF.value,
                        operands=[],
                        attributes={"name": rom_result_names[rom_name]},
                    )
                )
                continue
            if node.op is ExpressionOp.FIFO_REF:
                attributes = dict(node.attributes)
                fifo_name = attributes.get("fifo")
                signal = attributes.get("signal")
                if (
                    not isinstance(fifo_name, str)
                    or fifo_name not in fifo_state
                    or not isinstance(signal, ir_storage.FifoSignal)
                    or node.operands
                ):
                    raise SimulationPlanError(
                        f"FIFO reference %{node.id} has invalid metadata"
                    )
                nodes.append(
                    _expression_node_payload(
                        node,
                        op="fifo_ref",
                        operands=[],
                        attributes={"fifo": fifo_name, "signal": signal.value},
                    )
                )
                continue
            if node.op is ExpressionOp.MEMORY_REF:
                attributes = dict(node.attributes)
                memory_name = attributes.get("memory")
                signal = attributes.get("signal")
                port = attributes.get("port")
                key = (memory_name, port)
                if (
                    not isinstance(memory_name, str)
                    or key not in memory_read_registers
                    or signal is not ir_storage.MemorySignal.READ_DATA
                    or node.operands
                ):
                    raise SimulationPlanError(
                        f"memory reference %{node.id} has invalid metadata"
                    )
                read_registers = memory_read_registers[key]
                if read_registers:
                    replacement_op = ExpressionOp.REGISTER_REF.value
                    replacement_operands: list[int] = []
                    replacement_attributes = {"name": read_registers[-1]}
                else:
                    memory = next(item for item in canonical.memories if item.name == memory_name)
                    if port is None:
                        assert memory.read_address is not None
                        address = memory.read_address
                    else:
                        address = next(item.address for item in memory.ports if item.name == port)
                    replacement_op = "memory_port_read"
                    replacement_operands = [address]
                    replacement_attributes = {"memory": memory_name, "port": port}
                nodes.append(
                    _expression_node_payload(
                        node,
                        op=replacement_op,
                        operands=replacement_operands,
                        attributes=replacement_attributes,
                    )
                )
                continue
            if node.op in {ExpressionOp.DELAY, ExpressionOp.PIPELINE}:
                attributes = dict(node.attributes)
                stage_count_name = "cycles" if node.op is ExpressionOp.DELAY else "stages"
                stage_count = attributes.get(stage_count_name)
                instance = attributes.get("instance")
                domain = attributes.get("domain") or canonical.clock
                if (
                    isinstance(stage_count, bool)
                    or not isinstance(stage_count, int)
                    or stage_count < 1
                    or isinstance(instance, bool)
                    or not isinstance(instance, int)
                    or len(node.operands) != 1
                    or not isinstance(domain, str)
                    or not domain
                ):
                    raise SimulationPlanError(
                        f"sequential expression %{node.id} has invalid stage metadata"
                    )
                stage_names = [
                    f"$zlang_jit_stage_{instance}_{stage}" for stage in range(stage_count)
                ]
                staged_expressions.append(
                    {
                        "source": node.operands[0],
                        "type": node.type,
                        "domain": domain,
                        "names": stage_names,
                        "final_node": node.id,
                        "origins": node.origins,
                    }
                )
                nodes.append(
                    _expression_node_payload(
                        node,
                        op=ExpressionOp.REGISTER_REF.value,
                        operands=[],
                        attributes={"name": stage_names[-1]},
                    )
                )
                continue
            validate_native_expression_node(node, self._max_width)
            if node.op is ExpressionOp.FUNCTIONAL_VALUE:
                value = node.attribute("expression")
                if not isinstance(value, functional_regions.CompileTimeExpr):
                    raise JitUnsupportedFeatureError(
                        "simulation requires a compiler-owned functional value expression"
                    )
                attributes = {
                    "compile_time_expression": _compile_time_expression_payload(value),
                    "binders": _compile_time_expression_binders(value),
                }
            elif node.op is ExpressionOp.FUNCTIONAL_TABLE_LOOKUP:
                index = node.attribute("index")
                if (
                    not isinstance(index, functional_regions.CompileTimeExpr)
                    or index.operator is not functional_regions.CompileTimeOperator.BINDER
                    or not isinstance(index.operands[0], functional_regions.CompileTimeBinderRef)
                ):
                    raise JitUnsupportedFeatureError(
                        "simulation supports only direct-binder functional table lookups"
                    )
                attributes = {
                    "table_name": node.attribute("table_name"),
                    "binder": index.operands[0].identity,
                }
            elif node.op is ExpressionOp.FUNCTIONAL_REGION:
                binder = node.attribute("binder")
                kind = node.attribute("kind")
                tables = node.attribute("table_layout")
                captures = node.attribute("capture_layout")
                if (
                    kind not in {
                        functional_regions.FunctionalRegionKind.GENERATE,
                        functional_regions.FunctionalRegionKind.MAP,
                    }
                    or not isinstance(binder, functional_regions.CompileTimeBinderRef)
                ):
                    raise JitUnsupportedFeatureError(
                        "simulation supports only exact generated/mapped functional regions"
                    )
                attributes = {
                    "binder": binder.identity,
                    "start": binder.start,
                    "stop": binder.stop,
                    "tables": [
                        {
                            "name": name,
                            "start": start,
                            "width": type_.width,
                            "count": count,
                        }
                        for name, start, type_, count in tables
                    ],
                    "captures": [
                        {"identity": identity, "width": type_.width}
                        for identity, _display_name, type_ in captures
                    ],
                }
            elif node.op is ExpressionOp.VECTOR_INDEX and isinstance(
                node.attribute("index"), functional_regions.CompileTimeExpr
            ):
                index = node.attribute("index")
                attributes = {
                    "compile_time_expression": _compile_time_expression_payload(index),
                    "binders": _compile_time_expression_binders(index),
                }
            else:
                attributes = native_expression_attributes(node)
            if node.op is ExpressionOp.REGISTER_REF:
                register_name = attributes.get("name")
                if isinstance(register_name, str):
                    attributes["name"] = self._register_names.get(
                        register_name, register_name
                    )
            nodes.append(
                _expression_node_payload(
                    node,
                    op=node.op.value,
                    operands=list(node.operands),
                    attributes=attributes,
                )
            )

        return ExpressionPlanProduct(
            nodes,
            staged_expressions,
            rom_result_names,
            fifo_state,
            memory_read_registers,
            packed_register_initials,
        )
