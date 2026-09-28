"""Bounded native-versus-generated-RTL simulation comparison.

The native simulator remains the public execution engine.  This module emits
the same compiler-owned direct-SystemVerilog artifact for either supported
external simulator, drives its physical top ABI with a shared testbench, and
compares every physical output after every requested event.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from typing import Iterator, Literal

from zlang.backend.companions import publish_companion_bundle
from zlang.backend.identifiers import rtl_identifier
from zlang.backend.systemverilog import emit_artifact
from zlang.ir.interfaces import InterfaceProtocol
from zlang.ir.module import Module, PortDirection
from zlang.sim import Program, SimulationRuntimeError, Simulator, _pack_value


SIMULATION_COMPARISON_SCHEMA = "zlang-simulation-comparison-v1"
ExternalSimulator = Literal["iverilog", "verilator"]


class SimulationComparisonError(SimulationRuntimeError):
    """Generated RTL could not be proven equal to the native event trace."""


@dataclass(frozen=True)
class SimulationComparison:
    """One successful comparison and the native public results it validated."""

    simulator: ExternalSimulator
    plan_identity: str
    artifact_hash: str
    native_outputs: tuple[dict[str, object], ...]
    native_trace: tuple[dict[str, int], ...]
    rtl_trace: tuple[dict[str, int], ...]
    artifact_directory: Path | None = None


def _public_port_paths(module: Module) -> dict[str, tuple[str, ...]]:
    paths = {port.name: (port.name,) for port in module.ports}
    for aggregate in module.aggregate_protocol_endpoints:
        for member in aggregate.members:
            physical = f"{aggregate.name}__{member.name}"
            if physical not in paths:
                raise SimulationComparisonError(
                    f"aggregate member '{physical}' has no compiler-owned port"
                )
            paths[physical] = (aggregate.name, member.name)
    return paths


def _nested_value(value: object, path: Sequence[str], *, name: str) -> object:
    current = value
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            raise SimulationComparisonError(
                f"simulation input '{name}' has no field '{segment}'"
            )
        current = current[segment]
    return current


def _packed_events(
    module: Module,
    events: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Project public semantic event updates onto exact physical ABI leaves."""

    paths = _public_port_paths(module)
    request_responses = {item.name for item in module.request_responses}
    inputs = tuple(
        leaf
        for leaf in module.top_physical_abi.leaves
        if leaf.direction is PortDirection.INPUT
        and leaf.category not in {"clock", "reset"}
    )
    result: list[dict[str, object]] = []
    for event in events:
        updates: dict[str, int] = {}
        raw_updates = event.get("set", {})
        if not isinstance(raw_updates, Mapping):
            raise SimulationComparisonError("event set field must be a mapping")
        for raw_name, value in raw_updates.items():
            name = str(raw_name)
            if name in paths:
                prefix = paths[name]
            elif name in request_responses:
                prefix = (name,)
            else:
                raise SimulationComparisonError(
                    f"unknown public simulation input '{name}'"
                )
            selected = tuple(
                leaf
                for leaf in inputs
                if leaf.member_path[: len(prefix)] == prefix
            )
            if not selected:
                raise SimulationComparisonError(
                    f"public simulation input '{name}' has no physical input leaves"
                )
            roots: dict[str, list[object]] = {}
            for leaf in selected:
                root = leaf.packed_root_external_name
                if root is None or leaf.packed_root_type is None:
                    raise SimulationComparisonError(
                        f"physical input '{leaf.external_name}' has no packed root"
                    )
                roots.setdefault(root, []).append(leaf)
            for root, leaves in roots.items():
                first = leaves[0]
                relative = first.member_path[len(prefix) :]
                if first.category in {"port", "aggregate"}:
                    relative = () if first.signal_kind == "wire" else (first.signal_kind,)
                elif first.category == "request_response":
                    relative = tuple(first.member_path[1:3])
                packed = _pack_value(
                    first.packed_root_type,
                    _nested_value(value, relative, name=name) if relative else value,
                )
                for leaf in leaves:
                    lsb = leaf.packed_lsb or 0
                    updates[leaf.external_name] = (
                        packed >> lsb
                    ) & ((1 << leaf.width) - 1)
        resets = event.get("reset", {})
        edges = event.get("edges", ())
        if not isinstance(resets, Mapping):
            raise SimulationComparisonError("event reset field must be a mapping")
        if not isinstance(edges, Sequence) or isinstance(edges, (str, bytes)):
            raise SimulationComparisonError("event edges field must be a sequence")
        result.append(
            {
                "set": updates,
                "reset": {str(name): value for name, value in resets.items()},
                "edges": tuple(str(name) for name in edges),
            }
        )
    return tuple(result)


