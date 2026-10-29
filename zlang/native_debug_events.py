# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Pack a saved typed JSONL schedule for the existing Rust debug transport.

This pass performs value-boundary encoding only. Rust still owns all event
execution, state, clocks and U/X propagation. Compiler and stimulus identities
are bound separately; changing stimulus never changes the hardware identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from zlang import sim
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import PortDirection
from zlang.simulation_logic import LogicVector
from zlang.sim_cli import _read_events
from zlang.simulation_protocol_access import SimulationProtocolAccess
from zlang.simulation_values import SimulationRuntimeError, pack_value


MAX_PREPARED_EVENTS_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class PreparedDebugEvents:
    """An immutable schedule snapshot bound to one exact execution plan."""

    plan_identity: str
    source_sha256: str
    sha256: str
    jsonl: str

    def to_data(self) -> dict[str, str]:
        return {"schema": "zlang-native-debug-events-v1",
                "plan_identity": self.plan_identity, "source_sha256": self.source_sha256,
                "sha256": self.sha256, "jsonl": self.jsonl}


def prepare_debug_events(program: sim.Program, path: Path) -> PreparedDebugEvents:
    with path.open("rb") as source:
        raw = source.read(MAX_PREPARED_EVENTS_BYTES + 1)
    if len(raw) > MAX_PREPARED_EVENTS_BYTES:
        raise ValueError("debug event file exceeds 16 MiB")
    schedule = _read_events(path, text=raw.decode("utf-8"))
    ports = {port.name: port for port in program.module.ports}
    interfaces = {item.name: item for item in program.module.request_responses}
    lines: list[str] = []
    size = 0
    for record in schedule.records:
        event = dict(record.event)
        try:
            supplied = event.get("set", {})
            if not isinstance(supplied, Mapping):
                raise ValueError("event set field must be a mapping")
            packed: dict[str, str] = {}
            for name, value in supplied.items():
                port = ports.get(name)
                if name in interfaces:
                    updates = SimulationProtocolAccess.request_response_updates(interfaces[name], value)
                elif port is None:
                    raise ValueError(f"unknown public input '{name}'")
                elif port.protocol is not InterfaceProtocol.WIRE:
                    updates = tuple((binding, type_, item) for _, binding, type_, item
                                    in SimulationProtocolAccess.port_updates(port, value))
                elif port.direction is not PortDirection.INPUT:
                    raise ValueError(f"port '{name}' is not an input")
                else:
                    updates = ((name, port.type, value),)
                for binding, type_, item in updates:
                    packed[binding] = format(pack_value(type_, item), f"0{type_.width}b")
            supplied_logic = event.get("set_logic", {})
            if not isinstance(supplied_logic, Mapping):
                raise ValueError("event set_logic field must be a mapping")
            for name, value in supplied_logic.items():
                port = ports.get(name)
                if (port is None or port.protocol is not InterfaceProtocol.WIRE
                        or port.direction is not PortDirection.INPUT):
                    raise ValueError(f"set_logic requires a scalar wire input: '{name}'")
                if not isinstance(value, str):
                    raise ValueError(f"logic input '{name}' must be a string")
                packed[name] = LogicVector.parse(value, port.type.width).to_bits()
            event["set"] = {}
            event["set_logic"] = packed
            event["repeat"] = record.repeat
            # Keep physical source line numbers and compact repeats intact.
            blank_lines = record.line_number - len(lines) - 1
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
            size += blank_lines + len(encoded.encode("utf-8")) + 1
            if size > MAX_PREPARED_EVENTS_BYTES:
                raise ValueError("prepared debug events exceed 16 MiB")
            lines.extend([""] * blank_lines)
            lines.append(encoded)
        except (ValueError, SimulationRuntimeError) as error:
            raise ValueError(f"{path}:{record.line_number}: {error}") from error
    text = "\n".join(lines) + ("\n" if lines else "")
    return PreparedDebugEvents(program.plan.identity, hashlib.sha256(raw).hexdigest(),
                               hashlib.sha256(text.encode("utf-8")).hexdigest(), text)
