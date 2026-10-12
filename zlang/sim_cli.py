"""Command-line entrypoint for persistent ZLang simulation."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
import json
from itertools import islice
from pathlib import Path
import sys

from zlang import sim
from zlang._version import __version__


MAX_EXPANDED_EVENTS = 1_000_000


@dataclass(frozen=True)
class _EventRecord:
    event: Mapping[str, object]
    repeat: int
    line_number: int


@dataclass(frozen=True)
class EventSchedule(Sequence[Mapping[str, object]]):
    """A bounded JSONL schedule that retains one mapping per physical line.

    Iteration expands repeat counts deterministically without allocating one
    copied nested dictionary per logical event. The same immutable-by-contract
    mapping may therefore be yielded more than once.
    """

    records: tuple[_EventRecord, ...]
    event_count: int

    def __len__(self) -> int:
        return self.event_count

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        for record in self.records:
            for _ in range(record.repeat):
                yield record.event

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, object] | tuple[Mapping[str, object], ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self.event_count)
            if step == 1:
                return tuple(islice(self, start, stop))
            return tuple(self[item] for item in range(start, stop, step))
        normalized = index + self.event_count if index < 0 else index
        if normalized < 0 or normalized >= self.event_count:
            raise IndexError(index)
        offset = 0
        for record in self.records:
            next_offset = offset + record.repeat
            if normalized < next_offset:
                return record.event
            offset = next_offset
        raise IndexError(index)

    def with_first_updates(self, updates: Mapping[str, object]) -> EventSchedule:
        """Apply CLI initial inputs only to logical event zero."""

        if not self.records or not updates:
            return self
        first, *remaining = self.records
        event = dict(first.event)
        supplied = event.get("set", {})
        if not isinstance(supplied, Mapping):
            raise sim.SimulationRuntimeError("event set field must be a mapping")
        merged = dict(updates)
        merged.update(supplied)
        event["set"] = merged
        records = [_EventRecord(event, 1, first.line_number)]
        if first.repeat > 1:
            records.append(
                _EventRecord(first.event, first.repeat - 1, first.line_number)
            )
        records.extend(remaining)
        return EventSchedule(tuple(records), self.event_count)


def _engine(value: str) -> str:
    if value == "native":
        return value
    if value in {"reference", "python", "jit"}:
        raise argparse.ArgumentTypeError(
            f"engine '{value}' was removed; use native and optionally "
            "--compare-with iverilog|verilator"
        )
    raise argparse.ArgumentTypeError("expected 'native'")


def _assignment(value: str) -> tuple[str, object]:
    name, separator, payload = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError("expected NAME=JSON")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise argparse.ArgumentTypeError(
            f"value for '{name}' is not valid JSON: {error.msg}"
        ) from error
    return name, decoded


def _logic_assignment(value: str) -> tuple[str, str]:
    name, separator, payload = value.partition("=")
    if not separator or not name or not payload:
        raise argparse.ArgumentTypeError("expected NAME=01ux")
    if any(item.lower() not in "01ux_" for item in payload):
        raise argparse.ArgumentTypeError("logic value may contain only 0, 1, u, x, and _")
    return name, payload


def _read_events(path: Path) -> EventSchedule:
    records: list[_EventRecord] = []
    event_count = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise sim.SimulationRuntimeError(
                f"{path}:{line_number}: invalid event JSON: {error.msg}"
            ) from error
        if not isinstance(event, dict):
            raise sim.SimulationRuntimeError(
                f"{path}:{line_number}: event must be a JSON object"
            )
        repeat = event.get("repeat", 1)
        if isinstance(repeat, bool) or not isinstance(repeat, int) or repeat <= 0:
            raise sim.SimulationRuntimeError(
                f"{path}:{line_number}: event repeat must be a positive integer"
            )
        if event_count + repeat > MAX_EXPANDED_EVENTS:
            raise sim.SimulationRuntimeError(
                f"{path}:{line_number}: expanded event schedule exceeds "
                f"{MAX_EXPANDED_EVENTS} events"
            )
        normalized = dict(event)
        normalized.pop("repeat", None)
        records.append(_EventRecord(normalized, repeat, line_number))
        event_count += repeat
    return EventSchedule(tuple(records), event_count)


def _comparison_events(
    arguments: argparse.Namespace,
) -> Sequence[Mapping[str, object]]:
    """Normalize the public CLI modes into the event contract used by RTL."""

    initial = dict(arguments.assignments)
    if arguments.events is not None:
        events = _read_events(arguments.events)
        return events.with_first_updates(initial)
    if arguments.cycles is not None:
        count = max(arguments.cycles, 1)
        return tuple(
            {
                **({"set": initial} if index == 0 and initial else {}),
                **({"edges": (arguments.clock,)} if arguments.cycles else {}),
            }
            for index in range(count)
        )
    return ({"set": initial} if initial else {},)


def _vcd_identifier(index: int) -> str:
    alphabet = tuple(chr(item) for item in range(33, 127))
    result = ""
    value = index
    while True:
        result += alphabet[value % len(alphabet)]
        value //= len(alphabet)
        if value == 0:
            return result


def _json_values(value: object) -> object:
    if isinstance(value, sim.LogicVector):
        return value.to_bits()
    if isinstance(value, dict):
        return {str(name): _json_values(item) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_values(item) for item in value]
    return value


def _unique_assignments(
    values: list[tuple[str, object]], *, label: str
) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in values:
        if name in result:
            raise sim.SimulationRuntimeError(
                f"{label} specifies '{name}' more than once"
            )
        result[name] = value
    return result


def _control_levels(
    instance: sim.Simulator,
) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, int]]]:
    """Return physical idle/active levels for compiler-owned clocks and resets."""

    clocks: dict[str, tuple[int, int]] = {}
    resets: dict[str, tuple[int, int]] = {}
    for domain in instance.program.module.clock_domains:
        active_clock = 1 if domain.edge.value == "rising" else 0
        clock_levels = (1 - active_clock, active_clock)
        previous_clock = clocks.setdefault(domain.clock, clock_levels)
        if previous_clock != clock_levels:
            raise sim.SimulationRuntimeError(
                f"clock '{domain.clock}' has inconsistent physical edges"
            )
        if domain.reset is None:
            continue
        active_reset = 1 if domain.reset_polarity.value == "active_high" else 0
        reset_levels = (1 - active_reset, active_reset)
        previous_reset = resets.setdefault(domain.reset, reset_levels)
        if previous_reset != reset_levels:
            raise sim.SimulationRuntimeError(
                f"reset '{domain.reset}' has inconsistent physical polarities"
            )
    return clocks, resets


def _write_vcd(
    path: Path,
    instance: sim.Simulator,
    samples: list[dict[str, object]],
    *,
    events: Sequence[Mapping[str, object]] = (),
) -> None:
    widths = instance.trace_widths
    clocks, resets = _control_levels(instance)
    traced_names = sorted(
        {name for sample in samples for name in sample if name != "$event"}
    )
    missing = [name for name in traced_names if name not in widths]
    if missing:
        raise sim.SimulationRuntimeError(
            f"trace signal '{missing[0]}' has no compiler-owned packed width"
        )
    control_names = sorted(set(clocks) | set(resets))
    names = sorted(set(traced_names) | set(control_names))
    widths = {**widths, **dict.fromkeys(control_names, 1)}
    logic_trace = any(
        isinstance(value, sim.LogicVector)
        for sample in samples
        for name, value in sample.items()
        if name != "$event"
    )
    identifiers = {name: _vcd_identifier(index) for index, name in enumerate(names)}
    u_identifiers = {
        name: _vcd_identifier(len(names) + index)
        for index, name in enumerate(traced_names)
    }
    lines = [
        "$date deterministic $end",
        "$version ZLang HDL simulation $end",
        (
            "$comment Event-index visualization: each simulation event occupies "
            "two 1ns ticks; clock and reset transitions represent compiler event "
            "semantics, not analog timing. $end"
        ),
        "$timescale 1ns $end",
        f"$scope module {instance.program.module.name} $end",
    ]
    lines.extend(
        f"$var wire {widths[name]} {identifiers[name]} {name} $end"
        for name in names
    )
    lines.append("$upscope $end")
    if logic_trace:
        lines.append("$scope module __zlang_meta $end")
        lines.extend(
            f"$var wire {widths[name]} {u_identifiers[name]} {name}_u_mask $end"
            for name in traced_names
        )
        lines.append("$upscope $end")
    lines.append("$enddefinitions $end")

    timeline: dict[int, list[str]] = {}

    def change(time: int, text: str) -> None:
        timeline.setdefault(time, []).append(text)

    for name, (idle, _active) in clocks.items():
        change(0, f"b{idle} {identifiers[name]}")
    reset_levels = {name: idle for name, (idle, _active) in resets.items()}
    for name, value in reset_levels.items():
        change(0, f"b{value} {identifiers[name]}")

    for sample in samples:
        time = 2 * int(sample["$event"])
        for name in traced_names:
            value = sample.get(name, 0)
            if isinstance(value, sim.LogicVector):
                bits = value.to_bits()
                change(
                    time,
                    f"b{bits.replace('u', 'x')} {identifiers[name]}"
                )
                u_mask = "".join("1" if item == "u" else "0" for item in bits)
                change(time, f"b{u_mask} {u_identifiers[name]}")
            else:
                change(
                    time,
                    f"b{int(value):0{widths[name]}b} {identifiers[name]}",
                )

    for event_index, event in enumerate(events, 1):
        reset_updates = event.get("reset", {})
        if isinstance(reset_updates, dict):
            for raw_name, asserted in sorted(reset_updates.items()):
                name = str(raw_name)
                if name not in resets or not isinstance(asserted, bool):
                    continue
                idle, active = resets[name]
                value = active if asserted else idle
                if reset_levels[name] != value:
                    reset_levels[name] = value
                    change(2 * event_index - 1, f"b{value} {identifiers[name]}")
        raw_edges = event.get("edges", ())
        if not isinstance(raw_edges, Sequence) or isinstance(raw_edges, (str, bytes)):
            continue
        for raw_name in raw_edges:
            name = str(raw_name)
            if name not in clocks:
                continue
            idle, active = clocks[name]
            change(2 * event_index, f"b{active} {identifiers[name]}")
            change(2 * event_index + 1, f"b{idle} {identifiers[name]}")

    for time in sorted(timeline):
        lines.append(f"#{time}")
        lines.extend(timeline[time])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zlang sim",
        description="Run the persistent ZLang native simulator",
        epilog=(
            "examples:\n"
            "  zlang sim counter.zhl --top Counter --clock clk --cycles 100 --json\n"
            "  zlang sim system.zhl --top System --events events.jsonl\n"
            "  zlang sim comb.zhl --top Comb --set 'input=42' --json\n\n"
            "Use --cycles only for a single-clock top. For multi-clock designs,\n"
            "use --events; coincident edges may be listed in one JSONL event."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("source", type=Path, help="input .zhl file")
    source_options = parser.add_argument_group("source selection")
    source_options.add_argument("--top", help="select the top module")
    source_options.add_argument(
        "--project", type=Path, help="use this zlang.toml or containing directory"
    )
    source_options.add_argument(
        "--profile", help="select an implementation profile from zlang.toml"
    )
    execution_options = parser.add_argument_group("simulation execution")
    execution_options.add_argument(
        "--engine",
        type=_engine,
        metavar="native",
        default="native",
        # Native execution is the sole simulation path.  Keep the old selector
        # as a hidden migration guard for one alpha rather than advertising a
        # choice that no longer exists.
        help=argparse.SUPPRESS,
    )
    execution_options.add_argument(
        "--strict-uninitialized",
        action="store_true",
        help=(
            "enable per-bit logic state and fail when a typed read observes U/X"
        ),
    )
    execution_options.add_argument(
        "--logic-state",
        action="store_true",
        help="enable opt-in per-bit 0/1/U/X simulation",
    )
    execution_options.add_argument(
        "--clock", help="clock to tick when using --cycles"
    )
    execution_options.add_argument(
        "--cycles", type=int, help="run N cycles (single-clock tops only)"
    )
    execution_options.add_argument(
        "--events",
        type=Path,
        help="run the JSONL event schedule at PATH (supports bounded repeat)",
    )
    execution_options.add_argument(
        "--set",
        dest="assignments",
        action="append",
        type=_assignment,
        default=[],
        metavar="NAME=JSON",
        help="set an input before execution; may be repeated",
    )
    execution_options.add_argument(
        "--set-logic",
        dest="logic_assignments",
        action="append",
        type=_logic_assignment,
        default=[],
        metavar="NAME=01ux",
        help="set one packed input with 0/1/U/X bits; may be repeated",
    )
    execution_options.add_argument(
        "--initial-reg",
        dest="initial_registers",
        action="append",
        type=_logic_assignment,
        default=[],
        metavar="NAME=01ux",
        help="override one initial register value; may be repeated",
    )
    execution_options.add_argument(
        "--compare-with",
        choices=("iverilog", "verilator"),
        help="compare native results with generated RTL in this simulator",
    )
    execution_options.add_argument(
        "--compare-artifacts",
        type=Path,
        metavar="DIR",
        help="retain comparison RTL, testbench, logs, and manifest in DIR",
    )
    execution_options.add_argument(
        "--compare-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="bound each external comparison command (default: 120)",
    )
    output_options = parser.add_argument_group("simulation output")
    output_options.add_argument(
        "--trace",
        type=Path,
        help="write simulation transitions plus physical clocks/resets as VCD",
    )
    output_options.add_argument(
        "--trace-signal",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "include only this data/state signal in VCD output; may be repeated "
            "(physical clocks/resets are always included)"
        ),
    )
    output_options.add_argument(
        "--json", action="store_true", help="print compact JSON output"
    )
    arguments = parser.parse_args(argv)
    if arguments.cycles is not None and arguments.events is not None:
        parser.error("--cycles and --events are mutually exclusive")
    if arguments.cycles is not None and arguments.clock is None:
        parser.error("--cycles requires --clock")
    if arguments.cycles is not None and arguments.cycles < 0:
        parser.error("--cycles must not be negative")
    if arguments.compare_artifacts is not None and arguments.compare_with is None:
        parser.error("--compare-artifacts requires --compare-with")
    if arguments.compare_timeout <= 0:
        parser.error("--compare-timeout must be positive")
    ordinary_names = {name for name, _ in arguments.assignments}
    logic_names = {name for name, _ in arguments.logic_assignments}
    if ordinary_names & logic_names:
        parser.error(
            f"input '{sorted(ordinary_names & logic_names)[0]}' is present in both "
            "--set and --set-logic"
        )
    if (arguments.logic_assignments or arguments.initial_registers) and not (
        arguments.logic_state or arguments.strict_uninitialized
    ):
        parser.error("--set-logic/--initial-reg require --logic-state")
    if arguments.trace_signal and arguments.trace is None:
        parser.error("--trace-signal requires --trace")
    if arguments.compare_with is not None and (
        arguments.logic_state
        or arguments.strict_uninitialized
        or arguments.logic_assignments
        or arguments.initial_registers
    ):
        parser.error("external RTL comparison currently accepts binary simulation only")
    try:
        initial_registers = _unique_assignments(
            arguments.initial_registers,
            label="--initial-reg",
        )
        program = sim.compile(
            arguments.source,
            top=arguments.top,
            project=arguments.project,
            profile=arguments.profile,
            engine=arguments.engine,
            strict_uninitialized=arguments.strict_uninitialized,
            logic_state=arguments.logic_state,
        )
        if (
            arguments.cycles is not None
            and len(program.module.clock_domains) != 1
        ):
            raise sim.SimulationRuntimeError(
                "--cycles is valid only for a single-clock top; use --events"
            )
        if arguments.compare_with is not None:
            from zlang.simulation_compare import compare_program

            comparison_events = _comparison_events(arguments)
            comparison = compare_program(
                program,
                comparison_events,
                simulator=arguments.compare_with,
                artifact_directory=arguments.compare_artifacts,
                timeout=arguments.compare_timeout,
            )
            if arguments.events is not None:
                outputs = list(comparison.native_outputs)
            else:
                outputs = comparison.native_outputs[-1]
        else:
            outputs = None

        # Trace generation retains the established native trace contract.  A
        # comparison uses a fresh native instance so the external proof and
        # optional VCD cannot share mutable state accidentally.
        if arguments.compare_with is None or arguments.trace is not None:
            instance = program.create(initial_registers=initial_registers)
            with instance:
                for name, value in arguments.assignments:
                    instance.set(name, value)
                for name, value in arguments.logic_assignments:
                    instance.set_logic(name, value)
                if arguments.trace is not None:
                    instance.enable_trace(arguments.trace_signal or None)
                trace_events: Sequence[Mapping[str, object]] = ()
                if arguments.events is not None:
                    trace_events = _read_events(arguments.events)
                    native_outputs: object = instance.run_events(
                        trace_events
                    )
                elif arguments.cycles is not None:
                    trace_events = tuple(
                        {"edges": (arguments.clock,)}
                        for _ in range(arguments.cycles)
                    )
                    native_outputs = instance.run_cycles(
                        arguments.clock, arguments.cycles
                    )
                else:
                    if program.logic_state and not program.strict_uninitialized:
                        instance._native.eval()
                        assert instance._logic is not None
                        instance._logic.evaluate()
                        native_outputs = instance.outputs_logic()
                    else:
                        native_outputs = instance.eval()
                if arguments.compare_with is None:
                    outputs = native_outputs
                if arguments.trace is not None:
                    samples = (
                        instance.drain_logic_trace()
                        if program.logic_state
                        else instance.drain_trace()
                    )
                    _write_vcd(
                        arguments.trace,
                        instance,
                        samples,
                        events=trace_events,
                    )
        assert outputs is not None
    except (
        OSError,
        ValueError,
        sim.SimulationPlanError,
        sim.SimulationRuntimeError,
    ) as error:
        print(f"zlang sim: error: {error}", file=sys.stderr)
        return 2
    if arguments.json:
        print(json.dumps(_json_values(outputs), sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(_json_values(outputs), sort_keys=True, indent=2))
    return 0


__all__ = ["main"]
