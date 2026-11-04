# SPDX-License-Identifier: Apache-2.0
"""Fresh, independently driven native/Direct-SV simulation-I/O comparison."""

import hashlib
import json
from pathlib import Path
from itertools import islice
from dataclasses import dataclass, field

from zlang.backend.companions import publish_companion_bundle
from zlang.backend.systemverilog import emit_artifact
from zlang.ir.module import PortDirection
from zlang.simulation_compare import (
    SimulationComparison,
    SimulationComparisonError,
    _assert_equal,
    _comparison_directory,
    _packed_events,
    _parse_trace,
    _run_bounded,
    _tool_commands,
)
from zlang.simulation_comparison_testbench import STIMULUS_FILE, render_testbench
from zlang.simulation_io_channels import read_packed_records, snapshot_input
from zlang.simulation_io_product import build_io_product
from zlang.simulation_io_rtl import SimulationIOTestbench
from zlang.simulation_io_abi import physical_groups, physical_sample
from zlang.simulation_io_runtime import MAX_IO_EVENTS
from zlang.simulation_plan_policy import MAX_PLAN_BYTES


@dataclass(frozen=True)
class SimulationIOComparison(SimulationComparison):
    final_outputs: dict = field(default_factory=dict)


def _stimulus(product, inputs, directory, simulator):
    count = 0
    packed_bytes = 0
    path = directory / "simulation-input.bin"
    with path.open("x", encoding="ascii") as output:
        for channel, bindings in product.inputs:
            for record in read_packed_records(inputs[channel], bindings):
                if count >= MAX_IO_EVENTS:
                    raise SimulationComparisonError(
                        "simulation input exceeds the expanded-event limit"
                    )
                packed_bytes += sum(value.width for value in record) + 1
                if packed_bytes > MAX_PLAN_BYTES:
                    raise SimulationComparisonError(
                        "packed RTL stimulus exceeds native plan byte limit"
                    )
                if simulator == "verilator" and any(not v.is_binary for v in record):
                    raise SimulationComparisonError(
                        "Verilator I/O comparison requires binary vectors; use Icarus for X projection"
                    )
                output.write(
                    "".join(v.to_bits().replace("u", "x") for v in reversed(record))
                    + "\n"
                )
                count += 1
    return count


def _rtl_records(stdout, product):
    schemas = dict(product.outputs)
    records = {name: [] for name in schemas}
    reads = []
    inputs = []
    for line in stdout.splitlines():
        if line.startswith("io_input "):
            fields = line.split()
            if len(fields) != 4:
                raise SimulationComparisonError("malformed RTL input receipt")
            inputs.append((int(fields[1]), fields[2], fields[3]))
        elif line.startswith("io_read "):
            fields = line.split()
            if len(fields) != 3:
                raise SimulationComparisonError("malformed RTL read receipt")
            reads.append((int(fields[1]), int(fields[2])))
        elif line.startswith("io_record "):
            fields = line.split()
            channel = fields[2]
            if channel not in schemas or len(fields) != 3 + len(schemas[channel]):
                raise SimulationComparisonError("malformed RTL I/O record")
            values = {}
            for (name, expression), bits in zip(
                schemas[channel], fields[3:], strict=True
            ):
                if len(bits) != expression.type.width or any(
                    c not in "01x" for c in bits
                ):
                    raise SimulationComparisonError("invalid RTL I/O value")
                values[name] = {"bits": bits}
            records[channel].append(
                {"event": int(fields[1]), "epoch": 0, "values": values}
            )
    return records, reads, inputs


