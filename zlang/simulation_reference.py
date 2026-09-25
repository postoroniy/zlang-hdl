"""Open reference executor for the primitive SimulationPlan machine.

This module intentionally knows nothing about ZLang source constructs.  It
executes the same width-explicit packed bit-vector plan consumed by the native
runtime and exists as a portable oracle for differential validation.
"""

from __future__ import annotations

from dataclasses import dataclass

from zlang.simulation_plan import SimulationPlan
from zlang.simulation_primitives import (
    int_from_limbs as _from_limbs,
    int_to_limbs as _to_limbs,
)


_MAX_TRACE_EVENTS = 1_000_000
_MAX_INSTRUMENTATION_EVENTS = 1_000_000


def _mask(width: int) -> int:
    return (1 << width) - 1


def _signed(value: int, width: int) -> int:
    value &= _mask(width)
    sign = 1 << (width - 1)
    return value - (1 << width) if value & sign else value


@dataclass(frozen=True)
class ReferenceProgram:
    """Validated primitive plan reusable across reference instances."""

    plan: SimulationPlan

    @property
    def identity(self) -> str:
        return self.plan.identity

    @property
    def module_name(self) -> str:
        return str(self.plan.payload["module"])

    def create(self) -> "ReferenceInstance":
        return ReferenceInstance(self)


