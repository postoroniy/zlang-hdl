"""IEEE-authoritative packet encoder and OFDM interleaver tests.

The integer models below are independent of ZLang IR and deliberately use the
IEEE bit-order contract instead of importing the historical BSV fixture.
"""

from __future__ import annotations

import os
from pathlib import Path
import random
import shutil
import subprocess

import pytest

from tests.toolchain import CLASH_EXECUTABLE
from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.clash.public_wrapper import ClashPublicTopWrapper
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file
from zlang.opt import OptimizationStage, lower, restore
from zlang.simulate import simulate, simulate_cycles
from zlang.toolchain import generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (
    ROOT
    / "examples"
    / "projects"
    / "80211a_transmitter"
    / "src"
    / "interleaver.zl"
)
ENCODER_SOURCE = SOURCE.with_name("conv_encoder.zl")
ENCODE_KERNEL = "IeeeConvolutionalEncode24"
INTERLEAVE_KERNEL = "IeeeInterleaverBlock48"
TOP = "IeeePacketEncoderInterleaver24"
VERILATOR = shutil.which("verilator")

N_DBPS = {1: 24, 2: 48, 4: 96}


def encode_lsb_first(word: int, history: int = 0) -> tuple[int, int]:
    """K=7 (133,171), consuming IEEE representation bit zero first."""

    if not 0 <= word < 1 << 24:
        raise ValueError("word must fit 24 bits")
    if not 0 <= history < 1 << 6:
        raise ValueError("history must fit 6 bits")
    encoded = 0
    for index in range(24):
        bit = (word >> index) & 1
        g0 = (
            bit
            ^ ((history >> 4) & 1)
            ^ ((history >> 3) & 1)
            ^ ((history >> 1) & 1)
            ^ (history & 1)
        )
        g1 = (
            bit
            ^ ((history >> 5) & 1)
            ^ ((history >> 4) & 1)
            ^ ((history >> 3) & 1)
            ^ (history & 1)
        )
        encoded = (encoded << 2) | (g0 << 1) | g1
        history = ((bit << 5) | (history >> 1)) & 0x3F
    return encoded, history


def _bits_msb(value: int, width: int) -> list[int]:
    return [(value >> (width - 1 - index)) & 1 for index in range(width)]


def _from_bits_msb(bits: list[int]) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | bit
    return value


