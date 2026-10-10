# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Atomic initial-register override resolution through compiler-owned state ABI."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from zlang.simulation_logic import LogicVector
from zlang.simulation_state import (
    SimulationStateError,
    SimulationStateKind,
    build_simulation_state_catalog,
)
from zlang.simulation_values import SimulationRuntimeError


def resolve_initial_registers(
    module: object,
    selected_ir_identity: str,
    values: Mapping[str, str | LogicVector] | None,
) -> dict[str, LogicVector]:
    """Resolve all overrides before returning any native-state edits."""

    if not values:
        return {}
    try:
        catalog = build_simulation_state_catalog(
            module, selected_ir_identity=selected_ir_identity
        )
    except SimulationStateError as error:
        raise SimulationRuntimeError(
            f"initial-register catalog is unavailable: {error}"
        ) from error
    aliases: dict[str, list[tuple[str, object]]] = defaultdict(list)
    for binding in catalog.bindings:
        if binding.object_kind is not SimulationStateKind.REGISTER:
            continue
        relative = (*binding.physical_instance_path[1:], binding.object_name)
        native_name = ".".join(relative)
        full_name = ".".join((*binding.physical_instance_path, binding.object_name))
        aliases[native_name].append((native_name, binding))
        aliases[full_name].append((native_name, binding))
        aliases[binding.object_name].append((native_name, binding))
    result: dict[str, LogicVector] = {}
    for raw_name, raw_value in values.items():
        if not isinstance(raw_name, str) or not raw_name:
            raise SimulationRuntimeError("initial register name must not be empty")
        matches = aliases.get(raw_name, [])
        unique = {name: binding for name, binding in matches}
        if not unique:
            raise SimulationRuntimeError(
                f"unknown or non-register initial state '{raw_name}'"
            )
        if len(unique) != 1:
            owners = ", ".join(sorted(unique))
            raise SimulationRuntimeError(
                f"ambiguous initial register '{raw_name}'; use one of: {owners}"
            )
        native_name, binding = next(iter(unique.items()))
        if native_name in result:
            raise SimulationRuntimeError(
                f"initial register '{native_name}' is specified more than once"
            )
        width = int(binding.packed_width)
        try:
            value = (
                raw_value
                if isinstance(raw_value, LogicVector)
                else LogicVector.parse(raw_value, width)
            )
        except ValueError as error:
            raise SimulationRuntimeError(
                f"invalid initial value for '{raw_name}': {error}"
            ) from error
        if value.width != width:
            raise SimulationRuntimeError(
                f"initial value for '{raw_name}' has width {value.width}; expected {width}"
            )
        result[native_name] = value
    return result


__all__ = ["resolve_initial_registers"]
