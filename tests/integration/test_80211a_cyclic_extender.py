"""IFFT64 bit-reversal reorder and cyclic-prefix validation.

The oracle below is deliberately independent of ZLang IR and both RTL
emitters.  It models one 64-entry bank, accepted-transfer accounting, the
bit-reversed DIF input order, and the exact 16+64 drain sequence.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.ir.expressions import VectorUpdate
from zlang.ir.types import FixedType, StructType, VecType
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate_cycles
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "cyclic_extender.zhl"
)
TOP = "IFFT64ReorderCP"
ZERO = {"re": 0, "im": 0}


def _reverse6(value: int) -> int:
    return int(f"{value:06b}"[::-1], 2)


def _frame(seed: int) -> tuple[dict[str, int], ...]:
    # Keep every raw Q1.15 representation well inside its signed range while
    # making lane and frame identity obvious in a counterexample.
    return tuple(
        {
            "re": -22000 + seed * 3000 + index * 173,
            "im": 21000 - seed * 2000 - index * 149,
        }
        for index in range(64)
    )


def _dif_order(frame: tuple[dict[str, int], ...]) -> tuple[dict[str, int], ...]:
    return tuple(frame[_reverse6(index)] for index in range(64))


def _cycle(
    payload: dict[str, int] = ZERO,
    *,
    valid: int = 0,
    ready: int = 0,
) -> dict[str, object]:
    return {
        "input": {"payload": dict(payload), "valid": valid},
        "output": {"ready": ready},
    }


def _stimulus() -> tuple[
    list[dict[str, object]], list[bool], tuple[dict[str, int], ...], int
]:
    cycles: list[dict[str, object]] = []
    resets: list[bool] = []

    def append(cycle: dict[str, object], reset: bool = False) -> None:
        cycles.append(cycle)
        resets.append(reset)

    append(_cycle(), True)

    # An incomplete symbol must disappear at reset.
    partial = _dif_order(_frame(1))
    for sample in partial[:11]:
        append(_cycle(sample, valid=1))
    append(_cycle(), True)

    # Start a complete symbol, begin draining it with stalls, then reset while
    # it is still resident.  Present ignored input attempts during this drain
    # to prove that the single-bank architecture backpressures the next frame.
    for sample in _dif_order(_frame(2)):
        append(_cycle(sample, valid=1))
    for index in range(25):
        append(_cycle(_frame(7)[0], valid=index % 4 == 0, ready=index % 3 != 1))
    append(_cycle(), True)
    final_reset = len(cycles) - 1

    # A final complete symbol includes input bubbles and downstream stalls.
    final_frame = _frame(3)
    for index, sample in enumerate(_dif_order(final_frame)):
        if index % 9 == 3:
            append(_cycle())
        append(_cycle(sample, valid=1))
    for index in range(110):
        append(_cycle(ready=index % 5 != 1))
    append(_cycle(ready=1))
    return cycles, resets, final_frame, final_reset


def _oracle(
    cycles: list[dict[str, object]], resets: list[bool]
) -> list[dict[str, object]]:
    samples = [dict(ZERO) for _ in range(64)]
    fill_count = 0
    emit_count = 0
    emitting = 0
    result: list[dict[str, object]] = []

    for cycle, reset in zip(cycles, resets, strict=True):
        if reset:
            samples = [dict(ZERO) for _ in range(64)]
            fill_count = 0
            emit_count = 0
            emitting = 0

        input_ready = int(not emitting)
        output_valid = int(emitting)
        emit_index = emit_count + 48 if emit_count < 16 else emit_count - 16
        output_payload = dict(samples[emit_index])
        input_transfer = int(cycle["input"]["valid"] and input_ready)  # type: ignore[index]
        output_transfer = int(output_valid and cycle["output"]["ready"])  # type: ignore[index]
        result.append(
            {
                "input": {"ready": input_ready, "transfer": input_transfer},
                "output": {
                    "payload": output_payload,
                    "valid": output_valid,
                    "transfer": output_transfer,
                },
            }
        )

        if reset:
            continue
        if input_transfer:
            samples[_reverse6(fill_count)] = dict(cycle["input"]["payload"])  # type: ignore[index]
            emitting = int(fill_count == 63)
            fill_count = (fill_count + 1) & 0x3F
        elif output_transfer:
            emitting = int(emit_count != 79)
            emit_count = 0 if emit_count == 79 else emit_count + 1

    return result


def _assert_trace(
    actual: list[dict[str, object]], expected: list[dict[str, object]]
) -> None:
    assert len(actual) == len(expected)
    for index, (observed, wanted) in enumerate(zip(actual, expected, strict=True)):
        assert observed["input"] == wanted["input"], index
        assert observed["output"]["valid"] == wanted["output"]["valid"], index  # type: ignore[index]
        assert observed["output"]["transfer"] == wanted["output"]["transfer"], index  # type: ignore[index]
        if wanted["output"]["valid"]:  # type: ignore[index]
            assert observed["output"]["payload"] == wanted["output"]["payload"], index  # type: ignore[index]


def test_reorder_cp_semantic_canonical_and_single_bank_contract() -> None:
    module = compile_file(SOURCE, top=TOP).ir
    assert [port.name for port in module.inputs] == ["input"]
    assert [port.name for port in module.outputs] == ["output"]
    assert len(module.children) == 1
    kernel = module.children[0]
    assert kernel.name == "IFFT64ReorderCPKernel"
    assert len(kernel.registers) == 4
    samples = kernel.registers[0]
    assert isinstance(samples.type, VecType)
    assert samples.type.length == 64
    assert isinstance(samples.type.element_type, StructType)
    assert all(
        field.type == FixedType(16, 15)
        for field in samples.type.element_type.fields
    )
    update = kernel.rules[0].actions[0].expression
    assert isinstance(update, VectorUpdate)
    assert update.vector_length == 64
    assert (update.index_range.minimum, update.index_range.maximum) == (0, 63)
    assert not kernel.fifos and not kernel.memories and not kernel.roms
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module


def test_reorder_cp_hierarchical_simulator_matches_independent_cycle_oracle() -> None:
    module = compile_file(SOURCE, top=TOP).ir
    cycles, resets, final_frame, final_reset = _stimulus()
    expected = _oracle(cycles, resets)
    actual = simulate_cycles(module, cycles, reset=resets)
    _assert_trace(actual, expected)

    transferred = tuple(
        item["output"]["payload"]
        for item in actual[final_reset + 1 :]
        if item["output"]["transfer"]
    )
    assert transferred == (*final_frame[48:64], *final_frame)
    assert len(transferred) == 80




def _pack(sample: dict[str, int]) -> int:
    return ((sample["re"] & 0xFFFF) << 16) | (sample["im"] & 0xFFFF)


def _rtl_records(
    rtl: tuple[Path, ...],
    tmp_path: Path,
    *,
    cycles: list[dict[str, object]],
    resets: list[bool],
) -> list[tuple[int, int, int, int]]:
    lines = [
        "module tb;",
        "logic clk=0; logic rst; logic signed [15:0] input_payload_re; "
        "logic signed [15:0] input_payload_im; logic input_valid;",
        "wire input_ready; wire signed [15:0] output_payload_re; "
        "wire signed [15:0] output_payload_im; wire output_valid; logic output_ready;",
        f"{TOP} dut(.clk(clk), .rst(rst),",
        "  .input_payload_re(input_payload_re), .input_payload_im(input_payload_im), "
        ".input_valid(input_valid),",
        "  .input_ready(input_ready), .output_payload_re(output_payload_re), "
        ".output_payload_im(output_payload_im),",
        "  .output_valid(output_valid), .output_ready(output_ready));",
        "initial begin",
    ]
    for index, (cycle, reset) in enumerate(zip(cycles, resets, strict=True)):
        packed = _pack(cycle["input"]["payload"])  # type: ignore[index]
        lines.extend(
            (
                f"rst={int(reset)}; input_valid={int(cycle['input']['valid'])}; "  # type: ignore[index]
                f"output_ready={int(cycle['output']['ready'])}; "  # type: ignore[index]
                f"input_payload_re=16'h{(packed >> 16) & 0xffff:04x}; "
                f"input_payload_im=16'h{packed & 0xffff:04x};",
                "#1;",
                f'$display("REC {index} %0d %0d %0d %0d", input_ready, output_valid, '
                "$signed(output_payload_re), $signed(output_payload_im));",
                "#1 clk=1; #1 clk=0;",
            )
        )
    lines.extend(("$finish;", "end", "endmodule"))
    bench = tmp_path / "tb_direct.sv"
    bench.write_text("\n".join(lines))
    object_dir = tmp_path / "obj_direct"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            "--top-module",
            "tb",
            "--Mdir",
            str(object_dir),
            "-o",
            "reorder_cp_sim",
            *(str(path) for path in rtl),
            str(bench),
        ),
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / "reorder_cp_sim"),),
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr or run.stdout
    records: list[tuple[int, int, int, int]] = []
    for line in run.stdout.splitlines():
        if not line.startswith("REC "):
            continue
        _tag, _cycle_number, ready, valid, re_value, im_value = line.split()
        records.append((int(ready), int(valid), int(re_value), int(im_value)))
    return records


def _assert_rtl_records(
    records: list[tuple[int, int, int, int]],
    expected: list[dict[str, object]],
    resets: list[bool],
) -> None:
    assert len(records) == len(expected)
    for index, ((ready, valid, re_value, im_value), wanted) in enumerate(
        zip(records, expected, strict=True)
    ):
        # Reset is synchronous in generated RTL.  The semantic simulator
        # reports reset state during the asserted cycle, whereas RTL reaches
        # that state at the edge ending the cycle.  Compare from the first
        # post-reset cycle; this is the established backend-test convention.
        if resets[index]:
            continue
        assert ready == wanted["input"]["ready"], index  # type: ignore[index]
        assert valid == wanted["output"]["valid"], index  # type: ignore[index]
        if valid:
            assert (re_value, im_value) == (
                wanted["output"]["payload"]["re"],  # type: ignore[index]
                wanted["output"]["payload"]["im"],  # type: ignore[index]
            ), index


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_reorder_cp_direct_sv_strict_lint_and_cycle_oracle(tmp_path: Path) -> None:
    module = compile_file(SOURCE, top=TOP).ir
    artifact = emit_sv_artifact(module)
    rtl = tmp_path / "cyclic_extender.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), TOP)
    cycles, resets, _frame_value, _reset = _stimulus()
    records = _rtl_records((rtl,), tmp_path, cycles=cycles, resets=resets)
    _assert_rtl_records(records, _oracle(cycles, resets), resets)
