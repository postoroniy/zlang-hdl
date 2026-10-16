# SPDX-License-Identifier: Apache-2.0
"""Bind compiler-owned typed debug layouts to existing native storage only."""

from __future__ import annotations

from collections.abc import Mapping

from zlang.ir.hierarchy import build_hierarchy_index
from zlang.ir.interfaces import InterfaceProtocol, ready_valid_field_name
from zlang.ir.module import Module
from zlang.ir.types import BitType, BitsType
from zlang.native_debug_types import DebugMember, DebugSignal, DebugType, debug_type
from zlang.simulation_protocol_shared import (
    credit_field_name, packet_field_name, protocol_scalar_fields,
    request_response_field_name, request_response_scalar_fields, vc_credit_field_name,
)
from zlang.simulation_runtime_plan import registered_output_runtime_names


def build_debug_catalog(module: Module, plan: Mapping[str, object]) -> tuple[DebugSignal, ...]:
    """No source-name inference: protocol fields use their lowering bindings.

    Compiler-generated storage without a source type remains explicitly raw
    bits. This is not a guessed nominal type. Missing source bindings fail closed.
    """
    raw = {
        item["name"]: (kind, item)
        for key, kind in (("ports", "port"), ("registers", "register"), ("memories", "memory"))
        for item in plan[key]  # type: ignore[union-attr]
    }
    result: dict[str, DebugSignal] = {}
    used: set[str] = set()

    def bound(name: str, binding: str, type_: object, *, domain: str | None = None) -> DebugSignal | None:
        if binding not in raw:
            return None
        kind, item = raw[binding]
        layout = debug_type(type_)
        if layout.width != item["width"]:
            raise ValueError(f"debug type width differs from native binding '{binding}'")
        depth = item.get("depth")
        if kind == "memory":
            layout = DebugType("memory", f"memory<{layout.label}>", layout.width,
                               element=layout, length=int(depth))
        used.add(binding)
        return DebugSignal(name, kind, int(item["width"]), str(type_),
                           item.get("direction"), depth, layout, binding, item.get("domain") or domain)

    names_for = {
        InterfaceProtocol.READY_VALID: ready_valid_field_name,
        InterfaceProtocol.CREDIT: credit_field_name,
        InterfaceProtocol.PACKET: packet_field_name,
        InterfaceProtocol.VC_CREDIT: vc_credit_field_name,
    }
    for entry in build_hierarchy_index(module).entries:
        current = entry.module
        prefix = ".".join(entry.physical_path[1:])

        def native(name: str) -> str:
            return f"{prefix}.{name}" if prefix else name

        # The native hierarchy exposes state, but not private child-port aliases.
        if not prefix:
            for port in current.ports:
                if port.protocol is InterfaceProtocol.WIRE:
                    signal = bound(port.name, port.name, port.type, domain=port.domain or current.clock)
                    if signal is not None:
                        result[signal.name] = signal
                    continue
                fields = []
                for field in protocol_scalar_fields(port):
                    binding = names_for[port.protocol](port.name, field.signal)
                    signal = bound(binding, binding, field.type, domain=port.domain)
                    if signal is None:
                        raise ValueError(f"debug protocol binding '{binding}' is unavailable")
                    fields.append(DebugMember(field.signal.value, signal.layout,
                                              binding=binding, direction=signal.direction,
                                              domain=port.domain or current.clock))
                layout = DebugType("interface", port.protocol.value, 0, members=tuple(fields))
                result[port.name] = DebugSignal(port.name, "interface", 0, layout.label,
                                              layout=layout, domain=port.domain or current.clock)
            for interface in current.request_responses:
                channels: dict[str, list[DebugMember]] = {}
                for field in request_response_scalar_fields(interface):
                    binding = request_response_field_name(interface.name, field.channel, field.signal)
                    signal = bound(binding, binding, field.type, domain=interface.domain)
                    if signal is None:
                        raise ValueError(f"debug request/response binding '{binding}' is unavailable")
                    channels.setdefault(field.channel.value, []).append(DebugMember(field.signal.value,
                                              signal.layout, binding=binding,
                                              direction=signal.direction, domain=signal.domain))
                fields = tuple(DebugMember(channel, DebugType("interface", "ready_valid", 0, members=tuple(members)))
                               for channel, members in channels.items())
                layout = DebugType("interface", "request_response", 0, members=tuple(fields))
                result[interface.name] = DebugSignal(interface.name, "interface", 0, layout.label, layout=layout)
        # Reuse the native registered-output alias owner, never duplicate its hash.
        register_names = registered_output_runtime_names(current, current)
        for collection, element in ((current.registers, False), (current.memories, True)):
            for item in collection:
                binding = native(register_names.get(item.name, item.name))
                type_ = item.element_type if element else item.type
                signal = bound(native(item.name), binding, type_, domain=item.domain or current.clock)
                if signal is not None:
                    # The public registered output already exposes this same state.
                    if signal.name in result:
                        used.add(binding)
                    else:
                        result[signal.name] = signal
    # Aggregate protocols already carry the exact physical member inventory in
    # semantic IR (the same endpoint__member boundary used by top_physical_abi).
    # Group those existing bindings; do not manufacture a packed aggregate value.
    for endpoint in module.aggregate_protocol_endpoints:
        members = []
        for member in endpoint.members:
            physical = f"{endpoint.name}__{member.name}"
            signal = result.pop(physical, None)
            if signal is None:
                raise ValueError(f"debug interface member '{physical}' is unavailable")
            members.append(DebugMember(member.name, signal.layout,
                                       binding=signal.binding if signal.kind != "interface" else None,
                                       direction=signal.direction, domain=signal.domain))
        layout = DebugType("interface", f"{endpoint.protocol} ({endpoint.role})", 0, members=tuple(members))
        result[endpoint.name] = DebugSignal(endpoint.name, "interface", 0, layout.label,
                                            layout=layout, domain=endpoint.domain or module.clock)
    for name, (kind, item) in raw.items():
        if name in used:
            continue
        width = int(item["width"])
        layout = debug_type(BitsType(width))
        depth = item.get("depth")
        if depth is not None:
            layout = DebugType("memory", "memory<bits>", width, element=layout, length=int(depth))
        result[name] = DebugSignal(name, kind, width, layout.label, item.get("direction"),
                                   depth, layout, name, item.get("domain"))
    # Controls are plan-owned event inputs, not ordinary runtime value slots.
    # Keep their source names and read-only kind; never invent a shadow port.
    for domain in plan["domains"]:
        for key, kind in (("clock", "clock"), ("reset", "reset")):
            name = domain[key]
            if name is None:
                continue
            existing = result.get(name)
            if existing is not None:
                if existing.kind != kind:
                    raise ValueError(f"debug control '{name}' collides with a value binding")
                continue
            result[name] = DebugSignal(name, kind, 1, "bit", "input",
                                      layout=debug_type(BitType()), binding=name,
                                      domain=domain["clock"] if kind == "clock" else None)
    if len(result) > 16384:
        raise ValueError("debug catalog exceeds 16384 roots")
    return tuple(result[name] for name in sorted(result))
