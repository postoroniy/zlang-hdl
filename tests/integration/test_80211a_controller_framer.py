"""IEEE-authoritative DATA/SIGNAL framing for the Wi-Fi controller path."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_file, compile_source
from zlang.formal import build_recursive_formal_design
from zlang.ir import Concat, Constant
from zlang.opt import OptimizationStage, lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate, simulate_cycles
from zlang.toolchain import lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
PROJECT = ROOT / "examples" / "projects" / "80211a_transmitter"
SOURCE = PROJECT / "src" / "controller.zhl"
HEADER = "IeeeSignalHeader24"
TOP = "IeeeDataFramer24"
SCRAMBLED_TOP = "IeeePacketFramerScrambler24"
VERILATOR = shutil.which("verilator")

RATE_CODES = {1: 0b1101, 2: 0b0101, 4: 0b1001}
N_DBPS = {1: 24, 2: 48, 4: 96}


def _signal_header(rate: int, length: int) -> int:
    code = RATE_CODES[rate]
    parity = ((code << 12) | length).bit_count() & 1
    # IEEE SIGNAL bit zero is transmitted first: RATE [3:0], reserved [4],
    # LENGTH [16:5], parity [17], tail [23:18].
    return code | (length << 5) | (parity << 17)


def _framed_words(rate: int, payload: bytes) -> list[dict[str, object]]:
    """Build the packet without consulting ZLang IR or backend layout."""

    bits = [0] * 16
    psdu_ranges: list[range] = []
    for byte in payload:
        start = len(bits)
        bits.extend((byte >> bit) & 1 for bit in range(8))
        psdu_ranges.append(range(start, start + 8))
    tail_start = len(bits)
    bits.extend([0] * 6)
    while len(bits) % N_DBPS[rate]:
        bits.append(0)

    words: list[dict[str, object]] = []
    words_per_symbol = N_DBPS[rate] // 24
    for word_index, start in enumerate(range(0, len(bits), 24)):
        raw = sum(bit << offset for offset, bit in enumerate(bits[start : start + 24]))
        tail_mask = sum(
            1 << (absolute - start)
            for absolute in range(tail_start, tail_start + 6)
            if start <= absolute < start + 24
        )
        valid_bytes = sum(
            1 for occupied in psdu_ranges if start <= occupied.start < start + 24
        )
        slot = word_index % words_per_symbol
        words.append(
            {
                "data": raw,
                "meta": {
                    "rate": rate,
                    "valid_bytes": valid_bytes,
                    "tail_mask": tail_mask,
                    "symbol_first": int(slot == 0),
                    "symbol_last": int(slot == words_per_symbol - 1),
                },
                "first": int(word_index == 0),
                "last": int(word_index == len(bits) // 24 - 1),
            }
        )
    return words


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
    signal_ready: int = 1,
    data_ready: int = 1,
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
        "signal": {"ready": signal_ready},
        "data": {"ready": data_ready},
    }


def _run_packet(module, rate: int, payload: bytes) -> tuple[list[int], list[dict[str, object]]]:
    expected = _framed_words(rate, payload)
    cycles = [_cycle(), _cycle(command_valid=1, rate=rate, length=len(payload))]
    cycles.extend(
        _cycle(psdu_valid=1, psdu=beat) for beat in _psdu_beats(payload)
    )
    cycles.extend(_cycle() for _ in range(len(expected) + 5))
    result = simulate_cycles(
        module,
        cycles,
        reset=[True] + [False] * (len(cycles) - 1),
    )
    signals = [
        int(cycle["signal"]["payload"])
        for cycle in result
        if cycle["signal"]["transfer"]
    ]
    words = [
        cycle["data"]["payload"]
        for cycle in result
        if cycle["data"]["transfer"]
    ]
    return signals, words


def _scramble_words(words: list[dict[str, object]]) -> list[dict[str, object]]:
    state = 0x4B
    result: list[dict[str, object]] = []
    for beat in words:
        if beat["first"]:
            state = 0x4B
        mask = 0
        for bit in range(24):
            feedback = ((state >> 0) ^ (state >> 3)) & 1
            mask |= feedback << bit
            state = ((feedback << 6) | (state >> 1)) & 0x7F
        copied = {
            "data": (int(beat["data"]) ^ mask)
            & (~int(beat["meta"]["tail_mask"]) & 0xFFFFFF),
            "meta": dict(beat["meta"]),
            "first": beat["first"],
            "last": beat["last"],
        }
        result.append(copied)
    return result


def test_ieee_signal_codes_and_sparse_rate_boundary() -> None:
    module = compile_file(SOURCE, top=HEADER).ir
    for rate, code in RATE_CODES.items():
        for length in (1, 2, 3, 4, 4095):
            assert simulate(module, raw_rate=rate, length=length) == {
                "valid": 1,
                "header": _signal_header(rate, length),
            }
            assert _signal_header(rate, length) & 0xF == code

    for invalid in (0, 3, 5, 6, 7):
        assert simulate(module, raw_rate=invalid, length=1)["valid"] == 0
    assert simulate(module, raw_rate=1, length=0)["valid"] == 0

    function = next(
        item for item in module.functions if item.name == "ieee_signal_header"
    )

    assert isinstance(function.body, Concat)
    assert tuple(item.type.width for item in function.body.operands) == (6, 1, 17)
    padding, _, signal_data = function.body.operands
    assert isinstance(padding, Constant)
    assert padding.value == 0
    assert isinstance(signal_data, Concat)
    assert tuple(item.type.width for item in signal_data.operands) == (12, 1, 4)


def test_ieee_signal_header_wrong_field_width_fails_at_concat_boundary() -> None:
    source = (
        "fn bad(rate:bits<4>,length:u12)->bits<24>{ "
        "data=concat(length,0,extend<5>(rate)) "
        "concat(zeros<6>,parity(data),data) } "
        "module M{in rate:bits<4> in length:u12 out y:bits<24> "
        "y=bad(rate,length)}"
    )
    with pytest.raises(SemanticError) as caught:
        compile_source(source)
    assert caught.value.code == "ZL-WIDTH-CONCAT"
    assert "produces bits<25>, expected exact bits<24>" in str(caught.value)


@pytest.mark.parametrize(
    ("rate", "length"),
    [
        *((rate, length) for rate in RATE_CODES for length in (1, 2, 3, 4)),
        (1, 3),
        (1, 4),
        (2, 3),
        (2, 4),
        (4, 9),
        (4, 10),
    ],
)
def test_framer_matches_independent_packet_oracle(rate: int, length: int) -> None:
    module = compile_file(SOURCE, top=TOP).ir
    payload = bytes((17 * index + 0x23) & 0xFF for index in range(length))
    signals, words = _run_packet(module, rate, payload)
    assert signals == [_signal_header(rate, length)]
    assert words == _framed_words(rate, payload)


def test_input_shape_validation_stall_and_mid_packet_reset() -> None:
    module = compile_file(SOURCE, top=TOP).ir

    # The final count and first/last markers are part of the accepted contract.
    invalid_cycles = [
        _cycle(),
        _cycle(command_valid=1, rate=2, length=2),
        _cycle(
            psdu_valid=1,
            psdu={"data": 0xBBAA, "meta": 3, "first": 1, "last": 1},
        ),
    ]
    invalid = simulate_cycles(module, invalid_cycles, reset=[True, False, False])
    assert invalid[-1]["psdu"]["ready"] == 0
    assert invalid[-1]["psdu"]["transfer"] == 0

    # Hold the first framed word while the FIFO also accumulates later PAD.
    beat = _psdu_beats(b"\xab")[0]
    stall_cycles = [
        _cycle(),
        _cycle(command_valid=1, rate=4, length=1),
        _cycle(psdu_valid=1, psdu=beat, data_ready=0),
        *(_cycle(data_ready=0) for _ in range(4)),
        *(_cycle(data_ready=1) for _ in range(7)),
    ]
    stalled = simulate_cycles(
        module,
        stall_cycles,
        reset=[True] + [False] * (len(stall_cycles) - 1),
    )
    held = [cycle["data"]["payload"] for cycle in stalled[3:7]]
    assert all(cycle["data"]["valid"] for cycle in stalled[3:7])
    assert held == [held[0]] * len(held)
    assert [
        cycle["data"]["payload"]
        for cycle in stalled
        if cycle["data"]["transfer"]
    ] == _framed_words(4, b"\xab")

    # Reset drops a partially received packet and starts a fresh protocol epoch.
    old = _psdu_beats(b"\x01\x02\x03\x04")
    fresh = _psdu_beats(b"\xa5")
    reset_cycles = [
        _cycle(),
        _cycle(command_valid=1, rate=2, length=4),
        _cycle(psdu_valid=1, psdu=old[0], data_ready=0),
        _cycle(data_ready=0),
        _cycle(command_valid=1, rate=1, length=1),
        _cycle(psdu_valid=1, psdu=fresh[0]),
        *(_cycle() for _ in range(6)),
    ]
    resets = [True, False, False, True] + [False] * (len(reset_cycles) - 4)
    after_reset = simulate_cycles(module, reset_cycles, reset=resets)
    assert after_reset[3]["signal"]["valid"] == 0
    assert after_reset[3]["data"]["valid"] == 0
    assert [
        cycle["data"]["payload"]
        for cycle in after_reset[4:]
        if cycle["data"]["transfer"]
    ] == _framed_words(1, b"\xa5")


@pytest.mark.parametrize(("rate", "length"), [(1, 4), (2, 4), (4, 10)])
def test_ieee_scrambler_forces_tail_zero_and_restarts_packet_epoch(
    rate: int,
    length: int,
) -> None:
    module = compile_file(SOURCE, top=SCRAMBLED_TOP).ir
    payload = bytes((0xA5 + 29 * index) & 0xFF for index in range(length))
    signals, actual = _run_packet(module, rate, payload)
    framed = _framed_words(rate, payload)
    expected = _scramble_words(framed)
    assert signals == [_signal_header(rate, length)]
    assert actual == expected
    for beat in actual:
        tail_mask = int(beat["meta"]["tail_mask"])
        assert int(beat["data"]) & tail_mask == 0

    # A separately reset simulation of the same packet proves that FrameBeat.first
    # selects a fresh pilot/scrambler epoch, independent of prior process state.
    assert _run_packet(module, rate, payload)[1] == expected


def test_framer_canonical_and_direct_sv_artifact_are_deterministic() -> None:
    module = compile_file(SOURCE, top=TOP).ir
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module

    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert first.selected_ir_identity == second.selected_ir_identity
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.artifact_hash == first.artifact_hash
    assert restored.bindings == first.bindings
    assert restored.root_module_identity == first.root_module_identity
    assert restored.dependency_closure == first.dependency_closure
    assert first.root_module_identity is not None
    assert first.dependency_closure is not None
    assert [item.logical_path for item in first.dependency_closure.modules] == [
        "wifi80211a_transmitter.data_types",
        "wifi80211a_transmitter.scrambler",
    ]
    bindings = {item.semantic_signal_id: item for item in first.bindings}
    for semantic_id in (
        "port:command.payload.rate",
        "port:command.payload.length",
        "port:psdu.payload.data",
        "port:data.payload.data",
        "port:data.payload.meta.rate",
        "port:data.payload.meta.tail_mask",
    ):
        assert bindings[semantic_id].physical_available

    composed = compile_file(SOURCE, top=SCRAMBLED_TOP).ir
    recursive = build_recursive_formal_design(composed)
    composed_artifact = emit_sv_artifact(composed, recursive_design=recursive)
    assert composed_artifact.instances
    assert {
        item.source_instance_name for item in composed_artifact.instances
    } >= {
        "framer",
        "scrambler",
    }


def _run_verilator(tmp_path: Path, rtl: Path, testbench: str) -> None:
    tb = tmp_path / "framer_tb.sv"
    tb.write_text(testbench)
    obj = tmp_path / "obj_framer"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    build = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "--top-module",
            "tb",
            "--Mdir",
            str(obj),
            "-o",
            "framer_sim",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSED",
            "-Wno-UNDRIVEN",
            str(rtl),
            str(tb),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(obj / "framer_sim"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_framer_direct_sv_strict_lint_and_behavior(tmp_path: Path) -> None:
    module = compile_file(SOURCE, top=TOP).ir
    artifact = emit_sv_artifact(module)
    rtl = tmp_path / f"{TOP}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), TOP)
    composed_rtl = tmp_path / f"{SCRAMBLED_TOP}.sv"
    composed_rtl.write_text(
        emit_sv_artifact(
            compile_file(SOURCE, top=SCRAMBLED_TOP).ir
        ).text
    )
    lint_with_verilator((composed_rtl,), SCRAMBLED_TOP)

    expected_header = _signal_header(4, 1)
    expected = _framed_words(4, b"\xab")
    packed = []
    for beat in expected:
        meta = beat["meta"]
        meta_raw = (
            (int(meta["rate"]) << 28)
            | (int(meta["valid_bytes"]) << 26)
            | (int(meta["tail_mask"]) << 2)
            | (int(meta["symbol_first"]) << 1)
            | int(meta["symbol_last"])
        )
        packed.append(
            (int(beat["data"]) << 33)
            | (meta_raw << 2)
            | (int(beat["first"]) << 1)
            | int(beat["last"])
        )
    checks = "\n".join(
        f"    take_data(57'h{value:015x});" for value in packed
    )
    testbench = f"""
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
  logic signal_ready = 0;
  wire [23:0] data_payload_data;
  wire [2:0] data_payload_meta_rate;
  wire [1:0] data_payload_meta_valid_bytes;
  wire [23:0] data_payload_meta_tail_mask;
  wire data_payload_meta_symbol_first;
  wire data_payload_meta_symbol_last;
  wire data_payload_first;
  wire data_payload_last;
  wire [56:0] data_payload = {{
    data_payload_data,
    data_payload_meta_rate,
    data_payload_meta_valid_bytes,
    data_payload_meta_tail_mask,
    data_payload_meta_symbol_first,
    data_payload_meta_symbol_last,
    data_payload_first,
    data_payload_last
  }};
  wire data_valid;
  logic data_ready = 0;
  IeeeDataFramer24 dut (.*);
  task automatic tick;
    begin #1 clk = 1; #1 clk = 0; #1; end
  endtask
  task automatic take_data(input logic [56:0] expected);
    integer timeout;
    begin
      for (timeout = 0; timeout < 16 && !data_valid; timeout = timeout + 1)
        tick();
      if (!data_valid) $fatal(1, "data timeout");
      if (data_payload !== expected) $fatal(1, "framed word mismatch");
      data_ready = 1; tick(); data_ready = 0;
    end
  endtask
  initial begin
    rst = 1; tick(); rst = 0;
    command_payload_rate = 3'd4; command_payload_length = 12'd1;
    command_valid = 1;
    if (!command_ready) #1;
    if (!command_ready) $fatal(1, "command not ready");
    tick(); command_valid = 0;
    psdu_payload_data = 24'h0000ab; psdu_payload_meta = 2'd1;
    psdu_payload_first = 1; psdu_payload_last = 1; psdu_valid = 1;
    if (!psdu_ready) #1;
    if (!psdu_ready) $fatal(1, "PSDU not ready");
    tick(); psdu_valid = 0;
    #1;
    if (!signal_valid || signal_payload !== 24'h{expected_header:06x})
      $fatal(1, "SIGNAL mismatch");
    signal_ready = 1; tick(); signal_ready = 0;
{checks}
    rst = 1; tick(); rst = 0; #1;
    if (signal_valid || data_valid) $fatal(1, "reset leaked frame");
    $display("IEEE framer direct-SV ok");
    $finish;
  end
endmodule
"""
    _run_verilator(tmp_path, rtl, testbench)