class ReferenceInstance:
    """Persistent interpreter state for one primitive plan."""

    def __init__(self, program: ReferenceProgram) -> None:
        self.program = program
        payload = program.plan.payload
        self._ports = {item["name"]: item for item in payload["ports"]}
        self._outputs = {item["name"]: item["node"] for item in payload["outputs"]}
        self._register_info = {
            item["name"]: item for item in payload["registers"]
        }
        self._memory_info = {item["name"]: item for item in payload["memories"]}
        self._domains = {item["clock"]: item for item in payload["domains"]}
        self._edge_programs = {
            item["clock"]: item for item in payload["edge_programs"]
        }
        self._inputs = {
            name: 0
            for name, port in self._ports.items()
            if port["direction"] == "input"
        }
        self._registers = {
            name: _from_limbs(item["initial_limbs"])
            for name, item in self._register_info.items()
        }
        self._memories = {
            name: [_from_limbs(item["initial_limbs"])] * int(item["depth"])
            for name, item in self._memory_info.items()
        }
        self._resets = {
            str(item["reset"]): False
            for item in payload["domains"]
            if item["reset"] is not None
        }
        self._release = {name: 0 for name in self._domains}
        self._active_events: set[str] = set()
        self._output_values = {name: 0 for name in self._outputs}
        self._trace_enabled = False
        self._trace_signals: list[str] = []
        self._trace: list[dict[str, list[int]]] = []
        self._trace_last: dict[str, list[int]] = {}
        self._event_index = 0
        self._instrumentation_events: list[tuple[int, int, str]] = []
        self._covered_events: set[int] = set()
        self._closed = False
        self.eval()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("reference simulation instance is closed")

    def _read_signal(self, name: str) -> tuple[int, int]:
        if name in self._inputs:
            return self._inputs[name], int(self._ports[name]["width"])
        if name in self._output_values:
            return self._output_values[name], int(self._ports[name]["width"])
        if name in self._registers:
            return self._registers[name], int(self._register_info[name]["width"])
        raise ValueError(f"unknown signal '{name}'")

    def set_limbs(self, name: str, limbs: list[int]) -> None:
        self._require_open()
        if name not in self._inputs:
            raise ValueError(f"port '{name}' is not an input")
        width = int(self._ports[name]["width"])
        expected = (width + 63) // 64
        if len(limbs) != expected or any(
            isinstance(limb, bool) or not isinstance(limb, int) or limb < 0
            or limb >= 1 << 64
            for limb in limbs
        ):
            raise ValueError(f"input '{name}' has invalid packed limbs")
        value = _from_limbs(limbs)
        if value.bit_length() > width:
            raise ValueError(f"value does not fit {width}-bit input '{name}'")
        self._inputs[name] = value

    def get_limbs(self, name: str) -> list[int]:
        self._require_open()
        value, width = self._read_signal(name)
        return _to_limbs(value, width)

    def _evaluate_nodes(
        self,
        *,
        registers: dict[str, int] | None = None,
        memories: dict[str, list[int]] | None = None,
        nodes: list[dict[str, object]] | None = None,
        captures: tuple[int, ...] = (),
        binder_index: int | None = None,
    ) -> list[int]:
        registers = self._registers if registers is None else registers
        memories = self._memories if memories is None else memories
        nodes = self.program.plan.payload["nodes"] if nodes is None else nodes
        values: list[int] = []
        for node in nodes:
            op = node["op"]
            width = int(node["width"])
            operands = [values[index] for index in node["operands"]]
            attrs = node["attributes"]
            result: int
            if op == "constant":
                result = _from_limbs(attrs["limbs"])
            elif op == "load_input":
                name = str(attrs["name"])
                if name.startswith("$reset:"):
                    result = int(self._resets[name.removeprefix("$reset:")])
                else:
                    result = self._inputs[name]
            elif op == "load_state":
                name = str(attrs["name"])
                if name.startswith("$release:"):
                    result = self._release[name.removeprefix("$release:")]
                else:
                    result = registers[name]
            elif op == "load_event":
                result = int(str(attrs["name"]) in self._active_events)
            elif op == "load_memory":
                memory = memories[str(attrs["memory"])]
                address = operands[0]
                result = memory[address if address < len(memory) else 0]
            elif op == "load_capture":
                result = captures[int(attrs["slot"])]
            elif op == "load_index":
                assert binder_index is not None
                result = binder_index
            elif op == "loop_region":
                region = self.program.plan.payload["regions"][int(attrs["region"])]
                result = 0
                element_width = int(region["element_width"])
                for index in range(int(region["start"]), int(region["stop"])):
                    body = self._evaluate_nodes(
                        registers=registers, memories=memories,
                        nodes=region["nodes"], captures=tuple(operands),
                        binder_index=index,
                    )
                    result |= body[int(region["root"])] << (
                        (index - int(region["start"])) * element_width
                    )
            elif op == "add":
                result = operands[0] + operands[1]
            elif op == "sub":
                result = operands[0] - operands[1]
            elif op == "mul":
                result = operands[0] * operands[1]
            elif op == "and":
                result = operands[0] & operands[1]
            elif op == "or":
                result = operands[0] | operands[1]
            elif op == "xor":
                result = operands[0] ^ operands[1]
            elif op == "not":
                result = ~operands[0]
            elif op == "shl":
                result = operands[0] << operands[1]
            elif op == "lshr":
                result = operands[0] >> operands[1]
            elif op == "ashr":
                source_width = int(nodes[node["operands"][0]]["width"])
                result = _signed(operands[0], source_width) >> operands[1]
            elif op == "eq":
                result = int(operands[0] == operands[1])
            elif op in {"ult", "ule"}:
                result = int(
                    operands[0] < operands[1]
                    if op == "ult"
                    else operands[0] <= operands[1]
                )
            elif op in {"slt", "sle"}:
                source_width = int(nodes[node["operands"][0]]["width"])
                left = _signed(operands[0], source_width)
                right = _signed(operands[1], source_width)
                result = int(left < right if op == "slt" else left <= right)
            elif op == "select":
                result = operands[1] if operands[0] else operands[2]
            elif op == "extract_bits":
                result = operands[0] >> operands[1]
            elif op == "insert_bits":
                base_node, replacement_node, _ = node["operands"]
                base_width = int(nodes[base_node]["width"])
                replacement_width = int(nodes[replacement_node]["width"])
                offset = operands[2]
                if offset <= base_width - replacement_width:
                    field_mask = _mask(replacement_width) << offset
                    result = (
                        (operands[0] & ~field_mask)
                        | ((operands[1] & _mask(replacement_width)) << offset)
                    )
                else:
                    result = operands[0]
            elif op == "concat_bits":
                result = 0
                for value, operand_width in zip(
                    operands, attrs["operand_widths"], strict=True
                ):
                    result = (result << int(operand_width)) | value
            else:  # validated plans make this unreachable
                raise RuntimeError(f"unsupported primitive operation '{op}'")
            values.append(result & _mask(width))
        return values

    def eval(self) -> None:
        self._require_open()
        values = self._evaluate_nodes()
        for name, node in self._outputs.items():
            self._output_values[name] = values[node]

    def _run_edge_program(
        self,
        clock: str,
        registers: dict[str, int],
        memories: dict[str, list[int]],
    ) -> tuple[dict[str, int], dict[str, list[int]]]:
        program = self._edge_programs[clock]
        values = self._evaluate_nodes(registers=registers, memories=memories)
        if values[program["error"]]:
            raise RuntimeError("reference simulator returned status 2")
        self._sample_probes(program, values, clock)
        next_registers = dict(registers)
        next_memories = {name: list(cells) for name, cells in memories.items()}
        for effect in program["effects"]:
            if effect["op"] == "commit_state":
                target = str(effect["target"])
                width = int(self._register_info[target]["width"])
                next_registers[target] = values[effect["node"]] & _mask(width)
            elif effect["op"] == "store_memory":
                if values[effect["enable"]]:
                    name = str(effect["memory"])
                    address = values[effect["address"]]
                    cells = next_memories[name]
                    if address < len(cells):
                        width = int(self._memory_info[name]["width"])
                        cells[address] = values[effect["node"]] & _mask(width)
            elif effect["op"] == "fill_memory" and values[effect["enable"]]:
                name = str(effect["memory"])
                width = int(self._memory_info[name]["width"])
                value = values[effect["node"]] & _mask(width)
                next_memories[name] = [value] * len(next_memories[name])
        return next_registers, next_memories

    def _record_instrumentation(self, event: int, clock: str) -> None:
        if len(self._instrumentation_events) >= _MAX_INSTRUMENTATION_EVENTS:
            raise RuntimeError(
                "reference instrumentation buffer reached its "
                f"{_MAX_INSTRUMENTATION_EVENTS}-event limit; drain it before continuing"
            )
        self._instrumentation_events.append((event, self._event_index, clock))

    def _sample_probes(
        self,
        program: dict[str, object],
        values: list[int],
        clock: str,
    ) -> None:
        for probe in program["probes"]:
            condition = bool(values[probe["condition"]])
            if probe["kind"] == "check" and not condition:
                self._record_instrumentation(probe["event"], clock)
                raise RuntimeError("primitive instrumentation check failed")
            if probe["kind"] == "cover" and condition:
                event = probe["event"]
                if not probe["once"] or event not in self._covered_events:
                    self._record_instrumentation(event, clock)
                    if probe["once"]:
                        self._covered_events.add(event)

    def _sample_trace(self) -> None:
        if not self._trace_enabled:
            return
        if len(self._trace) >= _MAX_TRACE_EVENTS:
            raise RuntimeError(
                f"reference trace buffer reached its {_MAX_TRACE_EVENTS}-event limit; "
                "drain it before continuing"
            )
        values = {name: self.get_limbs(name) for name in self._trace_signals}
        if values == self._trace_last:
            return
        self._trace_last = {name: list(value) for name, value in values.items()}
        values["$event"] = [self._event_index]
        self._trace.append(values)

    def edge(self, clock: str) -> None:
        self.edge_many([clock])

    def edge_many(self, clocks: list[str]) -> None:
        self._require_open()
        if len(clocks) != len(set(clocks)):
            raise ValueError("one event cannot contain a clock twice")
        for clock in clocks:
            if clock not in self._edge_programs:
                raise ValueError(f"unknown clock '{clock}'")
        before_registers = dict(self._registers)
        before_memories = {
            name: list(cells) for name, cells in self._memories.items()
        }
        committed_registers = dict(before_registers)
        committed_memories = {
            name: list(cells) for name, cells in before_memories.items()
        }
        before_release = dict(self._release)
        committed_release = dict(before_release)
        self._active_events = set(clocks)
        try:
            for clock in clocks:
                domain_registers, domain_memories = self._run_edge_program(
                    clock, before_registers, before_memories
                )
                for name, info in self._register_info.items():
                    if info["domain"] == clock:
                        committed_registers[name] = domain_registers[name]
                for name, info in self._memory_info.items():
                    if info["domain"] == clock:
                        committed_memories[name] = domain_memories[name]
                if before_release[clock] > 0:
                    committed_release[clock] = before_release[clock] - 1
        finally:
            self._active_events.clear()
        self._registers = committed_registers
        self._memories = committed_memories
        self._release = committed_release
        self._event_index += 1
        self.eval()
        self._sample_trace()

    def reset(self, name: str, asserted: bool) -> None:
        self._require_open()
        if name not in self._resets:
            raise ValueError(f"unknown reset '{name}'")
        self._resets[name] = asserted
        for domain in self._domains.values():
            if domain["reset"] != name:
                continue
            if domain["reset_release_mode"] == "synchronized":
                self._release[domain["clock"]] = (
                    0 if asserted else int(domain["reset_release_cycles"])
                )
            if domain["reset_mode"] == "asynchronous" and asserted:
                registers, memories = self._run_edge_program(
                    domain["clock"], self._registers, self._memories
                )
                self._registers = registers
                self._memories = memories
        self.eval()

    def run_cycles(self, clock: str, count: int) -> None:
        self._require_open()
        for _ in range(count):
            self.edge_many([clock])

    def run_events(
        self,
        events: list[tuple[list[tuple[str, list[int]]], list[tuple[str, bool]], list[str]]],
    ) -> list[dict[str, list[int]]]:
        self._require_open()
        results: list[dict[str, list[int]]] = []
        for updates, resets, clocks in events:
            for name, limbs in updates:
                self.set_limbs(name, limbs)
            for name, asserted in resets:
                self.reset(name, asserted=asserted)
            if clocks:
                self.edge_many(clocks)
            else:
                self.eval()
                self._event_index += 1
            results.append({name: self.get_limbs(name) for name in self._outputs})
        return results

    def enable_trace(self, signals: list[str] | None) -> None:
        self._require_open()
        selected = (
            list(signals)
            if signals is not None
            else [
                *sorted(self._inputs),
                *sorted(self._output_values),
                *sorted(self._registers),
            ]
        )
        for name in selected:
            self._read_signal(name)
        self._trace_signals = selected
        self._trace_last.clear()
        self._trace_enabled = True

    def drain_trace(self) -> list[dict[str, list[int]]]:
        self._require_open()
        result = self._trace
        self._trace = []
        return result

    def drain_events(self) -> list[tuple[int, int, str]]:
        self._require_open()
        result = self._instrumentation_events
        self._instrumentation_events = []
        return result

    def close(self) -> None:
        self._inputs.clear()
        self._registers.clear()
        self._memories.clear()
        self._trace.clear()
        self._trace_last.clear()
        self._instrumentation_events.clear()
        self._covered_events.clear()
        self._closed = True


def compile_reference_plan(plan: SimulationPlan) -> ReferenceProgram:
    """Compile a validated primitive plan into the portable interpreter."""

    # Re-parse canonical bytes so this path exercises the same public schema
    # boundary rather than trusting compiler-owned in-memory dictionaries.
    return ReferenceProgram(SimulationPlan.from_bytes(plan.to_bytes()))


__all__ = ["ReferenceInstance", "ReferenceProgram", "compile_reference_plan"]
