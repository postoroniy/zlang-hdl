"""Command-line entrypoint for persistent ZLang simulation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from zlang import sim
from zlang._version import __version__


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


def _read_events(path: Path) -> list[dict[str, object]]:
    result = []
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
        result.append(event)
    return result


def _comparison_events(arguments: argparse.Namespace) -> tuple[dict[str, object], ...]:
    """Normalize the public CLI modes into the event contract used by RTL."""

    initial = dict(arguments.assignments)
    if arguments.events is not None:
        events = _read_events(arguments.events)
        if events and initial:
            first = dict(events[0])
            updates = dict(initial)
            supplied = first.get("set", {})
            if not isinstance(supplied, dict):
                raise sim.SimulationRuntimeError("event set field must be a mapping")
            updates.update(supplied)
            first["set"] = updates
            events[0] = first
        return tuple(events)
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


def _write_vcd(path: Path, instance: sim.Simulator, samples: list[dict[str, int]]) -> None:
    widths = instance.trace_widths
    names = sorted({name for sample in samples for name in sample if name != "$event"})
    missing = [name for name in names if name not in widths]
    if missing:
        raise sim.SimulationRuntimeError(
            f"trace signal '{missing[0]}' has no compiler-owned packed width"
        )
    identifiers = {name: _vcd_identifier(index) for index, name in enumerate(names)}
    lines = [
        "$date deterministic $end",
        "$version ZLang HDL simulation $end",
        "$timescale 1ns $end",
        f"$scope module {instance.program.module.name} $end",
    ]
    lines.extend(
        f"$var wire {widths[name]} {identifiers[name]} {name} $end"
        for name in names
    )
    lines.extend(("$upscope $end", "$enddefinitions $end"))
    for sample in samples:
        lines.append(f"#{sample['$event']}")
        for name in names:
            value = sample.get(name, 0)
            lines.append(f"b{value:0{widths[name]}b} {identifiers[name]}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zlang sim",
        description="Run a persistent ZLang simulation engine",
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
        help=(
            "simulation executor (default: native)"
        ),
    )
    execution_options.add_argument(
        "--clock", help="clock to tick when using --cycles"
    )
    execution_options.add_argument(
        "--cycles", type=int, help="run N cycles (single-clock tops only)"
    )
    execution_options.add_argument(
        "--events", type=Path, help="run the JSONL event schedule at PATH"
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
        "--trace", type=Path, help="write simulation transitions as VCD"
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
    try:
        program = sim.compile(
            arguments.source,
            top=arguments.top,
            project=arguments.project,
            profile=arguments.profile,
            engine=arguments.engine,
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
            instance = program.create()
            with instance:
                for name, value in arguments.assignments:
                    instance.set(name, value)
                if arguments.trace is not None:
                    instance.enable_trace()
                if arguments.events is not None:
                    native_outputs: object = instance.run_events(
                        _read_events(arguments.events)
                    )
                elif arguments.cycles is not None:
                    native_outputs = instance.run_cycles(
                        arguments.clock, arguments.cycles
                    )
                else:
                    native_outputs = instance.eval()
                if arguments.compare_with is None:
                    outputs = native_outputs
                if arguments.trace is not None:
                    _write_vcd(
                        arguments.trace, instance, instance.drain_trace()
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
        print(json.dumps(outputs, sort_keys=True, separators=(",", ":")))
    else:
        print(json.dumps(outputs, sort_keys=True, indent=2))
    return 0


__all__ = ["main"]