def ieee_interleave(words: list[int]) -> tuple[int, ...]:
    """Both IEEE 802.11 interleaver permutations for one OFDM symbol."""

    if len(words) not in {1, 2, 4}:
        raise ValueError("one, two, or four encoded words are required")
    if any(not 0 <= word < 1 << 48 for word in words):
        raise ValueError("encoded word must fit 48 bits")
    source = [bit for word in words for bit in _bits_msb(word, 48)]
    count = len(source)
    first = [0] * count
    for source_index, bit in enumerate(source):
        destination = (count // 16) * (source_index % 16) + source_index // 16
        first[destination] = bit
    span = max(len(words) // 2, 1)
    second = [0] * count
    for first_index, bit in enumerate(first):
        destination = span * (first_index // span) + (
            first_index + count - (16 * first_index) // count
        ) % span
        second[destination] = bit
    return tuple(
        _from_bits_msb(second[index : index + 48])
        for index in range(0, count, 48)
    )


def _framed_words(rate: int, payload: bytes) -> list[dict[str, object]]:
    bits = [0] * 16
    psdu_ranges: list[range] = []
    for byte in payload:
        start = len(bits)
        bits.extend((byte >> index) & 1 for index in range(8))
        psdu_ranges.append(range(start, start + 8))
    tail_start = len(bits)
    bits.extend([0] * 6)
    while len(bits) % N_DBPS[rate]:
        bits.append(0)
    words_per_symbol = N_DBPS[rate] // 24
    result = []
    for word_index, start in enumerate(range(0, len(bits), 24)):
        raw = sum(bit << offset for offset, bit in enumerate(bits[start : start + 24]))
        slot = word_index % words_per_symbol
        result.append(
            {
                "data": raw,
                "meta": {
                    "rate": rate,
                    "valid_bytes": sum(
                        1
                        for occupied in psdu_ranges
                        if start <= occupied.start < start + 24
                    ),
                    "tail_mask": sum(
                        1 << (absolute - start)
                        for absolute in range(tail_start, tail_start + 6)
                        if start <= absolute < start + 24
                    ),
                    "symbol_first": int(slot == 0),
                    "symbol_last": int(slot == words_per_symbol - 1),
                },
                "first": int(word_index == 0),
                "last": int(word_index == len(bits) // 24 - 1),
            }
        )
    return result


def _scramble(words: list[dict[str, object]]) -> list[dict[str, object]]:
    state = 0x4B
    result = []
    for beat in words:
        if beat["first"]:
            state = 0x4B
        mask = 0
        for index in range(24):
            feedback = ((state >> 0) ^ (state >> 3)) & 1
            mask |= feedback << index
            state = ((feedback << 6) | (state >> 1)) & 0x7F
        result.append(
            {
                "data": (int(beat["data"]) ^ mask)
                & (~int(beat["meta"]["tail_mask"]) & 0xFFFFFF),
                "meta": dict(beat["meta"]),
                "first": beat["first"],
                "last": beat["last"],
            }
        )
    return result


def _ieee_words(rate: int, payload: bytes) -> list[dict[str, object]]:
    scrambled = _scramble(_framed_words(rate, payload))
    encoded = []
    history = 0
    for beat in scrambled:
        if beat["first"]:
            history = 0
        data, history = encode_lsb_first(int(beat["data"]), history)
        encoded.append({**beat, "data": data})
        if beat["last"]:
            history = 0
    result = []
    for start in range(0, len(encoded), rate):
        group = encoded[start : start + rate]
        transformed = ieee_interleave([int(beat["data"]) for beat in group])
        result.extend({**beat, "data": data} for beat, data in zip(group, transformed, strict=True))
    return result


def _psdu_beats(payload: bytes) -> list[dict[str, object]]:
    chunks = [payload[index : index + 3] for index in range(0, len(payload), 3)]
    return [
        {
            "data": sum(byte << (8 * lane) for lane, byte in enumerate(chunk)),
            "meta": len(chunk),
            "first": int(index == 0),
            "last": int(index == len(chunks) - 1),
        }
        for index, chunk in enumerate(chunks)
    ]


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


def _run_packet(module, rate: int, payload: bytes) -> list[dict[str, object]]:
    cycles = [_cycle(), _cycle(command_valid=1, rate=rate, length=len(payload))]
    cycles.extend(_cycle(psdu_valid=1, psdu=beat) for beat in _psdu_beats(payload))
    cycles.extend(_cycle() for _ in range(48))
    result = simulate_cycles(
        module,
        cycles,
        reset=[True] + [False] * (len(cycles) - 1),
    )
    return [
        cycle["output"]["payload"]
        for cycle in result
        if cycle["output"]["transfer"]
    ]


def _pack_output_beat(beat: dict[str, object]) -> int:
    meta = beat["meta"]
    meta_raw = (
        (int(meta["rate"]) << 28)
        | (int(meta["valid_bytes"]) << 26)
        | (int(meta["tail_mask"]) << 2)
        | (int(meta["symbol_first"]) << 1)
        | int(meta["symbol_last"])
    )
    return (
        (int(beat["data"]) << 33)
        | (meta_raw << 2)
        | (int(beat["first"]) << 1)
        | int(beat["last"])
    )


def _verilate_and_run(
    tmp_path: Path,
    rtl: tuple[Path, ...],
    suffix: str,
) -> None:
    expected = [_pack_output_beat(beat) for beat in _ieee_words(4, b"\xab")]
    checks = "\n".join(
        f"    expect_output(81'h{value:021x}, {index});"
        for index, value in enumerate(expected)
    )
    testbench = tmp_path / f"ieee_encoder_interleaver_{suffix}_tb.sv"
    testbench.write_text(
        f"""
module tb;
  logic clk = 0;
  logic rst = 0;
  logic [2:0] command_payload_rate = '0;
  logic [11:0] command_payload_length = '0;
  logic command_valid = 0;
  wire command_ready;
  logic [23:0] psdu_payload_data = '0;
  logic [1:0] psdu_payload_meta = '0;
  logic psdu_payload_first = 0;
  logic psdu_payload_last = 0;
  logic psdu_valid = 0;
  wire psdu_ready;
  wire [23:0] signal_payload;
  wire signal_valid;
  logic signal_ready = 1;
  wire [47:0] output_payload_data;
  wire [2:0] output_payload_meta_rate;
  wire [1:0] output_payload_meta_valid_bytes;
  wire [23:0] output_payload_meta_tail_mask;
  wire output_payload_meta_symbol_first;
  wire output_payload_meta_symbol_last;
  wire output_payload_first;
  wire output_payload_last;
  wire [80:0] output_payload = {{
    output_payload_data, output_payload_meta_rate,
    output_payload_meta_valid_bytes, output_payload_meta_tail_mask,
    output_payload_meta_symbol_first, output_payload_meta_symbol_last,
    output_payload_first, output_payload_last
  }};
  wire output_valid;
  logic output_ready = 0;

  IeeePacketEncoderInterleaver24 dut (
    .clk(clk), .rst(rst),
    .command_payload_rate(command_payload_rate),
    .command_payload_length(command_payload_length), .command_valid(command_valid),
    .command_ready(command_ready),
    .psdu_payload_data(psdu_payload_data), .psdu_payload_meta(psdu_payload_meta),
    .psdu_payload_first(psdu_payload_first), .psdu_payload_last(psdu_payload_last),
    .psdu_valid(psdu_valid),
    .psdu_ready(psdu_ready),
    .signal_payload(signal_payload), .signal_valid(signal_valid),
    .signal_ready(signal_ready),
    .output_payload_data(output_payload_data),
    .output_payload_meta_rate(output_payload_meta_rate),
    .output_payload_meta_valid_bytes(output_payload_meta_valid_bytes),
    .output_payload_meta_tail_mask(output_payload_meta_tail_mask),
    .output_payload_meta_symbol_first(output_payload_meta_symbol_first),
    .output_payload_meta_symbol_last(output_payload_meta_symbol_last),
    .output_payload_first(output_payload_first), .output_payload_last(output_payload_last),
    .output_valid(output_valid), .output_ready(output_ready)
  );

  task automatic tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask

  task automatic send_command;
    integer timeout;
    begin
      command_payload_rate = 3'd4;
      command_payload_length = 12'd1;
      command_valid = 1;
      #1;
      for (timeout = 0; timeout < 32 && !command_ready; timeout = timeout + 1)
        tick();
      if (!command_ready) $fatal(1, "command timeout");
      tick();
      command_valid = 0;
    end
  endtask

  task automatic send_psdu;
    integer timeout;
    begin
      psdu_payload_data = 24'h0000ab;
      psdu_payload_meta = 2'd1;
      psdu_payload_first = 1;
      psdu_payload_last = 1;
      psdu_valid = 1;
      #1;
      for (timeout = 0; timeout < 32 && !psdu_ready; timeout = timeout + 1)
        tick();
      if (!psdu_ready) $fatal(1, "PSDU timeout");
      tick();
      psdu_valid = 0;
    end
  endtask

  task automatic expect_output(input logic [80:0] expected, input integer code);
    integer timeout;
    begin
      output_ready = 1;
      #1;
      for (timeout = 0; timeout < 96 && !output_valid; timeout = timeout + 1)
        tick();
      if (!output_valid) $fatal(1, "output timeout %0d", code);
      if (output_payload !== expected)
        $fatal(1, "output mismatch %0d got=%h expected=%h", code,
               output_payload, expected);
      tick();
      output_ready = 0;
    end
  endtask

  logic [80:0] held;
  integer timeout;
  initial begin
    rst = 1; tick(); rst = 0;
    if (output_valid) $fatal(1, "valid after reset");
    send_command();
    send_psdu();
    for (timeout = 0; timeout < 96 && !output_valid; timeout = timeout + 1)
      tick();
    if (!output_valid) $fatal(1, "stalled output timeout");
    held = output_payload;
    repeat (2) begin
      tick();
      if (!output_valid || output_payload !== held)
        $fatal(1, "stalled output changed");
    end
{checks}
    repeat (3) tick();
    if (output_valid) $fatal(1, "unexpected duplicate output");
    $finish;
  end
endmodule
"""
    )
    object_dir = tmp_path / f"obj_{suffix}"
    executable = f"ieee_encoder_interleaver_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "--top-module",
            "tb",
            "--Mdir",
            str(object_dir),
            "-o",
            executable,
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            *(str(path) for path in rtl),
            str(testbench),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(object_dir / executable),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


def test_ieee_convolutional_kernel_bit_order_and_history() -> None:
    module = compile_file(
        ENCODER_SOURCE, top=ENCODE_KERNEL, include_clash=False
    ).ir
    generator = random.Random(0x80211A24)
    vectors = [(0, 0), (1, 0), (0x800000, 0), (0xFFFFFF, 0x3F)]
    vectors.extend(
        (generator.randrange(1 << 24), generator.randrange(1 << 6))
        for _ in range(192)
    )
    for word, history in vectors:
        encoded, next_history = encode_lsb_first(word, history)
        assert simulate(module, word=word, history=history)["result"] == {
            "encoded": encoded,
            "history": next_history,
        }

    # The boundary really is different from the historical MSB-first contract.
    assert encode_lsb_first(1)[0] != encode_lsb_first(1 << 23)[0]


@pytest.mark.parametrize("rate", [1, 2, 4])
def test_ieee_interleaver_kernel_matches_two_permutation_oracle(rate: int) -> None:
    module = compile_file(SOURCE, top=INTERLEAVE_KERNEL, include_clash=False).ir
    generator = random.Random(0x1EEE000 + rate)
    vectors = [[1, 0, 0, 0]]
    vectors.extend(
        [generator.randrange(1 << 48) for _ in range(4)] for _ in range(64)
    )
    for words in vectors:
        expected_words = ieee_interleave(words[:rate])
        expected = 0
        for index in range(4):
            expected <<= 48
            if index < rate:
                expected |= expected_words[index]
        actual = simulate(
            module,
            raw_rate=rate,
            w0=words[0],
            w1=words[1],
            w2=words[2],
            w3=words[3],
        )["block"]
        assert actual == expected

    if rate == 4:
        assert ieee_interleave([1, 0, 0, 0]) == (0, 0, 0, 0x100)


@pytest.mark.parametrize(
    ("rate", "payload"),
    [
        (1, b"\x11\x22\x33\x44"),
        (2, b"\xa5\x5a\xc3\x3c"),
        (4, bytes(range(1, 11))),
    ],
)
def test_full_ieee_slice_matches_packet_oracle(
    rate: int, payload: bytes
) -> None:
    module = compile_file(SOURCE, top=TOP, include_clash=False).ir
    assert _run_packet(module, rate, payload) == _ieee_words(rate, payload)


def test_output_stall_and_reset_start_a_new_packet_epoch() -> None:
    module = compile_file(SOURCE, top=TOP, include_clash=False).ir
    payload = b"\xab"
    beat = _psdu_beats(payload)[0]
    cycles = [
        _cycle(),
        _cycle(command_valid=1, rate=4, length=1, output_ready=0),
        _cycle(psdu_valid=1, psdu=beat, output_ready=0),
        *(_cycle(output_ready=0) for _ in range(14)),
        *(_cycle(output_ready=1) for _ in range(12)),
    ]
    stalled = simulate_cycles(
        module,
        cycles,
        reset=[True] + [False] * (len(cycles) - 1),
    )
    held = [
        result["output"]["payload"]
        for stimulus, result in zip(cycles, stalled, strict=True)
        if result["output"]["valid"] and not stimulus["output"]["ready"]
    ]
    assert held and held == [held[0]] * len(held)
    assert [
        cycle["output"]["payload"]
        for cycle in stalled
        if cycle["output"]["transfer"]
    ] == _ieee_words(4, payload)

    # Reset after a complete symbol has entered the hierarchy but before it is
    # observed.  No prior-epoch data may emerge after reset.
    cycles = [
        _cycle(),
        _cycle(command_valid=1, rate=1, length=1, output_ready=0),
        _cycle(psdu_valid=1, psdu=beat, output_ready=0),
        *(_cycle(output_ready=0) for _ in range(8)),
        _cycle(output_ready=1),
        *(_cycle(output_ready=1) for _ in range(8)),
    ]
    resets = [True] + [False] * 10 + [True] + [False] * 8
    after_reset = simulate_cycles(module, cycles, reset=resets)
    assert not any(cycle["output"]["transfer"] for cycle in after_reset[11:])


def test_back_to_back_packets_restart_convolutional_history() -> None:
    module = compile_file(SOURCE, top=TOP, include_clash=False).ir
    first = b"\x01"
    second = b"\x80"
    cycles = [_cycle()]
    for payload in (first, second):
        cycles.append(_cycle(command_valid=1, rate=1, length=1))
        cycles.append(_cycle(psdu_valid=1, psdu=_psdu_beats(payload)[0]))
        cycles.extend(_cycle() for _ in range(20))
    result = simulate_cycles(
        module,
        cycles,
        reset=[True] + [False] * (len(cycles) - 1),
    )
    assert [
        cycle["output"]["payload"]
        for cycle in result
        if cycle["output"]["transfer"]
    ] == _ieee_words(1, first) + _ieee_words(1, second)


def test_semantic_canonical_and_backend_artifacts_are_deterministic() -> None:
    module = compile_file(SOURCE, top=TOP, include_clash=False).ir
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    assert [item.instance.name for item in module.elaborated_instances] == [
        "framer",
        "scrambler",
        "encoder",
        "interleaver",
    ]

    for emitter in (emit_sv_artifact, emit_clash_artifact):
        first = emitter(module)
        second = emitter(module)
        assert first.text == second.text
        assert first.artifact_hash == second.artifact_hash
        restored = BackendArtifact.from_json(first.to_json())
        assert restored.artifact_hash == first.artifact_hash
        assert restored.bindings == first.bindings
        assert restored.instances == first.instances
        assert first.root_module_identity is not None
        assert first.dependency_closure is not None
        dependencies = {
            item.logical_path for item in first.dependency_closure.modules
        }
        assert {
            "wifi80211a_transmitter.controller",
            "wifi80211a_transmitter.data_types",
            "wifi80211a_transmitter.conv_encoder",
        } <= dependencies
        bindings = {item.semantic_signal_id: item for item in first.bindings}
        for semantic_id in (
            "port:command.payload.rate",
            "port:psdu.payload.data",
            "port:output.payload.data",
            "port:output.payload.meta.rate",
            "port:output.payload.meta.tail_mask",
        ):
            assert bindings[semantic_id].physical_available


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_ieee_top_direct_sv_strict_lint(tmp_path: Path) -> None:
    module = compile_file(SOURCE, top=TOP, include_clash=False).ir
    artifact = emit_sv_artifact(module)
    rtl = tmp_path / f"{TOP}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), TOP)
    _verilate_and_run(tmp_path, (rtl,), "direct_sv")


@pytest.mark.skipif(
    CLASH_EXECUTABLE is None or VERILATOR is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_ieee_kernels_real_clash_and_strict_verilator(tmp_path: Path) -> None:
    for source, top in (
        (ENCODER_SOURCE, ENCODE_KERNEL),
        (SOURCE, INTERLEAVE_KERNEL),
        (SOURCE, TOP),
    ):
        compilation = compile_file(source, top=top)
        rtl = tuple(
            generate_verilog(
                compilation.clash,
                top,
                tmp_path / f"clash_{top}",
                CLASH_EXECUTABLE,
                public_wrapper=ClashPublicTopWrapper.build(compilation.ir),
            )
        )
        lint_with_verilator(rtl, top)
        if top == TOP:
            _verilate_and_run(tmp_path, rtl, "clash")