def _physical_output_sample(instance: Simulator) -> dict[str, int]:
    module = instance.program.module
    paths = _public_port_paths(module)
    ports = {port.name: port for port in module.ports}
    request_responses = {item.name: item for item in module.request_responses}
    public_values: dict[str, object] = {}
    for name in ports:
        public_values[name] = instance.get(name)
    for name in request_responses:
        public_values[name] = instance.get(name)

    result: dict[str, int] = {}
    for leaf in module.top_physical_abi.leaves:
        if leaf.direction is not PortDirection.OUTPUT:
            continue
        if leaf.category == "request_response":
            root_name = leaf.member_path[0]
            value = _nested_value(
                public_values[root_name], leaf.member_path[1:3], name=root_name
            )
        else:
            matches = (
                (name, path)
                for name, path in paths.items()
                if leaf.member_path[: len(path)] == path
            )
            try:
                root_name, prefix = max(matches, key=lambda item: len(item[1]))
            except ValueError as error:
                raise SimulationComparisonError(
                    f"physical output '{leaf.external_name}' has no public owner"
                ) from error
            root_value = public_values[root_name]
            port = ports[root_name]
            if port.protocol is InterfaceProtocol.WIRE:
                value = root_value
            else:
                value = _nested_value(
                    root_value, (leaf.signal_kind,), name=root_name
                )
        if leaf.packed_root_type is None:
            raise SimulationComparisonError(
                f"physical output '{leaf.external_name}' has no packed type"
            )
        packed = _pack_value(leaf.packed_root_type, value)
        lsb = leaf.packed_lsb or 0
        result[leaf.external_name] = (
            packed >> lsb
        ) & ((1 << leaf.width) - 1)
    return result


def _native_trace(
    program: Program,
    events: Sequence[Mapping[str, object]],
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, int], ...]]:
    outputs: list[dict[str, object]] = []
    trace: list[dict[str, int]] = []
    with program.create() as instance:
        for event in events:
            outputs.append(instance.run_events((event,))[0])
            trace.append(_physical_output_sample(instance))
    return tuple(outputs), tuple(trace)


def _domain_levels(module: Module) -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
    clocks: dict[str, int] = {}
    resets: dict[str, tuple[int, int]] = {}
    for domain in module.clock_domains:
        clocks[domain.clock] = 1 if domain.edge.value == "falling" else 0
        if domain.reset is not None:
            active_low = domain.reset_polarity.value == "active_low"
            levels = (0, 1) if active_low else (1, 0)
            previous = resets.setdefault(domain.reset, levels)
            if previous != levels:
                raise SimulationComparisonError(
                    f"reset '{domain.reset}' has inconsistent polarity"
                )
    if module.clock is not None:
        clocks.setdefault(module.clock, 0)
    if module.reset is not None:
        resets.setdefault(module.reset, (1, 0))
    return clocks, resets


def _sv_literal(width: int, value: int) -> str:
    return f"{width}'h{value & ((1 << width) - 1):x}"


