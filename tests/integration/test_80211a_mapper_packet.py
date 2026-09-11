"""IEEE mapper and natural-order serializer validation."""

from __future__ import annotations

import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from tests.integration.test_80211a_interleaver_packet import (
    _ieee_words,
    _psdu_beats,
)
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate, simulate_cycles
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "mapper.zhl"
)
KERNEL = "IeeeMapperBlock64"
FRAME_KERNEL = "IeeeMapperFrame64"
PACKET_TOP = "IeeePacketMapper64"
VERILATOR = shutil.which("verilator")
MASK48 = (1 << 48) - 1
DATA_CARRIERS = (
    *range(6, 11),
    *range(12, 25),
    *range(26, 32),
    *range(33, 39),
    *range(40, 53),
    *range(54, 59),
)


def _bits_msb(value: int, width: int) -> list[int]:
    return [(value >> (width - 1 - index)) & 1 for index in range(width)]


def _bpsk(value: int) -> tuple[int, int]:
    return (32767 if value else -32768), 0


def _qpsk(value: int) -> tuple[int, int]:
    levels = (-23170, 23170)
    return levels[(value >> 1) & 1], levels[value & 1]


def _qam16(value: int) -> tuple[int, int]:
    levels = (-31086, -10362, 31086, 10362)
    return levels[(value >> 2) & 3], levels[value & 3]


def ieee_mapper_frame(
    rate: int, words: list[int], polarity: int
) -> list[tuple[int, int]]:
    """Independent IEEE constellation and subcarrier placement oracle."""

    assert rate in (1, 2, 4)
    assert len(words) == rate
    source = [_bits_msb(word & MASK48, 48) for word in words]
    data: list[tuple[int, int]] = []
    for index in range(48):
        value = 0
        for lane in source:
            value = (value << 1) | lane[index]
        data.append(
            _bpsk(value)
            if rate == 1
            else (_qpsk(value) if rate == 2 else _qam16(value))
        )
    result = [(0, 0) for _ in range(64)]
    for destination, symbol in zip(DATA_CARRIERS, data, strict=True):
        result[destination] = symbol
    pilot = _bpsk(polarity)
    inverse = _bpsk(0 if polarity else 1)
    result[11], result[25], result[39], result[53] = (
        pilot,
        pilot,
        pilot,
        inverse,
    )
    return result


def _cycle(
    *,
    command_valid: int = 0,
    rate: int = 0,
    length: int = 0,
    psdu_valid: int = 0,
    psdu: dict[str, object] | None = None,
    output_ready: int = 1,
) -> dict[str, object]:
    return {
        "command": {
            "payload": {"rate": rate, "length": length},
            "valid": command_valid,
        },
        "psdu": {
            "payload": psdu
            if psdu is not None
            else {"data": 0, "meta": 0, "first": 0, "last": 0},
            "valid": psdu_valid,
        },
        "signal": {"ready": 1},
        "output": {"ready": output_ready},
    }


def _expected_packet(rate: int, payload: bytes) -> list[dict[str, object]]:
    chunks = _ieee_words(rate, payload)
    pilot_state = 0x78869B7EEC8A4A79958C25EA82D72380
    pilot_mask = (1 << 127) - 1
    result: list[dict[str, object]] = []
    for symbol_index, start in enumerate(range(0, len(chunks), rate)):
        group = chunks[start : start + rate]
        # vec[126] is the least-significant representation bit under the
        # frozen packed-vector order used by the source pilot register.
        polarity = pilot_state & 1
        pilot_state = ((pilot_state << 1) & pilot_mask) | (pilot_state >> 126)
        frame = ieee_mapper_frame(
            rate,
            [int(item["data"]) for item in group],
            polarity,
        )
        for sample_index, (real, imag) in enumerate(frame):
            result.append(
                {
                    "data": {"re": real, "im": imag},
                    "meta": {
                        "rate": rate,
                        "symbol_index": symbol_index,
                        "symbol_first": int(sample_index == 0),
                        "symbol_last": int(sample_index == 63),
                    },
                    "first": int(symbol_index == 0 and sample_index == 0),
                    "last": int(
                        symbol_index == len(chunks) // rate - 1
                        and sample_index == 63
                    ),
                }
            )
    return result


def test_mapper_kernel_matches_ieee_rates_constellations_and_pilots() -> None:
    module = compile_file(SOURCE, top=KERNEL).ir
    generator = random.Random(0x80211A)
    for rate in (1, 2, 4):
        for polarity in (0, 1):
            for _ in range(8):
                words = [generator.randrange(1 << 48) for _ in range(rate)]
                padded = [*words, *([0] * (4 - len(words)))]
                result = simulate(
                    module,
                    raw_rate=rate,
                    word0=padded[0],
                    word1=padded[1],
                    word2=padded[2],
                    word3=padded[3],
                    polarity=polarity,
                )
                assert result["valid"] == 1
                assert result["frame"] == [
                    {"re": real, "im": imag}
                    for real, imag in ieee_mapper_frame(rate, words, polarity)
                ]


def test_sparse_invalid_rate_fails_closed_at_raw_boundary() -> None:
    module = compile_file(SOURCE, top=KERNEL).ir
    for raw_rate in (0, 3, 5, 6, 7):
        result = simulate(
            module,
            raw_rate=raw_rate,
            word0=MASK48,
            word1=MASK48,
            word2=MASK48,
            word3=MASK48,
            polarity=1,
        )
        assert result["valid"] == 0
        assert result["frame"][6] == {"re": 0, "im": 0}


