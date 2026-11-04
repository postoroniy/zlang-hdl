# SPDX-License-Identifier: Apache-2.0
"""Simulation-I/O projection onto compiler-owned native and physical port facts."""

from zlang.ir.interfaces import InterfaceProtocol, ready_valid_field_name
from zlang.ir.module import PortDirection
from zlang.simulation_compare import _public_port_paths, _physical_leaf_value
from zlang.simulation_protocol_shared import request_response_field_name
from zlang.simulation_logic import LogicVector
from zlang.simulation_primitives import int_from_limbs


def physical_groups(module, direction):
    paths = _public_port_paths(module)
    ports = {port.name: port for port in module.ports}
    result = {}
    for leaf in module.top_physical_abi.leaves:
        if leaf.direction is not direction or leaf.category in {"clock", "reset"}:
            continue
        if leaf.category == "request_response":
            name = request_response_field_name(*leaf.member_path[:3])
        else:
            name, _ = max(
                (
                    (name, path)
                    for name, path in paths.items()
                    if leaf.member_path[: len(path)] == path
                ),
                key=lambda item: len(item[1]),
            )
            if ports[name].protocol is not InterfaceProtocol.WIRE:
                name = ready_valid_field_name(name, leaf.signal_kind)
        result.setdefault(name, []).append(leaf)
    return result


def input_groups(module):
    return physical_groups(module, PortDirection.INPUT)


def raw_value(instance, name, width):
    if instance.program.logic_state:
        value, unknown, actual = instance._native.get_logic(name)
        if actual != width:
            raise ValueError("simulation ABI width mismatch")
        return LogicVector(width, int_from_limbs(value), int_from_limbs(unknown))
    return LogicVector.known(int_from_limbs(instance._native.get_limbs(name)), width)


def physical_sample(instance, groups):
    """Preserve unresolved diagnostics; comparison still requires known controls."""
    result = {}
    for name, leaves in groups.items():
        value = raw_value(instance, name, leaves[0].packed_root_type.width)
        for leaf in leaves:
            packed = LogicVector(
                leaf.width,
                _physical_leaf_value(leaf, value.value),
                _physical_leaf_value(leaf, value.unknown),
            )
            result[leaf.external_name] = (
                packed.value if packed.is_binary else packed.to_bits().replace("u", "x")
            )
    return result


def leaf_slices(leaf):
    if leaf.packed_lsb is not None:
        return ((leaf.packed_lsb, leaf.width),)
    return tuple(
        (item.lsb, item.msb - item.lsb + 1)
        for item in sorted(leaf.packed_element_slices, key=lambda i: i.indices)
    )


def sv_leaf(leaf, container):
    pieces = [
        f"{container}[{lsb} +: {width}]" for lsb, width in reversed(leaf_slices(leaf))
    ]
    return "{" + ",".join(pieces) + "}"