def _render_testbench(
    module: Module,
    rtl_module: str,
    events: Sequence[Mapping[str, object]],
) -> str:
    leaves = module.top_physical_abi.leaves
    inputs = {leaf.external_name: leaf for leaf in leaves if leaf.direction is PortDirection.INPUT}
    outputs = tuple(leaf for leaf in leaves if leaf.direction is PortDirection.OUTPUT)
    clocks, resets = _domain_levels(module)
    lines = [
        "`timescale 1ns/1ps",
        "module zlang_compare_tb;",
    ]
    for leaf in leaves:
        name = rtl_identifier(leaf.external_name)
        width = leaf.width
        packed = "" if width == 1 else f" [{width - 1}:0]"
        lines.append(f"  logic{packed} {name};")
    lines.append(f"  {rtl_module} dut (")
    for index, leaf in enumerate(leaves):
        separator = "," if index + 1 < len(leaves) else ""
        name = rtl_identifier(leaf.external_name)
        lines.append(f"    .{name}({name}){separator}")
    lines.extend(("  );", "", "  initial begin"))
    for leaf in inputs.values():
        lines.append(
            f"    {rtl_identifier(leaf.external_name)} = {_sv_literal(leaf.width, 0)};"
        )
    for clock, inactive in sorted(clocks.items()):
        lines.append(f"    {rtl_identifier(clock)} = 1'b{inactive};")
    for reset, (_, inactive) in sorted(resets.items()):
        lines.append(f"    {rtl_identifier(reset)} = 1'b{inactive};")
    lines.append("    #1;")
    for event_index, event in enumerate(events):
        for name, value in sorted(event["set"].items()):
            try:
                leaf = inputs[name]
            except KeyError as error:
                raise SimulationComparisonError(
                    f"physical event update '{name}' is not an input"
                ) from error
            lines.append(
                f"    {rtl_identifier(name)} = {_sv_literal(leaf.width, int(value))};"
            )
        for name, asserted in sorted(event["reset"].items()):
            if not isinstance(asserted, bool):
                raise SimulationComparisonError("reset event values must be boolean")
            try:
                active, inactive = resets[name]
            except KeyError as error:
                raise SimulationComparisonError(f"unknown reset '{name}'") from error
            lines.append(
                f"    {rtl_identifier(name)} = 1'b{active if asserted else inactive};"
            )
        lines.append("    #1;")
        selected = tuple(event["edges"])
        if len(selected) != len(set(selected)):
            raise SimulationComparisonError("one event cannot contain a clock twice")
        for clock in selected:
            try:
                inactive = clocks[clock]
            except KeyError as error:
                raise SimulationComparisonError(f"unknown clock '{clock}'") from error
            lines.append(f"    {rtl_identifier(clock)} = 1'b{1 - inactive};")
        if selected:
            lines.append("    #1;")
            for clock in selected:
                lines.append(f"    {rtl_identifier(clock)} = 1'b{clocks[clock]};")
            lines.append("    #1;")
        for leaf in outputs:
            name = rtl_identifier(leaf.external_name)
            lines.extend(
                (
                    f"    if ((^{name}) === 1'bx)",
                    f'      $display("sample {event_index} {leaf.external_name} X");',
                    "    else",
                    f'      $display("sample {event_index} {leaf.external_name} %0h", {name});',
                )
            )
    lines.extend(("    $finish;", "  end", "endmodule", ""))
    return "\n".join(lines)


