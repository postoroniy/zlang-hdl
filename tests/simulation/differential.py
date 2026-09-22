"""Reusable event-level differential harness for all simulation backends."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import signal
import subprocess
from typing import Mapping, Sequence

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import create_file_compilation_session
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import PortDirection
from zlang.sim import Program, _pack_value
from zlang.simulation_reference import compile_reference_plan


@dataclass(frozen=True)
class DifferentialTrace:
    plan_identity: str
    reference: tuple[dict[str, int], ...]
    native: tuple[dict[str, int], ...]
    direct_sv: tuple[dict[str, int], ...]


def _public_port_paths(module) -> dict[str, tuple[str, ...]]:
    """Map semantic protocol members to their exact public ABI path."""

    paths = {port.name: (port.name,) for port in module.ports}
    for aggregate in module.aggregate_protocol_endpoints:
        for member in aggregate.members:
            physical = f"{aggregate.name}__{member.name}"
            if physical not in paths:
                raise AssertionError(
                    f"aggregate member '{physical}' has no physical port"
                )
            paths[physical] = (aggregate.name, member.name)
    return paths


def _packed_events(module, events: Sequence[Mapping[str, object]]):
    inputs = {port.name: port for port in module.inputs}
    all_ports = {port.name: port for port in module.ports}
    paths = _public_port_paths(module)
    physical_keys = {
        (port.name, leaf.signal_kind)
        for port in module.ports
        for leaf in module.top_physical_abi.leaves
        if leaf.direction is PortDirection.INPUT
        and leaf.category in {"port", "aggregate"}
        and leaf.member_path[:len(paths[port.name])] == paths[port.name]
    }
    physical_inputs = {
        (name, signal): tuple(
            candidate
            for candidate in module.top_physical_abi.leaves
            if candidate.direction is PortDirection.INPUT
            and candidate.category in {"port", "aggregate"}
            and candidate.member_path[:len(paths[name])] == paths[name]
            and candidate.signal_kind == signal
        )
        for name, signal in physical_keys
    }
    result = []
    for event in events:
        updates = {}
        for name, value in event.get("set", {}).items():
            port = all_ports.get(name)
            if port is None:
                raise AssertionError(f"unknown differential input '{name}'")
            if port.protocol is not InterfaceProtocol.WIRE:
                if not isinstance(value, Mapping):
                    raise AssertionError(
                        f"protocol differential input '{name}' is not a mapping"
                    )
                for field, field_value in value.items():
                    leaves = physical_inputs[(name, field)]
                    root_type = port.type if field == "payload" else leaves[0].canonical_type
                    packed = _pack_value(root_type, field_value)
                    for leaf in leaves:
                        updates[leaf.external_name] = (
                            packed >> leaf.packed_lsb
                        ) & ((1 << leaf.width) - 1)
            else:
                if name not in inputs:
                    raise AssertionError(f"unknown differential input '{name}'")
                updates[name] = _pack_value(inputs[name].type, value)
        result.append(
            {
                "set": updates,
                "reset": dict(event.get("reset", {})),
                "edges": tuple(event.get("edges", ())),
            }
        )
    return tuple(result)


def _runtime_trace(
    program: Program,
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, int], ...]:
    result = []
    paths = _public_port_paths(program.module)
    ports_by_path = {
        path: next(port for port in program.module.ports if port.name == name)
        for name, path in paths.items()
    }
    with program.create() as instance:
        for event in events:
            instance.run_events((event,))
            sample: dict[str, int] = {}
            endpoint_values = {
                port.name: instance.get(port.name)
                for port in program.module.ports
                if port.protocol is not InterfaceProtocol.WIRE
            }
            for leaf in program.module.top_physical_abi.leaves:
                if leaf.direction is not PortDirection.OUTPUT:
                    continue
                matches = (
                    (path, port) for path, port in ports_by_path.items()
                    if leaf.member_path[:len(path)] == path
                )
                port = max(matches, key=lambda match: len(match[0]))[1]
                if port.protocol is not InterfaceProtocol.WIRE:
                    value = endpoint_values[port.name][leaf.signal_kind]
                    root_type = (
                        port.type
                        if leaf.signal_kind == "payload"
                        else leaf.canonical_type
                    )
                    packed = _pack_value(root_type, value)
                    sample[leaf.external_name] = (
                        packed >> leaf.packed_lsb
                    ) & ((1 << leaf.width) - 1)
                elif len(leaf.member_path) == 1:
                    sample[leaf.external_name] = instance.get_packed(
                        leaf.member_path[0]
                    )
            result.append(sample)
    return tuple(result)


def _assignment(name: str, value: int, width: int, indent: str = "  ") -> list[str]:
    if width <= 64:
        return [f"{indent}design.{name} = 0x{value:x}ULL;"]
    return [
        f"{indent}design.{name}[{index}] = 0x{(value >> (32 * index)) & 0xFFFFFFFF:x}U;"
        for index in range((width + 31) // 32)
    ]


def _output_lines(name: str, width: int, event: int) -> list[str]:
    prefix = f'std::cout << "sample {event} {name} ";'
    if width <= 64:
        return [
            f"  {prefix}",
            f"  std::cout << std::hex << static_cast<unsigned long long>(design.{name})",
            '            << std::dec << "\\n";',
        ]
    words = (width + 31) // 32
    lines = [f"  {prefix}", "  std::cout << std::hex << std::setfill('0');"]
    for index in reversed(range(words)):
        lines.append(
            f"  std::cout << std::setw(8) << static_cast<unsigned>(design.{name}[{index}]);"
        )
    lines.append('  std::cout << std::dec << "\\n";')
    return lines


def _verilator_harness(module, events: Sequence[Mapping[str, object]]) -> str:
    domains = {domain.clock: domain for domain in module.clock_domains}
    if module.clock and not domains:
        domains[module.clock] = None
    reset_levels = {}
    for domain in module.clock_domains:
        if domain.reset is None:
            continue
        active_low = domain.reset_polarity.value == "active_low"
        levels = (0, 1) if active_low else (1, 0)
        previous = reset_levels.setdefault(domain.reset, levels)
        if previous != levels:
            raise AssertionError(f"reset '{domain.reset}' has inconsistent polarity")
    if module.reset and module.reset not in reset_levels:
        reset_levels[module.reset] = (1, 0)
    inputs = {
        leaf.external_name: leaf
        for leaf in module.top_physical_abi.leaves
        if leaf.direction is PortDirection.INPUT
    }
    outputs = tuple(
        leaf
        for leaf in module.top_physical_abi.leaves
        if leaf.direction is PortDirection.OUTPUT
    )
    lines = [
        f'#include "V{module.name}.h"',
        "#include <iomanip>",
        "#include <iostream>",
        "#include <verilated.h>",
        "int main(int argc, char** argv) {",
        "  Verilated::commandArgs(argc, argv);",
        "  Verilated::randReset(0);",
        f"  V{module.name} design;",
    ]
    for clock, domain in sorted(domains.items()):
        inactive = 1 if domain is not None and domain.edge.value == "falling" else 0
        lines.append(f"  design.{clock} = {inactive};")
    for reset, (_, inactive) in sorted(reset_levels.items()):
        lines.append(f"  design.{reset} = {inactive};")
    lines.append("  design.eval();")
    for event_index, event in enumerate(events):
        for name, value in sorted(event["set"].items()):
            lines.extend(_assignment(name, int(value), inputs[name].width))
        for name, asserted in sorted(event["reset"].items()):
            active, inactive = reset_levels[name]
            lines.append(f"  design.{name} = {active if asserted else inactive};")
        lines.append("  design.eval();")
        selected = tuple(event["edges"])
        for clock in selected:
            domain = domains[clock]
            active = 0 if domain is not None and domain.edge.value == "falling" else 1
            lines.append(f"  design.{clock} = {active};")
        if selected:
            lines.append("  design.eval();")
            for clock in selected:
                domain = domains[clock]
                inactive = (
                    1 if domain is not None and domain.edge.value == "falling" else 0
                )
                lines.append(f"  design.{clock} = {inactive};")
            lines.append("  design.eval();")
        for leaf in outputs:
            lines.extend(_output_lines(leaf.external_name, leaf.width, event_index))
    lines.extend(("  return 0;", "}"))
    return "\n".join(lines) + "\n"


def _direct_sv_trace(
    module,
    events: Sequence[Mapping[str, object]],
    directory: Path,
    *,
    timeout: int,
) -> tuple[dict[str, int], ...]:
    verilator = shutil.which("verilator")
    if verilator is None:
        raise AssertionError("Verilator is required for differential execution")
    directory.mkdir(parents=True, exist_ok=True)
    rtl = directory / f"{module.name}.sv"
    harness = directory / "differential.cpp"
    object_dir = directory / "obj"
    rtl.write_text(emit_experimental(module), encoding="utf-8")
    harness.write_text(_verilator_harness(module, events), encoding="utf-8")
    process = subprocess.Popen(
        (
            verilator,
            "--cc",
            "--exe",
            "--build",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            module.name,
            "--Mdir",
            str(object_dir),
            "-o",
            "differential",
            str(rtl),
            str(harness),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "CCACHE_DISABLE": "1"},
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise AssertionError(
            f"Verilator differential build exceeded {timeout} seconds"
        ) from error
    if process.returncode != 0:
        raise AssertionError(stderr or stdout)
    run = subprocess.run(
        (str(object_dir / "differential"),),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if run.returncode != 0:
        raise AssertionError(run.stderr or run.stdout)
    trace = [dict() for _ in events]
    for line in run.stdout.splitlines():
        kind, raw_event, name, value = line.split()
        if kind != "sample":
            continue
        trace[int(raw_event)][name] = int(value, 16)
    return tuple(trace)


def _assert_same(
    expected: Sequence[dict[str, int]],
    actual: Sequence[dict[str, int]],
    *,
    expected_name: str,
    actual_name: str,
    events: Sequence[Mapping[str, object]],
    plan_identity: str,
) -> None:
    if expected == actual:
        return
    for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
        if left != right:
            raise AssertionError(
                f"simulation mismatch for plan {plan_identity} at event {index}: "
                f"stimulus={events[index]!r}; {expected_name}={left!r}; "
                f"{actual_name}={right!r}"
            )
    raise AssertionError(
        f"simulation trace lengths differ for plan {plan_identity}: "
        f"{expected_name}={len(expected)}, {actual_name}={len(actual)}"
    )


def run_differential(
    source: Path,
    *,
    top: str,
    events: Sequence[Mapping[str, object]],
    directory: Path,
    timeout: int = 120,
) -> DifferentialTrace:
    """Execute exact events through reference, native and Direct-SV paths."""

    session = create_file_compilation_session(source, top=top)
    module = session.planning.module
    plan = session.simulation_plan
    packed = _packed_events(module, events)
    reference = Program(plan, module, compile_reference_plan(plan))
    import _zlang_native_sim

    native = Program(
        plan, module, _zlang_native_sim.compile_plan_bytes(plan.to_bytes())
    )
    reference_trace = _runtime_trace(reference, events)
    native_trace = _runtime_trace(native, events)
    direct_sv_trace = _direct_sv_trace(module, packed, directory, timeout=timeout)
    _assert_same(
        reference_trace,
        native_trace,
        expected_name="reference",
        actual_name="native",
        events=events,
        plan_identity=plan.identity,
    )
    _assert_same(
        reference_trace,
        direct_sv_trace,
        expected_name="reference",
        actual_name="direct_sv",
        events=events,
        plan_identity=plan.identity,
    )
    return DifferentialTrace(
        plan.identity,
        reference_trace,
        native_trace,
        direct_sv_trace,
    )


__all__ = ["DifferentialTrace", "run_differential"]
