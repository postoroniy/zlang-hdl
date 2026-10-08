# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Batch-event adaptation for the public native simulation API."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Port, PortDirection, RequestResponseInterface
from zlang.ir.types import HardwareType
from zlang.simulation_primitives import (
    int_from_limbs as _limbs_to_int,
    int_to_limbs as _int_to_limbs,
)
from zlang.simulation_protocol_shared import request_response_field_name
from zlang.simulation_values import (
    SimulationRuntimeError,
    pack_value,
    unpack_value,
)

if TYPE_CHECKING:
    from zlang.sim import Simulator


class SimulationEventRunner:
    """Encode, execute, and decode one bounded batch of public events.

    The mutable native instance remains owned by :class:`Simulator`. This
    service owns only the protocol-boundary adaptation previously embedded in
    that instance class.
    """

    def __init__(self, simulator: Simulator) -> None:
        self._simulator = simulator

    def run(
        self,
        events: Iterable[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        simulator = self._simulator
        simulator._require_open()
        protocol_ports = tuple(
            port
            for port in simulator.program.module.ports
            if port.protocol is not InterfaceProtocol.WIRE
        )
        request_responses = tuple(simulator.program.module.request_responses)
        external_values = self._initial_protocol_values(
            protocol_ports,
            request_responses,
        )
        encoded, snapshots = self._encode_events(events, external_values)
        try:
            raw_results = simulator._native.run_events(encoded)
        except (ValueError, RuntimeError) as error:
            simulator._raise_instrumentation_failure(error)
            raise SimulationRuntimeError(str(error)) from error
        simulator._raise_instrumentation_failure()
        if protocol_ports or request_responses:
            return self._decode_protocol_results(
                raw_results,
                snapshots,
                request_responses,
            )
        return [
            {
                name: unpack_value(simulator._ports[name].type, _limbs_to_int(value))
                for name, value in raw.items()
                if name in simulator._ports
            }
            for raw in raw_results
        ]

    def _initial_protocol_values(
        self,
        ports: tuple[Port, ...],
        interfaces: tuple[RequestResponseInterface, ...],
    ) -> dict[str, object]:
        simulator = self._simulator
        values: dict[str, object] = {}
        for port in ports:
            for field, type_ in simulator._protocols.input_fields(port):
                internal = simulator._protocols.field_name(port, field)
                values[internal] = simulator._get_scalar(internal, type_)
        for interface in interfaces:
            for channel, field, type_ in simulator._protocols.request_response_input_fields(
                interface
            ):
                internal = request_response_field_name(
                    interface.name,
                    channel,
                    field,
                )
                values[internal] = simulator._get_scalar(internal, type_)
        return values

    def _encode_events(
        self,
        events: Iterable[Mapping[str, object]],
        external_values: dict[str, object],
    ) -> tuple[list[object], list[dict[str, object]]]:
        encoded_events: list[object] = []
        snapshots: list[dict[str, object]] = []
        for event in events:
            unknown = set(event) - {"set", "reset", "edges"}
            if unknown:
                raise SimulationRuntimeError(
                    f"simulation event has unknown field '{sorted(unknown)[0]}'"
                )
            updates = event.get("set", {})
            resets = event.get("reset", {})
            edges = event.get("edges", ())
            if not isinstance(updates, Mapping) or not isinstance(resets, Mapping):
                raise SimulationRuntimeError("event set/reset fields must be mappings")
            if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
                raise SimulationRuntimeError("event edges field must be a sequence")
            packed_updates = self._encode_updates(updates, external_values)
            packed_resets = self._encode_resets(resets)
            selected_edges = [str(item) for item in edges]
            if len(selected_edges) != len(set(selected_edges)):
                raise SimulationRuntimeError("one event cannot contain a clock twice")
            encoded_events.append((packed_updates, packed_resets, selected_edges))
            snapshots.append(dict(external_values))
        return encoded_events, snapshots

    def _encode_updates(
        self,
        updates: Mapping[object, object],
        external_values: dict[str, object],
    ) -> list[tuple[str, object]]:
        simulator = self._simulator
        packed_updates: list[tuple[str, object]] = []
        for raw_name, value in updates.items():
            name = str(raw_name)
            port = simulator._ports.get(name)
            interface = simulator._request_responses.get(name)
            if port is None and interface is None:
                raise SimulationRuntimeError(f"unknown public port '{name}'")
            if interface is not None:
                packed_updates.extend(
                    self._encode_request_response_update(
                        interface,
                        value,
                        external_values,
                    )
                )
                continue
            assert port is not None
            if port.protocol is InterfaceProtocol.WIRE:
                packed = pack_value(port.type, value)
                packed_updates.append(
                    (name, _int_to_limbs(packed, port.type.width))
                )
                continue
            packed_updates.extend(
                self._encode_protocol_update(port, value, external_values)
            )
        return packed_updates

    def _encode_request_response_update(
        self,
        interface: RequestResponseInterface,
        value: object,
        external_values: dict[str, object],
    ) -> list[tuple[str, object]]:
        encoded: list[tuple[str, object]] = []
        for internal, type_, field_value in (
            self._simulator._protocols.request_response_updates(interface, value)
        ):
            packed = pack_value(type_, field_value)
            encoded.append((internal, _int_to_limbs(packed, type_.width)))
            external_values[internal] = field_value
        return encoded

    def _encode_protocol_update(
        self,
        port: Port,
        value: object,
        external_values: dict[str, object],
    ) -> list[tuple[str, object]]:
        encoded: list[tuple[str, object]] = []
        for _, internal, type_, field_value in (
            self._simulator._protocols.port_updates(
                port,
                value,
                label=port.protocol.value,
            )
        ):
            packed = pack_value(type_, field_value)
            encoded.append((internal, _int_to_limbs(packed, type_.width)))
            external_values[internal] = field_value
        return encoded

    @staticmethod
    def _encode_resets(
        resets: Mapping[object, object],
    ) -> list[tuple[str, bool]]:
        result: list[tuple[str, bool]] = []
        for raw_name, value in resets.items():
            if not isinstance(value, bool):
                raise SimulationRuntimeError("reset event values must be boolean")
            result.append((str(raw_name), value))
        return result

    def _decode_protocol_results(
        self,
        raw_results: Iterable[Mapping[str, object]],
        snapshots: list[dict[str, object]],
        request_responses: tuple[RequestResponseInterface, ...],
    ) -> list[dict[str, object]]:
        simulator = self._simulator
        results: list[dict[str, object]] = []
        for raw, external in zip(raw_results, snapshots, strict=True):
            decoded: dict[str, object] = {}
            for port in simulator._ports.values():
                self._decode_port(decoded, port, raw, external)
            for interface in request_responses:
                decoded[interface.name] = (
                    simulator._protocols.decode_request_response(
                        interface,
                        self._scalar_reader(raw, external),
                    )
                )
            results.append(decoded)
        return results

    def _decode_port(
        self,
        decoded: dict[str, object],
        port: Port,
        raw: Mapping[str, object],
        external: Mapping[str, object],
    ) -> None:
        if port.protocol is InterfaceProtocol.WIRE:
            if port.direction is PortDirection.OUTPUT:
                decoded[port.name] = unpack_value(
                    port.type,
                    _limbs_to_int(raw[port.name]),
                )
            return
        decoded[port.name] = self._simulator._protocols.decode_port(
            port,
            self._scalar_reader(raw, external),
        )

    @staticmethod
    def _scalar_reader(
        raw: Mapping[str, object],
        external: Mapping[str, object],
    ) -> Callable[[str, HardwareType], object]:
        def read(name: str, type_: HardwareType) -> object:
            if name in external:
                return external[name]
            return unpack_value(type_, _limbs_to_int(raw[name]))

        return read



__all__ = ["SimulationEventRunner"]
