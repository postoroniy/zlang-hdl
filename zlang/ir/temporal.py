"""Target-neutral temporal implementation records.

This layer deliberately records *when* an already exact semantic operation is
performed and *where* it is bound.  It never changes the semantic expression:
the e-graph owns WHAT, this graph owns WHEN/WHERE, and a protocol wrapper owns
transaction admission and retirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from zlang.common.serialization import stable_digest
from zlang.ir.temporal_admission import TemporalAdmissionPolicy
from zlang.ir.temporal_storage import (
    RegisterBinding,
    TemporalStoragePlan,
    ValueLifetime,
)
from zlang.ir.type_codec import canonical_type_data


class TemporalClass(str, Enum):
    """Admission model of an implementation candidate."""

    FIXED_RATE = "fixed_rate"
    FLOW_CONTROLLED = "flow_controlled"


@dataclass(frozen=True)
class ScheduledOperation:
    """One exact semantic operation placed in a deterministic cycle."""

    operation_id: str
    semantic_identity: str
    operation: str
    start_cycle: int
    result_cycle: int
    resource_class: str

    def __post_init__(self) -> None:
        if not self.operation_id or not self.semantic_identity or not self.operation:
            raise ValueError("temporal operation identity and kind must be non-empty")
        if not self.resource_class:
            raise ValueError("temporal operation resource class must be non-empty")
        if self.start_cycle < 0 or self.result_cycle < self.start_cycle:
            raise ValueError("temporal operation cycles are invalid")


@dataclass(frozen=True)
class ResourceBinding:
    operation_id: str
    physical_resource_id: str

    def __post_init__(self) -> None:
        if not self.operation_id or not self.physical_resource_id:
            raise ValueError("temporal resource bindings must be named")


@dataclass(frozen=True)
class TemporalResourceCost:
    lut: int
    ff: int
    dsp: int

    def __post_init__(self) -> None:
        if any(value < 0 for value in (self.lut, self.ff, self.dsp)):
            raise ValueError("temporal resource cost must not be negative")


@dataclass(frozen=True)
class TemporalImplementationGraph:
    """Frozen implementation schedule for one semantic region.

    This first foundation supports a capacity-one non-interleaved schedule.  A
    later modulo scheduler must use a distinct temporal class rather than
    silently changing this contract.
    """

    semantic_region_identity: str
    operations: tuple[ScheduledOperation, ...]
    dependencies: tuple[tuple[str, str], ...]
    resource_bindings: tuple[ResourceBinding, ...]
    value_lifetimes: tuple[ValueLifetime, ...]
    register_bindings: tuple[RegisterBinding, ...]
    storage_plan: TemporalStoragePlan
    admission_policy: TemporalAdmissionPolicy
    latency: int
    initiation_interval: int
    capacity: int
    temporal_class: TemporalClass
    resource_cost: TemporalResourceCost
    implementation_identity: str

    def __post_init__(self) -> None:
        if not self.semantic_region_identity or not self.implementation_identity:
            raise ValueError("temporal graph identities must be non-empty")
        if self.latency < 1 or self.initiation_interval < 1 or self.capacity < 1:
            raise ValueError("temporal timing/capacity must be positive")
        operation_ids = tuple(item.operation_id for item in self.operations)
        if not operation_ids or len(operation_ids) != len(set(operation_ids)):
            raise ValueError("temporal operation IDs must be non-empty and unique")
        known = set(operation_ids)
        if any(source not in known or destination not in known for source, destination in self.dependencies):
            raise ValueError("temporal dependency references an unknown operation")
        if any(item.operation_id not in known for item in self.resource_bindings):
            raise ValueError("temporal resource binding references an unknown operation")
        if len({item.operation_id for item in self.resource_bindings}) != len(self.resource_bindings):
            raise ValueError("each temporal operation requires one resource binding")
        lifetime_ids = tuple(item.value_id for item in self.value_lifetimes)
        if len(lifetime_ids) != len(set(lifetime_ids)):
            raise ValueError("temporal lifetimes must be unique")
        if set(item.value_id for item in self.register_bindings) != set(lifetime_ids):
            raise ValueError("every temporal value lifetime requires one register binding")
        if {
            value_id
            for register in self.storage_plan.registers
            for value_id in register.value_ids
        } != set(lifetime_ids):
            raise ValueError("temporal storage plan does not cover every lifetime")
        if self.resource_cost.ff != self.storage_plan.ff_cost:
            raise ValueError("temporal FF cost must match its physical storage plan")
        if self.temporal_class is TemporalClass.FIXED_RATE and self.initiation_interval != 1:
            raise ValueError("fixed-rate temporal candidates require II=1")

    @staticmethod
    def identity_data(
        *,
        semantic_region_identity: str,
        operations: tuple[ScheduledOperation, ...],
        dependencies: tuple[tuple[str, str], ...],
        resource_bindings: tuple[ResourceBinding, ...],
        value_lifetimes: tuple[ValueLifetime, ...],
        register_bindings: tuple[RegisterBinding, ...],
        storage_plan: TemporalStoragePlan,
        admission_policy: TemporalAdmissionPolicy,
        latency: int,
        initiation_interval: int,
        capacity: int,
        temporal_class: TemporalClass,
        resource_cost: TemporalResourceCost,
    ) -> str:
        """Return the stable HOW/WHEN/WHERE identity, never semantic identity."""

        return "temporal:" + stable_digest({
            "schema": "zlang-temporal-implementation-v2",
            "semantic_region_identity": semantic_region_identity,
            "operations": tuple(
                (item.operation_id, item.semantic_identity, item.operation,
                 item.start_cycle, item.result_cycle, item.resource_class)
                for item in operations
            ),
            "dependencies": dependencies,
            "resource_bindings": tuple(
                (item.operation_id, item.physical_resource_id)
                for item in resource_bindings
            ),
            "value_lifetimes": tuple(
                (item.value_id, item.first_live_cycle, item.last_live_cycle,
                 item.width, item.signed)
                for item in value_lifetimes
            ),
            "register_bindings": tuple(
                (item.value_id, item.physical_register_id)
                for item in register_bindings
            ),
            "storage": tuple(
                (item.register_id, canonical_type_data(item.type), item.value_ids)
                for item in storage_plan.registers
            ),
            "storage_control_ff": storage_plan.control_ff,
            "admission_policy": admission_policy.value,
            "latency": latency,
            "ii": initiation_interval,
            "capacity": capacity,
            "temporal_class": temporal_class.value,
            "resource_cost": (resource_cost.lut, resource_cost.ff, resource_cost.dsp),
        })


__all__ = [
    "RegisterBinding",
    "ResourceBinding",
    "ScheduledOperation",
    "TemporalClass",
    "TemporalImplementationGraph",
    "TemporalResourceCost",
    "ValueLifetime",
]