def test_packet_mapper_streams_all_rates_holds_stalls_and_preserves_metadata() -> None:
    module = compile_file(SOURCE, top=PACKET_TOP).ir
    for rate, payload in ((1, b"\x55"), (2, b"\xa5"), (4, b"\x3c")):
        beats = _psdu_beats(payload)
        cycles = [
            _cycle(),
            _cycle(command_valid=1, rate=rate, length=len(payload)),
            *(_cycle(psdu_valid=1, psdu=beat) for beat in beats),
        ]
        # Downstream stalls are independent of the upstream packet timing.
        cycles.extend(
            _cycle(output_ready=int(index % 7 not in (2, 3)))
            for index in range(320)
        )
        traced = simulate_cycles(
            module,
            cycles,
            reset=[True] + [False] * (len(cycles) - 1),
        )
        transferred = [
            cycle["output"]["payload"]
            for cycle in traced
            if cycle["output"]["transfer"]
        ]
        assert transferred == _expected_packet(rate, payload)
        held = [
            cycle["output"]["payload"]
            for cycle, stimulus in zip(traced, cycles, strict=True)
            if cycle["output"]["valid"]
            and not stimulus["output"]["ready"]
        ]
        assert held


def test_reset_discards_a_stalled_symbol_and_restarts_pilot_epoch() -> None:
    module = compile_file(SOURCE, top=PACKET_TOP).ir
    payload = b"\x55"
    beat = _psdu_beats(payload)[0]
    cycles = [
        _cycle(),
        _cycle(command_valid=1, rate=1, length=1, output_ready=0),
        _cycle(psdu_valid=1, psdu=beat, output_ready=0),
        *(_cycle(output_ready=0) for _ in range(12)),
        _cycle(output_ready=0),
        _cycle(command_valid=1, rate=1, length=1),
        _cycle(psdu_valid=1, psdu=beat),
        *(_cycle() for _ in range(180)),
    ]
    reset_cycle = 15
    resets = [False] * len(cycles)
    resets[0] = True
    resets[reset_cycle] = True
    traced = simulate_cycles(module, cycles, reset=resets)
    assert not any(
        cycle["output"]["transfer"] for cycle in traced[: reset_cycle + 1]
    )
    transferred = [
        cycle["output"]["payload"]
        for cycle in traced[reset_cycle + 1 :]
        if cycle["output"]["transfer"]
    ]
    assert transferred == _expected_packet(1, payload)


def test_mapper_call_graph_canonical_roundtrip_and_packet_hierarchy() -> None:
    kernel = compile_file(SOURCE, top=KERNEL)
    packet = compile_file(SOURCE, top=PACKET_TOP)
    assert restore(lower(kernel.ir, stage=OptimizationStage.HIGH_LEVEL)) == kernel.ir
    assert restore(lower(packet.ir, stage=OptimizationStage.HIGH_LEVEL)) == packet.ir
    assert [port.type.width for port in kernel.ir.outputs] == [1, 2048]
    assert [instance.module for instance in packet.ir.instances] == [
        "IeeePacketEncoderInterleaver24",
        "IeeeMapper64",
        "IeeeMapperSerializer64",
    ]
    names = [function.name for function in kernel.ir.functions]
    assert names.count("mapper_frame48") == 1
    assert names.count("ieee_mapper_frame") == 1
    assert names.count("raw_mapper_sample") == 1


def test_direct_sv_is_deterministic_and_strict_lint_clean(tmp_path: Path) -> None:
    for top in (FRAME_KERNEL, PACKET_TOP):
        compilation = compile_file(SOURCE, top=top)
        first = emit_sv_artifact(compilation.ir)
        second = emit_sv_artifact(compilation.ir)
        assert first.text == second.text
        assert first.artifact_hash == second.artifact_hash
        assert first.to_json() == second.to_json()
        bindings = {item.semantic_signal_id: item for item in first.bindings}
        if top == FRAME_KERNEL:
            assert bindings["port:frame.re"].physical_available
            assert bindings["port:frame.im"].physical_available
        rtl = tmp_path / f"{top}.sv"
        rtl.write_text(first.text)
        lint_with_verilator((rtl,), top)




@pytest.mark.skipif(VERILATOR is None, reason="Verilator is unavailable")
def test_direct_sv_numerical_smoke(tmp_path: Path) -> None:
    compilation = compile_file(SOURCE, top=FRAME_KERNEL)
    rtl = tmp_path / f"{FRAME_KERNEL}.sv"
    rtl.write_text(emit_sv_artifact(compilation.ir).text)
    words = [0x0123456789AB, 0xFEDCBA987654]
    expected = ieee_mapper_frame(2, words, 1)
    checks = []
    for index in (0, 6, 11, 25, 32, 53, 58, 63):
        real, imag = expected[index]
        checks.append(
            f"if ($signed(frame_re[{index}]) != {real} "
            f"|| $signed(frame_im[{index}]) != {imag}) "
            f"$fatal(1, \"bin {index}\");"
        )
    bench = tmp_path / "tb.sv"
    bench.write_text(
        "\n".join(
            (
                "module tb;",
                "logic [2:0] raw_rate; logic [47:0] word0, word1, word2, word3;",
                "logic polarity; wire signed [15:0] frame_re [0:63]; "
                "wire signed [15:0] frame_im [0:63];",
                f"{FRAME_KERNEL} dut(.*);",
                "initial begin",
                f"raw_rate=2; word0=48'h{words[0]:012x}; word1=48'h{words[1]:012x};",
                "word2=0; word3=0; polarity=1; #1;",
                *checks,
                "$finish; end endmodule",
            )
        )
    )
    output = tmp_path / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            VERILATOR,
            "--binary",
            "--timing",
            "-Wno-fatal",
            "--Mdir",
            str(output),
            "--top-module",
            "tb",
            str(rtl),
            str(bench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(output / "Vtb"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout
