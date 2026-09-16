"""Deterministic physical planning for normalized logical memories."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import json
from typing import Mapping

from zlang.ir.storage import Memory, MemoryPortKind
from zlang.ir.signed_reductions import expression_semantic_identity


class MemoryPlanningError(ValueError):
    """A logical memory has no legal bounded implementation."""


class MemoryImplementationKind(str, Enum):
    LEGACY_1R1W = "legacy_1r1w"
    NATIVE = "native"
    ASYNC_1W1R = "async_1w1r"
    REPLICATED_1R1W = "replicated_1r1w"
    REGISTER_ARRAY = "register_array"


class MemoryTargetPolicy(str, Enum):
    GENERIC = "generic"
    PREFERRED = "preferred"
    REQUIRED = "required"


@dataclass(frozen=True)
class MemoryImplementationPlan:
    """One evidence-honest logical-to-physical memory decision."""

    memory_semantic_id: str
    memory_recipe_identity: str
    logical_shape: str
    implementation: MemoryImplementationKind
    physical_copies: int
    collision_contract: str
    collision_guarantee: str
    priority_gates: int
    storage_bits: int
    estimated_luts: int
    estimated_ffs: int
    estimated_brams: int
    cost_source: str
    target_resource_identity: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        payload = {
            "schema": "zlang-memory-implementation-plan-v1",
            "memory_semantic_id": self.memory_semantic_id,
            "memory_recipe_identity": self.memory_recipe_identity,
            "logical_shape": self.logical_shape,
            "implementation": self.implementation.value,
            "physical_copies": self.physical_copies,
            "collision_contract": self.collision_contract,
            "collision_guarantee": self.collision_guarantee,
            "priority_gates": self.priority_gates,
            "storage_bits": self.storage_bits,
            "estimated_luts": self.estimated_luts,
            "estimated_ffs": self.estimated_ffs,
            "estimated_brams": self.estimated_brams,
            "cost_source": self.cost_source,
            "target_resource_identity": self.target_resource_identity,
            "notes": self.notes,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def _logical_shape(memory: Memory) -> tuple[str, int, int, int]:
    if not memory.ported:
        return "1R1W", 1, 1, 2
    reads = sum(
        port.kind in {MemoryPortKind.READ, MemoryPortKind.READ_WRITE}
        for port in memory.ports
    )
    writes = sum(
        port.kind in {MemoryPortKind.WRITE, MemoryPortKind.READ_WRITE}
        for port in memory.ports
    )
    return f"{reads}R{writes}W", reads, writes, len(memory.ports)


def _memory_recipe_identity(memory: Memory) -> str:
    payload = (
        "zlang-memory-recipe-v1",
        memory.semantic_id,
        repr(memory.element_type),
        memory.depth,
        memory.read_latency,
        memory.collision.value,
        memory.contents_reset.value,
        memory.read_data_reset.value,
        memory.domain,
        memory.async_memory,
        memory.write_priority,
        (
            expression_semantic_identity(memory.initial_value)
            if memory.initial_value is not None else None
        ),
        tuple(
            (port.semantic_id, port.name, port.kind.value, port.domain)
            for port in memory.ports
        ),
    )
    return sha256(repr(payload).encode("utf-8")).hexdigest()


def _native_capable(
    memory: Memory,
    capabilities: Mapping[str, str] | None,
) -> bool:
    if capabilities is None:
        return False
    # A clocked block-memory read has at least one visible read boundary.
    # A zero-cycle contract may be mapped only to a resource explicitly
    # advertising combinational reads (for example LUTRAM), never to BRAM.
    if memory.read_latency == 0 and capabilities.get("synchronous_read") == "true":
        return False
    # The present physical target mapper verifies exactly one native clocked
    # read boundary.  A catalog entry alone cannot prove a second internal
    # output cut or the reset/enable behavior of fabric compensation stages.
    if memory.read_latency != 1:
        return False
    if memory.initial_value is not None and not capabilities.get("initialization"):
        return False
    shape, reads, writes, _ = _logical_shape(memory)
    modes = set(capabilities.get("port_modes", "").split("."))
    clocks = set(capabilities.get("clock_modes", "common").split("."))
    collisions = set(
        capabilities.get("same_clock_collision", "read_first.write_first").split(".")
        if not memory.async_memory
        else capabilities.get("cross_clock_collision", "").split(".")
    )
    wanted_collision = memory.collision.value
    mode = (
        "independent_1w1r"
        if memory.async_memory
        else "true_dual" if reads == 2 and writes == 2 else "simple_dual"
    )
    if mode not in modes:
        return False
    if memory.async_memory and "independent" not in clocks:
        return False
    if wanted_collision not in collisions:
        return False
    latencies = {
        int(value)
        for value in capabilities.get("read_latencies", "1").split(".")
        if value
    }
    return memory.read_latency in latencies


def plan_memory_implementation(
    memory: Memory,
    *,
    target_resource_identity: str | None = None,
    target_capabilities: Mapping[str, str] | None = None,
    target_policy: MemoryTargetPolicy | str = MemoryTargetPolicy.GENERIC,
    target_inventory: int | None = None,
) -> MemoryImplementationPlan:
    """Choose one bounded implementation without inferring a CDC contract."""

    policy = MemoryTargetPolicy(target_policy)
    shape, reads, writes, ports = _logical_shape(memory)
    storage_bits = memory.element_type.width * memory.depth
    if ports > 8:
        raise MemoryPlanningError(
            f"memory '{memory.name}' has {ports} ports; maximum is 8"
        )

    if _native_capable(memory, target_capabilities):
        if target_inventory is not None and target_inventory < 1:
            if policy is MemoryTargetPolicy.REQUIRED:
                raise MemoryPlanningError(
                    f"memory '{memory.name}' requires one target memory resource"
                )
        else:
            return MemoryImplementationPlan(
                memory.semantic_id,
                _memory_recipe_identity(memory),
                shape,
                MemoryImplementationKind.NATIVE,
                1,
                memory.collision.value,
                "target_capability",
                max(0, writes - 1),
                storage_bits,
                0,
                0,
                1,
                "target_estimate",
                target_resource_identity,
            )

    if policy is MemoryTargetPolicy.REQUIRED:
        raise MemoryPlanningError(
            f"memory '{memory.name}' shape {shape} has no exact target resource; "
            "required target policy forbids generic fallback"
        )

    if not memory.ported:
        kind = MemoryImplementationKind.LEGACY_1R1W
        copies = 1
    elif memory.async_memory:
        kind = MemoryImplementationKind.ASYNC_1W1R
        copies = 1
    elif writes == 1 and reads > 1:
        if reads > 8:
            raise MemoryPlanningError(
                f"memory '{memory.name}' requires {reads} read replicas; maximum is 8"
            )
        kind = MemoryImplementationKind.REPLICATED_1R1W
        copies = reads
    elif ports <= 2:
        kind = MemoryImplementationKind.REGISTER_ARRAY
        copies = 1
    else:
        if storage_bits > 4096:
            raise MemoryPlanningError(
                f"memory '{memory.name}' generic multi-write implementation requires "
                f"{storage_bits} storage bits; maximum is 4096; use banking, "
                "arbitration, or explicit stdlib composition"
            )
        kind = MemoryImplementationKind.REGISTER_ARRAY
        copies = 1

    physical_bits = storage_bits * copies
    guarantee = (
        "structural_digital_model"
        if memory.async_memory else "language_contract"
    )
    notes = (
        "independent-clock collision behavior is not claimed as vendor silicon behavior",
    ) if memory.async_memory else ()
    return MemoryImplementationPlan(
        memory.semantic_id,
        _memory_recipe_identity(memory),
        shape,
        kind,
        copies,
        memory.collision.value,
        guarantee,
        max(0, writes - 1),
        physical_bits,
        physical_bits,
        physical_bits,
        0,
        "structural_estimate",
        target_resource_identity,
        notes,
    )


def render_memory_implementation_report(plan: MemoryImplementationPlan) -> str:
    lines = [
        f"memory_identity={plan.memory_semantic_id}",
        f"memory_recipe_identity={plan.memory_recipe_identity}",
        f"logical_shape={plan.logical_shape}",
        f"implementation={plan.implementation.value}",
        f"physical_copies={plan.physical_copies}",
        f"collision={plan.collision_contract}",
        f"collision_guarantee={plan.collision_guarantee}",
        f"priority_gates={plan.priority_gates}",
        f"storage_bits={plan.storage_bits}",
        f"estimated_lut={plan.estimated_luts}",
        f"estimated_ff={plan.estimated_ffs}",
        f"estimated_bram={plan.estimated_brams}",
        f"cost_source={plan.cost_source}",
        f"plan_identity={plan.identity}",
    ]
    if plan.target_resource_identity is not None:
        lines.append(f"target_resource={plan.target_resource_identity}")
    lines.extend(f"note={note}" for note in plan.notes)
    return "\n".join(lines) + "\n"


__all__ = [
    "MemoryImplementationKind",
    "MemoryImplementationPlan",
    "MemoryPlanningError",
    "MemoryTargetPolicy",
    "plan_memory_implementation",
    "render_memory_implementation_report",
]