def _run_bounded(
    command: Sequence[str],
    *,
    directory: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        tuple(command),
        cwd=directory,
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
        raise SimulationComparisonError(
            f"external simulation command exceeded {timeout:g} seconds: "
            + " ".join(command)
        ) from error
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _tool_commands(
    simulator: ExternalSimulator,
    *,
    directory: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    build = directory / "build"
    build.mkdir(parents=True, exist_ok=True)
    if simulator == "iverilog":
        compiler = shutil.which("iverilog")
        runtime = shutil.which("vvp")
        if compiler is None or runtime is None:
            raise SimulationComparisonError(
                "Icarus comparison requires both 'iverilog' and 'vvp' on PATH"
            )
        return (
            (
                compiler,
                "-g2012",
                "-s",
                "zlang_compare_tb",
                "-o",
                "build/compare_sim.vvp",
                "design.sv",
                "compare_tb.sv",
            ),
            (runtime, "build/compare_sim.vvp"),
        )
    if simulator == "verilator":
        compiler = shutil.which("verilator")
        if compiler is None:
            raise SimulationComparisonError(
                "Verilator comparison requires 'verilator' on PATH"
            )
        return (
            (
                compiler,
                "--binary",
                "--timing",
                "--top-module",
                "zlang_compare_tb",
                "--Mdir",
                "build/obj",
                "-o",
                "compare_sim",
                "-Wno-DECLFILENAME",
                "-Wno-TIMESCALEMOD",
                "-Wno-UNUSED",
                "-Wno-UNDRIVEN",
                "design.sv",
                "compare_tb.sv",
            ),
            (str(directory / "build" / "obj" / "compare_sim"),),
        )
    raise SimulationComparisonError(
        f"unsupported RTL simulator '{simulator}'; expected iverilog or verilator"
    )


def _parse_trace(
    stdout: str,
    *,
    event_count: int,
    output_names: Sequence[str],
) -> tuple[dict[str, int], ...]:
    trace = [dict() for _ in range(event_count)]
    expected_names = set(output_names)
    for line in stdout.splitlines():
        if not line.startswith("sample "):
            continue
        fields = line.split()
        if len(fields) != 4:
            raise SimulationComparisonError(f"malformed RTL sample line: {line!r}")
        _, raw_index, name, value = fields
        try:
            index = int(raw_index)
        except ValueError as error:
            raise SimulationComparisonError(f"malformed RTL sample line: {line!r}") from error
        if index < 0 or index >= event_count or name not in expected_names:
            raise SimulationComparisonError(f"unexpected RTL sample line: {line!r}")
        if name in trace[index]:
            raise SimulationComparisonError(
                f"duplicate RTL sample for event {index}, output '{name}'"
            )
        if any(character in value.lower() for character in ("x", "z")):
            raise SimulationComparisonError(
                f"RTL output '{name}' is unknown/high-impedance at event {index}"
            )
        try:
            trace[index][name] = int(value, 16)
        except ValueError as error:
            raise SimulationComparisonError(f"malformed RTL sample line: {line!r}") from error
    for index, sample in enumerate(trace):
        missing = expected_names - set(sample)
        if missing:
            raise SimulationComparisonError(
                f"RTL trace is missing output '{sorted(missing)[0]}' at event {index}"
            )
    return tuple(trace)


def _assert_equal(
    native: Sequence[dict[str, int]],
    rtl: Sequence[dict[str, int]],
    *,
    simulator: str,
    plan_identity: str,
) -> None:
    if len(native) != len(rtl):
        raise SimulationComparisonError(
            f"simulation trace length mismatch for plan {plan_identity}: "
            f"native={len(native)}, {simulator}={len(rtl)}"
        )
    for index, (left, right) in enumerate(zip(native, rtl, strict=True)):
        if left != right:
            names = sorted(set(left) | set(right))
            name = next(item for item in names if left.get(item) != right.get(item))
            raise SimulationComparisonError(
                f"simulation mismatch for plan {plan_identity} at event {index}, "
                f"output '{name}': native={left.get(name)!r}, "
                f"{simulator}={right.get(name)!r}"
            )


@contextmanager
def _comparison_directory(path: Path | None) -> Iterator[tuple[Path, bool]]:
    if path is None:
        with tempfile.TemporaryDirectory(prefix="zlang-sim-compare-") as raw:
            yield Path(raw), False
        return
    destination = Path(path)
    if destination.is_symlink():
        raise SimulationComparisonError(
            f"comparison artifact directory '{destination}' must not be a symlink"
        )
    if destination.exists() and any(destination.iterdir()):
        raise SimulationComparisonError(
            f"comparison artifact directory '{destination}' must be empty"
        )
    destination.mkdir(parents=True, exist_ok=True)
    yield destination, True


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compare_program(
    program: Program,
    events: Sequence[Mapping[str, object]],
    *,
    simulator: ExternalSimulator,
    artifact_directory: Path | None = None,
    timeout: float = 120,
) -> SimulationComparison:
    """Compare one native event trace with Icarus or Verilator Direct-SV."""

    if isinstance(timeout, bool) or timeout <= 0:
        raise SimulationComparisonError("comparison timeout must be positive")
    normalized_events = tuple(events)
    native_outputs, native_trace = _native_trace(program, normalized_events)
    packed_events = _packed_events(program.module, normalized_events)
    artifact = emit_artifact(
        program.module,
        selected_ir_identity=str(program.plan.payload["canonical_ir_identity"]),
    )
    with _comparison_directory(artifact_directory) as (directory, retained):
        design_path = directory / "design.sv"
        testbench_path = directory / "compare_tb.sv"
        design_path.write_text(artifact.text, encoding="utf-8")
        testbench_path.write_text(
            _render_testbench(program.module, artifact.module, packed_events),
            encoding="utf-8",
        )
        publish_companion_bundle(artifact.companions, directory)
        compile_command, run_command = _tool_commands(simulator, directory=directory)
        compiled = _run_bounded(compile_command, directory=directory, timeout=timeout)
        (directory / "compile.log").write_text(
            "stdout:\n" + compiled.stdout + "\nstderr:\n" + compiled.stderr,
            encoding="utf-8",
        )
        if compiled.returncode != 0:
            shutil.rmtree(directory / "build", ignore_errors=True)
            raise SimulationComparisonError(
                f"{simulator} failed to compile generated RTL: "
                + (compiled.stderr.strip() or compiled.stdout.strip())
            )
        executed = _run_bounded(run_command, directory=directory, timeout=timeout)
        (directory / "run.log").write_text(
            "stdout:\n" + executed.stdout + "\nstderr:\n" + executed.stderr,
            encoding="utf-8",
        )
        if executed.returncode != 0:
            shutil.rmtree(directory / "build", ignore_errors=True)
            raise SimulationComparisonError(
                f"{simulator} failed while executing generated RTL: "
                + (executed.stderr.strip() or executed.stdout.strip())
            )
        output_names = tuple(
            leaf.external_name
            for leaf in program.module.top_physical_abi.leaves
            if leaf.direction is PortDirection.OUTPUT
        )
        rtl_trace = _parse_trace(
            executed.stdout,
            event_count=len(normalized_events),
            output_names=output_names,
        )
        _assert_equal(
            native_trace,
            rtl_trace,
            simulator=simulator,
            plan_identity=program.plan.identity,
        )
        shutil.rmtree(directory / "build", ignore_errors=True)
        manifest = {
            "schema": SIMULATION_COMPARISON_SCHEMA,
            "simulator": simulator,
            "plan_identity": program.plan.identity,
            "artifact_hash": artifact.artifact_hash,
            "event_count": len(normalized_events),
            "native_trace": native_trace,
            "rtl_trace": rtl_trace,
            "files": {
                "design.sv": _sha256(design_path),
                "compare_tb.sv": _sha256(testbench_path),
                **{
                    companion.logical_path: companion.file_hash
                    for companion in artifact.companions
                },
            },
        }
        (directory / "comparison.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return SimulationComparison(
            simulator=simulator,
            plan_identity=program.plan.identity,
            artifact_hash=artifact.artifact_hash,
            native_outputs=native_outputs,
            native_trace=native_trace,
            rtl_trace=rtl_trace,
            artifact_directory=directory if retained else None,
        )


__all__ = [
    "ExternalSimulator",
    "SIMULATION_COMPARISON_SCHEMA",
    "SimulationComparison",
    "SimulationComparisonError",
    "compare_program",
]