def compare_io_program(
    program,
    events,
    *,
    simulation,
    io_inputs,
    io_outputs,
    simulator,
    artifact_directory=None,
    timeout=120,
):
    """Never use native-selected reads as the RTL stimulus schedule."""
    events = tuple(islice(events, MAX_IO_EVENTS + 1))
    if len(events) > MAX_IO_EVENTS:
        raise SimulationComparisonError(
            "simulation I/O exceeds the expanded-event limit"
        )
    if "-" in map(str, io_outputs.values()):
        raise SimulationComparisonError("comparison requires file-bound log channels")
    product = build_io_product(
        program.module, program.simulation_blocks, simulation, program.simulation_locals
    )
    with _comparison_directory(artifact_directory) as (directory, retained):
        receipt = {
            "schema": "zlang-simulation-io-comparison-v1",
            "status": "running",
            "dut_plan_identity": program.plan.identity,
            "simulator": simulator,
        }
        try:
            frozen_inputs = {}
            for index, (channel, path) in enumerate(io_inputs.items()):
                text, digest = snapshot_input(path)
                frozen = directory / f"simulation-source-{index}.jsonl"
                frozen.write_bytes(text.encode("utf-8"))
                frozen_inputs[channel] = frozen
                receipt.setdefault("input_sources", {})[channel] = {"sha256": digest}
            count = _stimulus(product, frozen_inputs, directory, simulator)
            artifact = emit_artifact(product.module)
            (directory / "design.sv").write_text(artifact.text)
            harness = SimulationIOTestbench(product, program.module, count)
            packed = _packed_events(program.module, events)
            tb = render_testbench(
                product.module, artifact.module, packed, simulation_io=harness
            )
            (directory / "compare_tb.sv").write_text(tb.text)
            (directory / STIMULUS_FILE).write_text(tb.stimulus)
            publish_companion_bundle(artifact.companions, directory)
            outputs, native_trace, reads, native_inputs = [], [], [], []
            output_groups = physical_groups(program.module, PortDirection.OUTPUT)
            with program.create(
                simulation=simulation, io_inputs=frozen_inputs, io_outputs=io_outputs
            ) as dut:
                for event in events:
                    result = dut.run_events((event,))
                    if dut._io.finished:
                        break
                    outputs.extend(result)
                    native_trace.append(physical_sample(dut, output_groups))
                    index = len(native_trace) - 1
                    if product.block.clock in event.get("edges", ()):
                        for name, location in {
                            v.name: v for _, v in product.locations
                        }.items():
                            native_inputs.append(
                                (
                                    index,
                                    name,
                                    dut._io._get(name, location.container_type.width)
                                    .to_bits()
                                    .replace("u", "x"),
                                )
                            )
                    if dut._io.last_read is not None:
                        reads.append(dut._io.last_read)
                        dut._io.last_read = None
                receipt["native_io"] = dut.simulation_io
                receipt["plan_identity"] = dut.program.plan.identity
                receipt["recipe_identity"] = dut.simulation_io["recipe"]["identity"]
                final_outputs = dut._step_outputs()
            native_records = {
                channel: [
                    json.loads(line) for line in Path(path).read_text().splitlines()
                ]
                for channel, path in io_outputs.items()
            }
            compile_command, run_command = _tool_commands(
                simulator, directory=directory
            )
            for phase, command in [("compile", compile_command), ("run", run_command)]:
                completed = _run_bounded(
                    command, directory=directory, timeout=timeout, phase=phase
                )
                if completed.returncode:
                    raise SimulationComparisonError(
                        f"{simulator} I/O {phase} failed; see retained logs"
                    )
            rtl_records, rtl_reads, rtl_inputs = _rtl_records(completed.stdout, product)
            projected = json.loads(json.dumps(native_records))
            # Project native U tags only in packed bit strings, never in field/channel names.
            for records in projected.values():
                for record in records:
                    for value in record["values"].values():
                        value["bits"] = value["bits"].replace("u", "x")
            if (
                projected != rtl_records
                or reads != rtl_reads
                or native_inputs != rtl_inputs
            ):
                receipt.update(
                    native_records=native_records,
                    rtl_records=rtl_records,
                    native_reads=reads,
                    rtl_reads=rtl_reads,
                    native_inputs=native_inputs,
                    rtl_inputs=rtl_inputs,
                )
                raise SimulationComparisonError(
                    "native/RTL simulation I/O transcript or read advancement differs"
                )
            names = [
                p.external_name
                for p in program.module.top_physical_abi.leaves
                if p.direction is PortDirection.OUTPUT
            ]
            rtl_trace = _parse_trace(
                completed.stdout,
                event_count=len(native_trace),
                output_names=names,
            )
            _assert_equal(
                native_trace,
                rtl_trace,
                simulator=simulator,
                plan_identity=receipt["plan_identity"],
            )
            receipt.update(
                status="passed",
                native_records=native_records,
                rtl_records=rtl_records,
                native_reads=reads,
                rtl_reads=rtl_reads,
                native_inputs=native_inputs,
                rtl_inputs=rtl_inputs,
                artifact_hash=artifact.artifact_hash,
                event_count=len(native_trace),
                files={
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in directory.iterdir()
                    if p.is_file()
                },
            )
            return SimulationIOComparison(
                simulator,
                receipt["plan_identity"],
                artifact.artifact_hash,
                tuple(outputs),
                tuple(native_trace),
                rtl_trace,
                directory if retained else None,
                final_outputs,
            )
        except BaseException as error:
            receipt.update(status="failed", error=str(error))
            raise
        finally:
            (directory / "comparison.json").write_text(
                json.dumps(receipt, indent=2) + "\n"
            )
