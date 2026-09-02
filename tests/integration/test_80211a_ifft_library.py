"""Exact two-bank inverse DIF-SDF stage validation for the IEEE IFFT64.

The cycle oracle is independent of the ZLang evaluator.  It operates directly
on signed fixed-point raw integers, freezes the inverse Q2.22 twiddles with a
high-precision Decimal implementation, and models the accepted-transfer state
machine and two typed FIFO banks explicitly.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from functools import lru_cache
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.companions import publish_companion_bundle
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.ir import expressions as expr
from zlang.ir.types import FixedType, StructType
from zlang.opt import OptimizationStage, lower, restore
from zlang.parser import parse
from zlang.semantic import analyze
from zlang.simulate import simulate_cycles
from zlang.toolchain import (
    find_clash_executable,
    generate_verilog,
    lint_with_verilator,
)


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples/projects/80211a_transmitter/src/ifft_library.zhl"
)
PI = Decimal(
    "3.14159265358979323846264338327950288419716939937510"
    "58209749445923078164062862089986280348253421170679"
)


def _decimal_sin(value: Decimal) -> Decimal:
    term = value
    total = value
    index = 1
    while True:
        term *= -(value * value) / Decimal((2 * index) * (2 * index + 1))
        updated = total + term
        if updated == total:
            return total
        total = updated
        index += 1


def _decimal_cos(value: Decimal) -> Decimal:
    term = Decimal(1)
    total = term
    index = 1
    while True:
        term *= -(value * value) / Decimal((2 * index - 1) * (2 * index))
        updated = total + term
        if updated == total:
            return total
        total = updated
        index += 1


@lru_cache(maxsize=None)
def _twiddles(depth: int) -> tuple[tuple[int, int], ...]:
    """Unit inverse exp(+j*2*pi*k/(2D)) in signed Q2.22."""

    result: list[tuple[int, int]] = []
    with localcontext() as context:
        context.prec = 100
        scale = Decimal(1 << 22)
        size = depth * 2
        for phase in range(depth):
            angle = Decimal(2) * PI * Decimal(phase) / Decimal(size)
            real = (_decimal_cos(angle) * scale).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
            imag = (_decimal_sin(angle) * scale).to_integral_value(
                rounding=ROUND_HALF_EVEN
            )
            result.append((int(real), int(imag)))
    return tuple(result)


def _cycles(depth: int) -> tuple[list[dict[str, object]], list[bool]]:
    count = max(64, depth * 10)
    reset_at = count // 2
    cycles: list[dict[str, object]] = []
    resets: list[bool] = []
    for index in range(count):
        resets.append(index in (0, reset_at))
        cycles.append(
            {
                "input": {
                    "payload": {
                        "re": (index * 911 + depth * 37) % 30001 - 15000,
                        "im": (index * 557 + depth * 19) % 24001 - 12000,
                    },
                    "valid": int(index % 17 not in (3, 4, 12)),
                },
                "output": {"ready": int(index % 19 not in (7, 8, 9, 15))},
            }
        )
    return cycles, resets


def _oracle(
    cycles: list[dict[str, object]], resets: list[bool], depth: int
) -> list[dict[str, object]]:
    input_delay: list[tuple[int, int]] = []
    feedback: list[tuple[int, int]] = []
    phase = 0
    output = (0, 0)
    output_valid = 0
    observed: list[dict[str, object]] = []

    for cycle, reset in zip(cycles, resets, strict=True):
        if reset:
            input_delay = []
            feedback = []
            phase = 0
            output = (0, 0)
            output_valid = 0

        source = cycle["input"]
        sink = cycle["output"]
        input_ready = int((not output_valid) or bool(sink["ready"]))
        input_transfer = bool(source["valid"]) and bool(input_ready)
        output_transfer = bool(output_valid) and bool(sink["ready"])
        observed.append(
            {
                "input": {
                    "ready": input_ready,
                    "transfer": int(input_transfer),
                },
                "output": {
                    "payload": {"re": output[0], "im": output[1]},
                    "valid": output_valid,
                    "transfer": int(output_transfer),
                },
            }
        )
        if reset:
            continue

        action: str | None = None
        if input_transfer and phase < depth and not feedback:
            action = "fill"
        elif input_transfer and phase < depth and feedback:
            action = "low"
        elif input_transfer and phase >= depth:
            action = "high"

        sample = (int(source["payload"]["re"]), int(source["payload"]["im"]))
        if action == "fill":
            input_delay.append(sample)
            phase = (phase + 1) % (2 * depth)
        elif action == "low":
            output = feedback.pop(0)
            input_delay.append(sample)
            output_valid = 1
            phase = (phase + 1) % (2 * depth)
        elif action == "high":
            delayed = input_delay.pop(0)
            difference = (delayed[0] - sample[0], delayed[1] - sample[1])
            twiddle = _twiddles(depth)[phase - depth]
            feedback.append(
                (
                    difference[0] * twiddle[0]
                    - difference[1] * twiddle[1],
                    difference[0] * twiddle[1]
                    + difference[1] * twiddle[0],
                )
            )
            # Lossless Q15 -> Q37 alignment, exactly as the source raw mapping.
            output = (
                (delayed[0] + sample[0]) << 22,
                (delayed[1] + sample[1]) << 22,
            )
            output_valid = 1
            phase = (phase + 1) % (2 * depth)
        elif output_transfer:
            output_valid = 0

    return observed


def _module(depth: int):
    return compile_file(
        SOURCE,
        top=f"IFFT64DIFStageExactD{depth}",
        include_clash=False,
    ).ir


def _chain_module():
    return compile_file(
        SOURCE,
        top="IFFT64DIFExactChain",
        include_clash=False,
    ).ir


def _exact_stage_frame(
    frame: list[tuple[int, int]], depth: int
) -> list[tuple[int, int]]:
    twiddles = _twiddles(depth)
    sums = [
        (
            (frame[index][0] + frame[index + depth][0]) << 22,
            (frame[index][1] + frame[index + depth][1]) << 22,
        )
        for index in range(depth)
    ]
    differences = []
    for index in range(depth):
        real = frame[index][0] - frame[index + depth][0]
        imag = frame[index][1] - frame[index + depth][1]
        twiddle_real, twiddle_imag = twiddles[index]
        differences.append(
            (
                real * twiddle_real - imag * twiddle_imag,
                real * twiddle_imag + imag * twiddle_real,
            )
        )
    return sums + differences


def _nearest_even_shift(value: int, shift: int) -> int:
    sign = -1 if value < 0 else 1
    quotient, remainder = divmod(abs(value), 1 << shift)
    halfway = 1 << (shift - 1)
    if remainder > halfway or (remainder == halfway and quotient & 1):
        quotient += 1
    return sign * quotient


def _exact_ifft64_frame(
    frame: list[tuple[int, int]],
) -> list[tuple[int, int]]:
    assert len(frame) == 64
    current = list(frame)
    for depth in (32, 16, 8, 4, 2, 1):
        current = [
            value
            for offset in range(0, 64, 2 * depth)
            for value in _exact_stage_frame(
                current[offset : offset + 2 * depth], depth
            )
        ]
    # Stage six is Q25.147. Multiplication by raw one in Q1.6 is exactly
    # 1/64, so conversion to Q1.15 discards 147+6-15 = 138 bits once.
    return [
        (
            max(-32768, min(32767, _nearest_even_shift(real, 138))),
            max(-32768, min(32767, _nearest_even_shift(imag, 138))),
        )
        for real, imag in current
    ]


def _semantic_stage(depth: int):
    parsed = parse(SOURCE.read_text())
    syntax = next(
        item
        for item in (*parsed.submodules, parsed)
        if item.name == "IFFT64DIFStageExactDualBank"
    )
    syntax = replace(
        syntax,
        imports=parsed.imports,
        type_aliases=parsed.type_aliases,
        structs=parsed.structs,
        functions=parsed.functions,
        operators=parsed.operators,
        protocols=parsed.protocols,
        submodules=(),
        parameters=tuple(
            replace(
                parameter,
                default={
                    "D": depth,
                    "CW": (2 * depth - 1).bit_length(),
                    "IW": max(1, depth.bit_length() - 1),
                }.get(parameter.name, parameter.default),
            )
            for parameter in syntax.parameters
        ),
    )
    return analyze(
        syntax,
        specialization_type_bindings={
            "In": FixedType(16, 15),
            "Out": FixedType(42, 37),
        },
    )


def _fixed_converts(value: object) -> list[expr.FixedConvert]:
    found: list[expr.FixedConvert] = []
    seen: set[int] = set()

    def visit(item: object) -> None:
        if isinstance(item, expr.FixedConvert):
            found.append(item)
        if is_dataclass(item):
            identity = id(item)
            if identity in seen:
                return
            seen.add(identity)
            for field in fields(item):
                if field.name != "origin":
                    visit(getattr(item, field.name))
        elif isinstance(item, tuple):
            for child in item:
                visit(child)

    visit(value)
    return found


def _pack_complex(real: int, imag: int, width: int) -> int:
    mask = (1 << width) - 1
    return ((real & mask) << width) | (imag & mask)


def _rtl_records(
    rtl: Path | tuple[Path, ...],
    *,
    direct: bool,
    depth: int,
    cycles: list[dict[str, object]],
    resets: list[bool],
) -> list[tuple[int, int, int, int, int]]:
    rtl_sources = (rtl,) if isinstance(rtl, Path) else rtl
    top = f"IFFT64DIFStageExactD{depth}"
    connection = (
        ".clk(clk), .rst(rst), .input_payload_re(in_re), "
        ".input_payload_im(in_im), .input_valid(in_valid), "
        ".input_ready(in_ready), .output_payload_re(out_re), "
        ".output_payload_im(out_im), .output_valid(out_valid), "
        ".output_ready(out_ready)"
    )
    lines = [
        "module tb;",
        "logic clk=0; always #5 clk=~clk;",
        "logic rst; logic signed [15:0] in_re, in_im; "
        "logic in_valid; wire in_ready;",
        "wire signed [41:0] out_re, out_im; wire out_valid; logic out_ready;",
        f"{top} dut({connection});",
        "initial begin",
    ]
    for index, (cycle, reset) in enumerate(zip(cycles, resets, strict=True)):
        payload = cycle["input"]["payload"]
        packed = _pack_complex(payload["re"], payload["im"], 16)
        lines.extend(
            (
                f"rst={int(reset)}; in_valid={cycle['input']['valid']}; "
                f"out_ready={cycle['output']['ready']}; "
                f"in_re=16'h{(packed >> 16) & 0xffff:04x}; "
                f"in_im=16'h{packed & 0xffff:04x};",
                f"#1; $display(\"REC %0d %0d %0d %0d %0d\", {index}, "
                "in_ready, out_valid, $signed(out_re), $signed(out_im));",
                "@(posedge clk); #1;",
            )
        )
    lines.extend(("$finish;", "end", "endmodule"))

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bench = root / "tb.sv"
        bench.write_text("\n".join(lines))
        output = root / "obj"
        environment = os.environ.copy()
        environment["CCACHE_DISABLE"] = "1"
        build = subprocess.run(
            (
                "verilator",
                "--binary",
                "--timing",
                "-Wno-fatal",
                "--Mdir",
                str(output),
                "--top-module",
                "tb",
                *(str(path) for path in rtl_sources),
                str(bench),
            ),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stderr or build.stdout
        run = subprocess.run(
            (str(output / "Vtb"),),
            cwd=rtl_sources[0].parent,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr or run.stdout
    pattern = re.compile(r"REC (\d+) (\d+) (\d+) (-?\d+) (-?\d+)")
    return [
        tuple(map(int, match.groups()))
        for match in map(pattern.search, run.stdout.splitlines())
        if match
    ]


def _chain_rtl_records(
    rtl: Path | tuple[Path, ...],
    *,
    direct: bool,
    cycles: list[dict[str, object]],
    resets: list[bool],
) -> list[tuple[int, int, int, int, int]]:
    rtl_sources = (rtl,) if isinstance(rtl, Path) else rtl
    lines = [
        "module tb;",
        "logic clk=0; always #5 clk=~clk;",
        "logic rst; logic signed [15:0] in_re, in_im; "
        "logic in_valid; wire in_ready;",
        "wire signed [15:0] out_re, out_im; wire out_valid; logic out_ready;",
        "IFFT64DIFExactChain dut(" +
        ".clk(clk), .rst(rst), .input_payload_re(in_re), " +
        ".input_payload_im(in_im), .input_valid(in_valid), " +
        ".input_ready(in_ready), .output_payload_re(out_re), " +
        ".output_payload_im(out_im), .output_valid(out_valid), " +
        ".output_ready(out_ready));",
        "initial begin",
    ]
    for index, (cycle, reset) in enumerate(zip(cycles, resets, strict=True)):
        payload = cycle["input"]["payload"]
        packed = _pack_complex(payload["re"], payload["im"], 16)
        lines.extend(
            (
                f"rst={int(reset)}; in_valid={cycle['input']['valid']}; "
                f"out_ready={cycle['output']['ready']}; "
                f"in_re=16'h{(packed >> 16) & 0xffff:04x}; "
                f"in_im=16'h{packed & 0xffff:04x};",
                f"#1; $display(\"REC %0d %0d %0d %0d %0d\", {index}, "
                "in_ready, out_valid, $signed(out_re), $signed(out_im));",
                "@(posedge clk); #1;",
            )
        )
    lines.extend(("$finish;", "end", "endmodule"))

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        bench = root / "tb.sv"
        bench.write_text("\n".join(lines))
        output = root / "obj"
        environment = os.environ.copy()
        environment["CCACHE_DISABLE"] = "1"
        build = subprocess.run(
            (
                "verilator",
                "--binary",
                "--timing",
                "-Wno-fatal",
                "--Mdir",
                str(output),
                "--top-module",
                "tb",
                *(str(path) for path in rtl_sources),
                str(bench),
            ),
            cwd=rtl_sources[0].parent,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert build.returncode == 0, build.stderr or build.stdout
        run = subprocess.run(
            (str(output / "Vtb"),),
            cwd=rtl_sources[0].parent,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr or run.stdout
    pattern = re.compile(r"REC (\d+) (\d+) (\d+) (-?\d+) (-?\d+)")
    return [
        tuple(map(int, match.groups()))
        for match in map(pattern.search, run.stdout.splitlines())
        if match
    ]


@pytest.mark.parametrize("depth", (4, 8))
def test_dual_bank_types_twiddles_and_canonical_round_trip(depth: int) -> None:
    top = _module(depth)
    stage = top.children[0]
    assert [(item.name, item.depth) for item in stage.fifos] == [
        ("input_delay", depth),
        ("feedback", depth),
    ]
    assert str(stage.fifos[0].element_type) == "Complex<fixed<16,15>>"
    assert str(stage.fifos[1].element_type) == "Complex<fixed<42,37>>"
    assert tuple(
        (word.fields[0][1].value, word.fields[1][1].value)
        for word in stage.roms[0].contents
    ) == _twiddles(depth)
    assert restore(lower(top, stage=OptimizationStage.HIGH_LEVEL)) == top

    semantic_stage = _semantic_stage(depth)
    locals_by_name = {item.name: item for item in semantic_stage.locals}
    assert str(locals_by_name["high_sum_exact"].type) == "Complex<fixed<17,15>>"
    assert str(locals_by_name["high_difference_exact"].type) == (
        "Complex<fixed<17,15>>"
    )
    assert str(locals_by_name["rotated_difference_exact"].type) == (
        "Complex<fixed<42,37>>"
    )
    assert str(locals_by_name["high_sum_aligned"].type) == (
        "Complex<fixed<42,37>>"
    )

    # The only retained fixed conversions are the explicit raw representation
    # boundary used for exact scale alignment.  In particular there is no
    # RESCALE conversion and no width-reducing storage conversion.
    conversions = _fixed_converts(semantic_stage)
    assert conversions
    assert {item.kind for item in conversions} == {
        expr.FixedConversionKind.TO_RAW,
        expr.FixedConversionKind.FROM_RAW,
    }
    assert all(
        not (
            isinstance(item.expression.type, FixedType)
            and isinstance(item.type, FixedType)
            and item.type.width < item.expression.type.width
        )
        for item in conversions
    )


@pytest.mark.parametrize("depth", (4, 8))
def test_dual_bank_simulator_matches_independent_cycle_oracle(depth: int) -> None:
    cycles, resets = _cycles(depth)
    expected = _oracle(cycles, resets, depth)
    actual = simulate_cycles(_module(depth), cycles, reset=resets)
    assert actual == expected
    assert any(item["output"]["valid"] for item in actual)
    assert any(not item["input"]["transfer"] for item in actual)
    # Continuous accepted traffic after fill reaches one producing transfer per
    # cycle whenever the downstream is not stalling.
    assert any(
        actual[index]["output"]["transfer"]
        and actual[index + 1]["output"]["transfer"]
        for index in range(len(actual) - 1)
    )


def test_complete_six_stage_chain_matches_exact_topology_prefix() -> None:
    frames = [
        [
            (
                (frame_index * 71 + index * 13) % 500 - 250,
                (frame_index * 43 + index * 7) % 400 - 200,
            )
            for index in range(64)
        ]
        for frame_index in range(12)
    ]
    cycles: list[dict[str, object]] = [
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1},
        }
    ]
    cycles.extend(
        {
            "input": {
                "payload": {"re": real, "im": imag},
                "valid": 1,
            },
            "output": {"ready": 1},
        }
        for frame in frames
        for real, imag in frame
    )
    module = _chain_module()
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    result = simulate_cycles(
        module,
        cycles,
        reset=[index == 0 for index in range(len(cycles))],
    )
    actual = [
        (
            item["output"]["payload"]["re"],
            item["output"]["payload"]["im"],
        )
        for item in result
        if item["output"]["transfer"]
    ]
    expected = [
        value
        for frame in frames
        for value in _exact_ifft64_frame(frame)
    ]
    # The finite trace ends before the last feedback values drain.  Every
    # observed value, including more than ten complete symbols, is exact.
    assert len(actual) >= 10 * 64
    assert actual == expected[: len(actual)]


def test_complete_chain_direct_sv_is_bounded_and_strict_lint_clean() -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    artifact = emit_sv_artifact(_chain_module())
    assert len(artifact.text) < 150_000
    assert max(map(len, artifact.text.splitlines())) < 20_000
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / "IFFT64DIFExactChain.sv"
        rtl.write_text(artifact.text)
        publish_companion_bundle(artifact.companions, root)
        lint_with_verilator((rtl,), "IFFT64DIFExactChain")


def test_complete_chain_real_clash_is_bounded_and_lint_clean() -> None:
    clash = find_clash_executable()
    if clash is None or shutil.which("verilator") is None:
        pytest.skip("Clash and Verilator are required")
    module = _chain_module()
    artifact = emit_clash_artifact(module)
    assert len(artifact.text) < 150_000
    assert max(map(len, artifact.text.splitlines())) < 20_000
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = generate_verilog(
            artifact.text,
            "IFFT64DIFExactChain",
            root,
            clash,
            companions=artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(module),
        )
        lint = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "-Wno-WIDTHTRUNC",
                "--top-module",
                "IFFT64DIFExactChain",
                *(str(path) for path in rtl),
            ),
            capture_output=True,
            text=True,
        )
        assert lint.returncode == 0, lint.stderr or lint.stdout


def test_complete_chain_direct_sv_and_clash_match_exact_verilator_trace() -> None:
    clash = find_clash_executable()
    if clash is None or shutil.which("verilator") is None:
        pytest.skip("Clash and Verilator are required")
    frames = [
        [
            (
                (frame_index * 37 + index * 11) % 420 - 210,
                (frame_index * 29 + index * 5) % 360 - 180,
            )
            for index in range(64)
        ]
        for frame_index in range(6)
    ]
    cycles: list[dict[str, object]] = [
        {
            "input": {"payload": {"re": 0, "im": 0}, "valid": 0},
            "output": {"ready": 1},
        }
    ]
    cycles.extend(
        {
            "input": {
                "payload": {"re": real, "im": imag},
                "valid": 1,
            },
            "output": {"ready": 1},
        }
        for frame in frames
        for real, imag in frame
    )
    resets = [index == 0 for index in range(len(cycles))]
    expected = [
        value
        for frame in frames
        for value in _exact_ifft64_frame(frame)
    ]
    module = _chain_module()

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(module)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        publish_companion_bundle(direct_artifact.companions, root)
        direct_records = _chain_rtl_records(
            direct,
            direct=True,
            cycles=cycles,
            resets=resets,
        )

        clash_artifact = emit_clash_artifact(module)
        clash_rtl = generate_verilog(
            clash_artifact.text,
            "IFFT64DIFExactChain",
            root / "clash",
            clash,
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(module),
        )
        clash_records = _chain_rtl_records(
            clash_rtl,
            direct=False,
            cycles=cycles,
            resets=resets,
        )

    assert direct_records == clash_records
    assert len(direct_records) == len(cycles)
    actual = [
        (real, imag)
        for _cycle, _ready, valid, real, imag in direct_records
        if valid
    ]
    assert len(actual) >= 4 * 64
    assert actual == expected[: len(actual)]


@pytest.mark.parametrize("depth", (4, 8))
def test_direct_sv_is_strict_lint_clean(depth: int) -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    artifact = emit_sv_artifact(_module(depth))
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / f"IFFT64DIFStageExactD{depth}.sv"
        rtl.write_text(artifact.text)
        publish_companion_bundle(artifact.companions, root)
        lint_with_verilator((rtl,), f"IFFT64DIFStageExactD{depth}")


@pytest.mark.parametrize("depth", (4, 8))
def test_real_clash_generates_and_lints_with_established_rom_index_waiver(
    depth: int,
) -> None:
    clash = find_clash_executable()
    if clash is None or shutil.which("verilator") is None:
        pytest.skip("Clash and Verilator are required")
    module = _module(depth)
    artifact = emit_clash_artifact(module)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = generate_verilog(
            artifact.text,
            f"IFFT64DIFStageExactD{depth}",
            root,
            clash,
            companions=artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(module),
        )
        # Clash 1.11 romFile widens its Verilog selector to host Int width.
        # This established generated-ROM waiver does not cover arithmetic,
        # payload, FIFO, or register width diagnostics.
        lint = subprocess.run(
            (
                "verilator",
                "--lint-only",
                "-Wno-WIDTHTRUNC",
                "--top-module",
                f"IFFT64DIFStageExactD{depth}",
                *(str(path) for path in rtl),
            ),
            capture_output=True,
            text=True,
        )
        assert lint.returncode == 0, lint.stderr or lint.stdout


@pytest.mark.parametrize("depth", (4, 8))
def test_direct_sv_and_real_clash_cycle_traces_match_integer_oracle(
    depth: int,
) -> None:
    clash = find_clash_executable()
    if clash is None or shutil.which("verilator") is None:
        pytest.skip("Clash and Verilator are required")
    cycles, resets = _cycles(depth)
    expected = _oracle(cycles, resets, depth)
    module = _module(depth)

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        direct_artifact = emit_sv_artifact(module)
        direct = root / "direct.sv"
        direct.write_text(direct_artifact.text)
        publish_companion_bundle(direct_artifact.companions, root)
        direct_records = _rtl_records(
            direct,
            direct=True,
            depth=depth,
            cycles=cycles,
            resets=resets,
        )

        clash_artifact = emit_clash_artifact(module)
        clash_rtl = generate_verilog(
            clash_artifact.text,
            f"IFFT64DIFStageExactD{depth}",
            root / "clash",
            clash,
            companions=clash_artifact.companions,
            public_wrapper=ClashPublicTopWrapper.build(module),
        )
        clash_records = _rtl_records(
            clash_rtl,
            direct=False,
            depth=depth,
            cycles=cycles,
            resets=resets,
        )

    assert direct_records == clash_records
    assert len(direct_records) == len(cycles)
    for index, record in enumerate(direct_records):
        if resets[index]:
            continue
        _cycle, input_ready, output_valid, output_re, output_im = record
        assert input_ready == expected[index]["input"]["ready"]
        assert output_valid == expected[index]["output"]["valid"]
        if output_valid:
            assert (output_re, output_im) == (
                expected[index]["output"]["payload"]["re"],
                expected[index]["output"]["payload"]["im"],
            )
