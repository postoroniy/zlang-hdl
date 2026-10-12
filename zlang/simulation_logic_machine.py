# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Viacheslav Vinogradov
"""Bounded four-state execution of the validated primitive simulation plan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from zlang.simulation_logic import (
    LogicBit,
    LogicVector,
    logic_add,
    logic_and,
    logic_compare,
    logic_equal,
    logic_mul,
    logic_not,
    logic_or,
    logic_select,
    logic_shift,
    logic_sub,
    logic_truthy,
    logic_xor,
)
from zlang.simulation_values import SimulationRuntimeError


def _limbs_to_int(limbs: list[int]) -> int:
    return sum(int(limb) << (64 * index) for index, limb in enumerate(limbs))


@dataclass(frozen=True)
class LogicMachineSnapshot:
    states: dict[str, LogicVector]
    memories: dict[str, tuple[LogicVector, ...]]
    resets: dict[str, LogicVector]
    releases: dict[str, int]
    event_flags: dict[str, LogicVector]
    event_index: int
    trace: tuple[dict[str, object], ...]
    trace_last: dict[str, LogicVector]
    refreshed_registers: frozenset[str]
    written_memory_cells: frozenset[tuple[str, int]]


class LogicStateMachine:
    """Own the opt-in logic planes for one native simulation instance.

    The plan is already schema-validated before construction.  This owner does
    not parse ZLang or rediscover scheduling; it executes the same primitive
    nodes/effects that the binary Cranelift engine consumes.
    """

    def __init__(
        self,
        payload: Mapping[str, object],
        *,
        initial_registers: Mapping[str, LogicVector] | None = None,
    ) -> None:
        self._payload = payload
        self._nodes = tuple(payload["nodes"])  # type: ignore[arg-type]
        self._regions = tuple(payload["regions"])  # type: ignore[arg-type]
        self._outputs = tuple(payload["outputs"])  # type: ignore[arg-type]
        self._edge_programs = {
            str(item["clock"]): item
            for item in payload["edge_programs"]  # type: ignore[index]
        }
        self._domains = {
            str(item["clock"]): item for item in payload["domains"]  # type: ignore[index]
        }
        self.inputs: dict[str, LogicVector] = {
            str(item["name"]): LogicVector.known(0, int(item["width"]))
            for item in payload["ports"]  # type: ignore[index]
            if item["direction"] == "input"
        }
        self.states: dict[str, LogicVector] = {}
        for item in payload["registers"]:  # type: ignore[index]
            name = str(item["name"])
            width = int(item["width"])
            if bool(item["resettable"]):
                self.states[name] = LogicVector.known(
                    _limbs_to_int(item["initial_limbs"]), width
                )
            else:
                self.states[name] = LogicVector.filled(LogicBit.UNINITIALIZED, width)
        for name, value in (initial_registers or {}).items():
            self.states[name] = value
        self.memories: dict[str, list[LogicVector]] = {
            str(item["name"]): [
                LogicVector.known(_limbs_to_int(item["initial_limbs"]), int(item["width"]))
                for _ in range(int(item["depth"]))
            ]
            for item in payload["memories"]  # type: ignore[index]
        }
        self.resets = {
            str(item["reset"]): LogicVector.known(0, 1)
            for item in payload["domains"]  # type: ignore[index]
            if item["reset"] is not None
        }
        self.releases = {str(item["clock"]): 0 for item in payload["domains"]}  # type: ignore[index]
        self.event_flags = {str(item["clock"]): LogicVector.known(0, 1) for item in payload["domains"]}  # type: ignore[index]
        self.output_values: dict[str, LogicVector] = {}
        self.event_index = 0
        self._trace_signals: tuple[str, ...] = ()
        self._trace: list[dict[str, object]] = []
        self._trace_last: dict[str, LogicVector] = {}
        self.refreshed_registers: frozenset[str] = frozenset()
        self.written_memory_cells: frozenset[tuple[str, int]] = frozenset()
        self.evaluate()

    def snapshot(self) -> LogicMachineSnapshot:
        return LogicMachineSnapshot(
            dict(self.states),
            {name: tuple(cells) for name, cells in self.memories.items()},
            dict(self.resets),
            dict(self.releases),
            dict(self.event_flags),
            self.event_index,
            tuple(dict(item) for item in self._trace),
            dict(self._trace_last),
            self.refreshed_registers,
            self.written_memory_cells,
        )

    def restore(self, snapshot: LogicMachineSnapshot) -> None:
        self.states = dict(snapshot.states)
        self.memories = {name: list(cells) for name, cells in snapshot.memories.items()}
        self.resets = dict(snapshot.resets)
        self.releases = dict(snapshot.releases)
        self.event_flags = dict(snapshot.event_flags)
        self.event_index = snapshot.event_index
        self._trace = [dict(item) for item in snapshot.trace]
        self._trace_last = dict(snapshot.trace_last)
        self.refreshed_registers = snapshot.refreshed_registers
        self.written_memory_cells = snapshot.written_memory_cells
        self.evaluate()

    def set_input(self, name: str, value: LogicVector) -> None:
        expected = self.inputs.get(name)
        if expected is None:
            raise SimulationRuntimeError(f"port '{name}' is not an input")
        if expected.width != value.width:
            raise SimulationRuntimeError(
                f"logic value for '{name}' has width {value.width}; expected {expected.width}"
            )
        self.inputs[name] = value

    def set_state(self, name: str, value: LogicVector) -> None:
        expected = self.states.get(name)
        if expected is None:
            raise SimulationRuntimeError(f"unknown register state '{name}'")
        if expected.width != value.width:
            raise SimulationRuntimeError(
                f"logic value for register '{name}' has width {value.width}; expected {expected.width}"
            )
        self.states[name] = value
        self.evaluate()

    def get(self, name: str) -> LogicVector:
        if name in self.output_values:
            return self.output_values[name]
        if name in self.states:
            return self.states[name]
        if name in self.inputs:
            return self.inputs[name]
        raise SimulationRuntimeError(f"unknown signal '{name}'")

    def evaluate(self) -> dict[str, LogicVector]:
        values = self._evaluate_nodes(self._nodes, captures=(), binder=None)
        self.output_values = {
            str(item["name"]): values[int(item["node"])] for item in self._outputs
        }
        return dict(self.output_values)

    def evaluation_event(self) -> dict[str, LogicVector]:
        """Evaluate one edge-free batch event and advance trace time once."""

        self.refreshed_registers = frozenset()
        self.written_memory_cells = frozenset()
        self.event_index += 1
        outputs = self.evaluate()
        self._sample_trace()
        return outputs

    def reset(self, name: str, asserted: bool) -> None:
        if name not in self.resets:
            raise SimulationRuntimeError(f"unknown reset '{name}'")
        self.resets[name] = LogicVector.known(int(asserted), 1)
        self.refreshed_registers = frozenset()
        self.written_memory_cells = frozenset()
        async_clocks = [
            clock
            for clock, domain in self._domains.items()
            if domain["reset"] == name
            and domain["reset_mode"] == "asynchronous"
            and asserted
        ]
        for clock, domain in self._domains.items():
            if domain["reset"] == name and domain["reset_release_mode"] == "synchronized":
                self.releases[clock] = 0 if asserted else int(domain["reset_release_cycles"])
        if async_clocks:
            self.edge(async_clocks, count_event=False)
        else:
            self.evaluate()
        self._sample_trace()

    def edge(self, clocks: list[str], *, count_event: bool = True) -> None:
        unknown = next((clock for clock in clocks if clock not in self._edge_programs), None)
        if unknown is not None:
            raise SimulationRuntimeError(f"unknown clock '{unknown}'")
        if len(clocks) != len(set(clocks)):
            raise SimulationRuntimeError("one event cannot contain a clock twice")
        for clock in clocks:
            self.event_flags[clock] = LogicVector.known(1, 1)
        before_states = dict(self.states)
        before_memories = {name: list(cells) for name, cells in self.memories.items()}
        committed_states = dict(before_states)
        committed_memories = {name: list(cells) for name, cells in before_memories.items()}
        refreshed_registers: set[str] = set()
        written_memory_cells: set[tuple[str, int]] = set()
        for clock in clocks:
            self.states = dict(before_states)
            self.memories = {name: list(cells) for name, cells in before_memories.items()}
            values = self._evaluate_nodes(self._nodes, captures=(), binder=None)
            program = self._edge_programs[clock]
            error = logic_truthy(values[int(program["error"])])
            self._require_control(error, f"runtime error condition for clock '{clock}'")
            if error.value:
                raise SimulationRuntimeError("primitive instrumentation check failed")
            for effect in program["effects"]:
                op = effect["op"]
                if op == "commit_state":
                    name = str(effect["target"])
                    old = before_states[name]
                    refresh = (
                        LogicVector.known(1, 1)
                        if effect["refresh"] is None
                        else logic_truthy(values[int(effect["refresh"])])
                    )
                    self._require_control(refresh, f"register '{name}' write condition")
                    new = values[int(effect["node"])]
                    refreshed = bool(refresh.value)
                    committed_states[name] = (
                        new.accepted_write() if refreshed else old
                    )
                    if refreshed:
                        refreshed_registers.add(name)
                elif op == "store_memory":
                    enable = logic_truthy(values[int(effect["enable"])])
                    self._require_control(enable, f"memory '{effect['memory']}' write enable")
                    if enable.value:
                        address = values[int(effect["address"])]
                        self._require_control_known(address, f"memory '{effect['memory']}' address")
                        cells = committed_memories[str(effect["memory"])]
                        if address.value < len(cells):
                            cells[address.value] = values[int(effect["node"])].accepted_write()
                            written_memory_cells.add(
                                (str(effect["memory"]), address.value)
                            )
                elif op == "fill_memory":
                    enable = logic_truthy(values[int(effect["enable"])])
                    self._require_control(enable, f"memory '{effect['memory']}' fill enable")
                    if enable.value:
                        value = values[int(effect["node"])].accepted_write()
                        committed_memories[str(effect["memory"])] = [
                            value for _ in committed_memories[str(effect["memory"])]
                        ]
                        written_memory_cells.update(
                            (str(effect["memory"]), index)
                            for index in range(len(committed_memories[str(effect["memory"])]))
                        )
                else:
                    raise SimulationRuntimeError(f"unsupported logic-state effect '{op}'")
        self.states = committed_states
        self.memories = committed_memories
        self.refreshed_registers = frozenset(refreshed_registers)
        self.written_memory_cells = frozenset(written_memory_cells)
        for clock in clocks:
            self.event_flags[clock] = LogicVector.known(0, 1)
            if self.releases[clock] > 0:
                self.releases[clock] -= 1
        if count_event:
            self.event_index += 1
        self.evaluate()
        self._sample_trace()


    @staticmethod
    def _require_control(value: LogicVector, label: str) -> None:
        if value.unknown:
            kind = "U" if value.has_uninitialized and not value.has_unknown else "X"
            raise SimulationRuntimeError(f"{label} is {kind}; stateful control must be binary")

    @staticmethod
    def _require_control_known(value: LogicVector, label: str) -> None:
        if value.unknown:
            kind = "U" if value.has_uninitialized and not value.has_unknown else "X"
            raise SimulationRuntimeError(f"{label} contains {kind}; stateful control must be binary")

    def _evaluate_nodes(
        self,
        nodes: tuple[Mapping[str, object], ...],
        *,
        captures: tuple[LogicVector, ...],
        binder: int | None,
    ) -> list[LogicVector]:
        values: list[LogicVector] = []
        for node in nodes:
            op = str(node["op"])
            width = int(node["width"])
            operands = [values[int(item)] for item in node["operands"]]  # type: ignore[index]
            attributes = node["attributes"]
            if op == "constant":
                result = LogicVector.known(_limbs_to_int(attributes["limbs"]), width)
            elif op == "load_input":
                name = str(attributes["name"])
                if name.startswith("$reset:"):
                    result = self.resets[name.removeprefix("$reset:")]
                else:
                    result = self.inputs[name]
            elif op == "load_state":
                name = str(attributes["name"])
                if name.startswith("$release:"):
                    result = LogicVector.known(self.releases[name.removeprefix("$release:")], width)
                else:
                    result = self.states[name]
            elif op == "load_event":
                result = self.event_flags[str(attributes["name"])]
            elif op == "load_memory":
                address = operands[0]
                self._require_control_known(address, f"memory '{attributes['memory']}' read address")
                cells = self.memories[str(attributes["memory"])]
                result = cells[address.value if address.value < len(cells) else 0]
            elif op == "load_capture":
                result = captures[int(attributes["slot"])]
            elif op == "load_index":
                if binder is None:
                    raise SimulationRuntimeError("logic-state binder index is unavailable")
                result = LogicVector.known(binder, width)
            elif op == "loop_region":
                region = self._regions[int(attributes["region"])]
                result = LogicVector.known(0, width)
                for index in range(int(region["start"]), int(region["stop"])):
                    nested = self._evaluate_nodes(
                        tuple(region["nodes"]),
                        captures=tuple(operands),
                        binder=index,
                    )[int(region["root"])]
                    offset = (index - int(region["start"])) * int(region["element_width"])
                    result = self._insert_known(result, nested, offset)
            elif op == "add":
                result = logic_add(operands[0], operands[1], width)
            elif op == "sub":
                result = logic_sub(operands[0], operands[1], width)
            elif op == "mul":
                result = logic_mul(operands[0], operands[1], width)
            elif op == "and":
                result = logic_and(operands[0], operands[1])
            elif op == "or":
                result = logic_or(operands[0], operands[1])
            elif op == "xor":
                result = logic_xor(operands[0], operands[1])
            elif op == "not":
                result = logic_not(operands[0])
            elif op in {"shl", "lshr", "ashr"}:
                result = logic_shift(
                    operands[0], operands[1], width,
                    left=op == "shl", arithmetic=op == "ashr",
                )
            elif op == "eq":
                result = logic_equal(operands[0], operands[1])
            elif op in {"ult", "ule", "slt", "sle"}:
                result = logic_compare(
                    operands[0], operands[1],
                    signed=op.startswith("s"), or_equal=op.endswith("le"),
                )
            elif op == "select":
                result = logic_select(logic_truthy(operands[0]), operands[1], operands[2])
            elif op == "extract_bits":
                result = self._extract_dynamic(operands[0], operands[1], width)
            elif op == "insert_bits":
                result = self._insert_dynamic(operands[0], operands[1], operands[2])
            elif op == "concat_bits":
                result = operands[0]
                for operand in operands[1:]:
                    result = result.concat(operand)
            else:
                raise SimulationRuntimeError(f"unsupported logic-state primitive '{op}'")
            values.append(result.resize(width))
        return values

    @staticmethod
    def _insert_known(base: LogicVector, replacement: LogicVector, offset: int) -> LogicVector:
        if offset < 0 or offset + replacement.width > base.width:
            return base
        mask = replacement.mask << offset
        return LogicVector(
            base.width,
            (base.value & ~mask) | (replacement.value << offset),
            (base.unknown & ~mask) | (replacement.unknown << offset),
        )

    def _extract_dynamic(
        self, value: LogicVector, offset: LogicVector, width: int
    ) -> LogicVector:
        shifted = logic_shift(value, offset, value.width, left=False, arithmetic=False)
        return shifted.resize(width)

    def _insert_dynamic(
        self, base: LogicVector, replacement: LogicVector, offset: LogicVector
    ) -> LogicVector:
        if offset.is_binary:
            return self._insert_known(base, replacement, offset.value)
        candidates = [
            self._insert_known(base, replacement, item)
            for item in range(base.width - replacement.width + 1)
            if self._matches_offset(offset, item)
        ]
        if (offset.value | offset.unknown) > base.width - replacement.width:
            candidates.append(base)
        if not candidates:
            return base
        result = candidates[0]
        condition = LogicVector.filled(
            LogicBit.UNKNOWN if offset.has_unknown else LogicBit.UNINITIALIZED, 1
        )
        for candidate in candidates[1:]:
            result = logic_select(condition, result, candidate)
        return result

    @staticmethod
    def _matches_offset(offset: LogicVector, value: int) -> bool:
        known_mask = offset.mask & ~offset.unknown
        return (value & known_mask) == (offset.value & known_mask)

    def enable_trace(self, signals: tuple[str, ...]) -> None:
        for name in signals:
            self.get(name)
        self._trace_signals = signals
        self._trace.clear()
        self._trace_last.clear()
        self._sample_trace(force=True)

    def _sample_trace(self, *, force: bool = False) -> None:
        if not self._trace_signals:
            return
        values = {name: self.get(name) for name in self._trace_signals}
        if not force and values == self._trace_last:
            return
        self._trace_last = values
        self._trace.append({"$event": self.event_index, **values})

    def drain_trace(self) -> list[dict[str, object]]:
        result = self._trace
        self._trace = []
        return result


__all__ = ["LogicStateMachine"]
