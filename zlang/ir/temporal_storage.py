"""Immutable physical storage plans for bounded temporal implementations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from zlang.ir.types import FixedType, HardwareType, SIntType


@dataclass(frozen=True)
class ValueLifetime:
    """Inclusive liveness interval of one scheduled value."""

    value_id: str
    first_live_cycle: int
    last_live_cycle: int
    width: int
    signed: bool

    def __post_init__(self) -> None:
        if not self.value_id or self.width < 1:
            raise ValueError("temporal value lifetime is incomplete")
        if self.first_live_cycle < 0 or self.last_live_cycle < self.first_live_cycle:
            raise ValueError("temporal value lifetime cycles are invalid")


@dataclass(frozen=True)
class RegisterBinding:
    """One value's compiler-owned physical storage identity."""

    value_id: str
    physical_register_id: str

    def __post_init__(self) -> None:
        if not self.value_id or not self.physical_register_id:
            raise ValueError("temporal register bindings must be named")


@dataclass(frozen=True)
class TemporalPhysicalRegister:
    """One physical register and the values that legally reuse it."""

    register_id: str
    type: HardwareType
    value_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.register_id or not self.value_ids:
            raise ValueError("temporal physical storage is incomplete")


@dataclass(frozen=True)
class TemporalStoragePlan:
    """The sole source of truth for temporal value storage and FF cost."""

    registers: tuple[TemporalPhysicalRegister, ...]
    control_ff: int

    def __post_init__(self) -> None:
        names = tuple(item.register_id for item in self.registers)
        if len(names) != len(set(names)) or self.control_ff < 0:
            raise ValueError("temporal storage plan is invalid")

    @property
    def ff_cost(self) -> int:
        return self.control_ff + sum(item.type.width for item in self.registers)

    def register_for(self, value_id: str) -> TemporalPhysicalRegister:
        matches = tuple(item for item in self.registers if value_id in item.value_ids)
        if len(matches) != 1:
            raise ValueError(f"temporal storage has no unique register for '{value_id}'")
        return matches[0]


def build_temporal_storage_plan(
    *,
    lifetimes: tuple[ValueLifetime, ...],
    bindings: tuple[RegisterBinding, ...],
    value_types: Mapping[str, HardwareType],
    control_ff: int,
) -> TemporalStoragePlan:
    """Validate bindings and create one deterministic physical storage plan."""

    if control_ff < 0:
        raise ValueError("temporal control FF cost must be non-negative")
    lifetime_by_id = {item.value_id: item for item in lifetimes}
    if len(lifetime_by_id) != len(lifetimes):
        raise ValueError("temporal lifetimes must be unique")
    binding_by_value = {item.value_id: item.physical_register_id for item in bindings}
    if len(binding_by_value) != len(bindings) or set(binding_by_value) != set(lifetime_by_id):
        raise ValueError("every temporal lifetime requires one unique register binding")
    if set(value_types) != set(lifetime_by_id):
        raise ValueError("temporal storage types must cover exactly the scheduled values")

    grouped: dict[str, list[ValueLifetime]] = {}
    for value_id, register_id in binding_by_value.items():
        grouped.setdefault(register_id, []).append(lifetime_by_id[value_id])

    registers: list[TemporalPhysicalRegister] = []
    for register_id in sorted(grouped):
        values = sorted(grouped[register_id], key=lambda item: (
            item.first_live_cycle, item.last_live_cycle, item.value_id,
        ))
        first_type = value_types[values[0].value_id]
        previous: ValueLifetime | None = None
        for value in values:
            value_type = value_types[value.value_id]
            if value_type != first_type:
                raise ValueError("shared temporal storage requires exactly equal types")
            if value.width != first_type.width:
                raise ValueError("temporal storage width disagrees with value type")
            if value.signed != isinstance(value_type, (SIntType, FixedType)):
                raise ValueError("temporal storage signedness disagrees with value type")
            if previous is not None and previous.last_live_cycle >= value.first_live_cycle:
                raise ValueError("overlapping temporal values cannot share one register")
            previous = value
        registers.append(TemporalPhysicalRegister(
            register_id=register_id,
            type=first_type,
            value_ids=tuple(item.value_id for item in values),
        ))
    return TemporalStoragePlan(tuple(registers), control_ff)


__all__ = [
    "RegisterBinding",
    "TemporalPhysicalRegister",
    "TemporalStoragePlan",
    "ValueLifetime",
    "build_temporal_storage_plan",
]
