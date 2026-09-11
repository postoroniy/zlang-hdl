"""End-to-end IEEE-authoritative bounded 802.11a transmitter validation."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile

import pytest

from tests.integration.test_80211a_ifft_library import (
    _exact_ifft64_frame,
)
from tests.integration.test_80211a_interleaver_packet import (
    _psdu_beats,
)
from tests.integration.test_80211a_controller_framer import _signal_header
from tests.integration.test_80211a_mapper_packet import _expected_packet
from zlang.backend.companions import publish_companion_bundle
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
import zlang.backend.systemverilog.emitter as direct_sv_emitter
import zlang.cli as cli_module
from zlang.compiler import compile_file
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
    / "transmitter.zhl"
)
TOP = "Ieee80211aTransmitter"


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


def _reverse6(value: int) -> int:
    return int(f"{value:06b}"[::-1], 2)


def _expected_time_packet(rate: int, payload: bytes) -> list[dict[str, object]]:
    mapped = _expected_packet(rate, payload)
    assert len(mapped) % 64 == 0
    result: list[dict[str, object]] = []
    symbols = len(mapped) // 64
    for symbol_index in range(symbols):
        frame = mapped[symbol_index * 64 : (symbol_index + 1) * 64]
        dif = _exact_ifft64_frame(
            [
                (int(item["data"]["re"]), int(item["data"]["im"]))
                for item in frame
            ]
        )
        natural = [dif[_reverse6(index)] for index in range(64)]
        with_cp = [*natural[48:64], *natural]
        for output_index, (real, imag) in enumerate(with_cp):
            result.append(
                {
                    "data": {"re": real, "im": imag},
                    "meta": {
                        "rate": rate,
                        "symbol_index": symbol_index,
                        "symbol_first": int(output_index == 0),
                        "symbol_last": int(output_index == 79),
                    },
                    "first": int(symbol_index == 0 and output_index == 0),
                    "last": int(
                        symbol_index == symbols - 1 and output_index == 79
                    ),
                }
            )
    return result


def _run_packet(rate: int, payload: bytes) -> tuple[list[int], list[dict[str, object]]]:
    module = compile_file(SOURCE, top=TOP).ir
    cycles = [_cycle(), _cycle(command_valid=1, rate=rate, length=len(payload))]
    cycles.extend(
        _cycle(psdu_valid=1, psdu=beat) for beat in _psdu_beats(payload)
    )
    cycles.extend(
        _cycle(output_ready=int(index % 13 not in (3, 4, 9)))
        for index in range(1000)
    )
    traced = simulate_cycles(
        module,
        cycles,
        reset=[True] + [False] * (len(cycles) - 1),
    )
    signal = [
        int(item["signal"]["payload"])
        for item in traced
        if item["signal"]["transfer"]
    ]
    output = [
        item["output"]["payload"]
        for item in traced
        if item["output"]["transfer"]
    ]
    return signal, output


@pytest.mark.parametrize("rate,payload", ((1, b"\x55"), (2, b"\xa5"), (4, b"\x3c")))
def test_full_ieee_chain_matches_packet_oracle(
    rate: int, payload: bytes
) -> None:
    signal, output = _run_packet(rate, payload)
    assert signal == [_signal_header(rate, len(payload))]
    assert output == _expected_time_packet(rate, payload)


def test_full_ieee_chain_canonical_and_direct_sv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    compilation = compile_file(SOURCE, top=TOP)
    module = compilation.ir
    assert [child.name for child in module.children] == [
        "IeeePacketMapper64",
        "IeeeFramedIFFT64",
        "IeeeIFFTFramedOutputBoundary",
    ]
    assert restore(lower(module, stage=OptimizationStage.HIGH_LEVEL)) == module
    assert tuple(
        (
            leaf.leaf_semantic_id,
            leaf.external_name,
            leaf.direction.value,
            str(leaf.canonical_type),
            leaf.width,
        )
        for leaf in module.top_physical_abi.leaves
    ) == (
        ("clock", "clk", "input", "bit", 1),
        ("reset", "rst", "input", "bit", 1),
        ("port:command.payload.rate", "command_payload_rate", "input", "bits<3>", 3),
        ("port:command.payload.length", "command_payload_length", "input", "u12", 12),
        ("port:command.valid", "command_valid", "input", "bit", 1),
        ("port:command.ready", "command_ready", "output", "bit", 1),
        ("port:psdu.payload.data", "psdu_payload_data", "input", "bits<24>", 24),
        ("port:psdu.payload.meta", "psdu_payload_meta", "input", "u2", 2),
        ("port:psdu.payload.first", "psdu_payload_first", "input", "bit", 1),
        ("port:psdu.payload.last", "psdu_payload_last", "input", "bit", 1),
        ("port:psdu.valid", "psdu_valid", "input", "bit", 1),
        ("port:psdu.ready", "psdu_ready", "output", "bit", 1),
        ("port:signal.payload", "signal_payload", "output", "bits<24>", 24),
        ("port:signal.valid", "signal_valid", "output", "bit", 1),
        ("port:signal.ready", "signal_ready", "input", "bit", 1),
        ("port:output.payload.data.re", "output_payload_data_re", "output", "fixed<16,15>", 16),
        ("port:output.payload.data.im", "output_payload_data_im", "output", "fixed<16,15>", 16),
        ("port:output.payload.meta.rate", "output_payload_meta_rate", "output", "bits<3>", 3),
        ("port:output.payload.meta.symbol_index", "output_payload_meta_symbol_index", "output", "u12", 12),
        ("port:output.payload.meta.symbol_first", "output_payload_meta_symbol_first", "output", "bit", 1),
        ("port:output.payload.meta.symbol_last", "output_payload_meta_symbol_last", "output", "bit", 1),
        ("port:output.payload.first", "output_payload_first", "output", "bit", 1),
        ("port:output.payload.last", "output_payload_last", "output", "bit", 1),
        ("port:output.valid", "output_valid", "output", "bit", 1),
        ("port:output.ready", "output_ready", "input", "bit", 1),
    )

    first = emit_sv_artifact(module)
    render_calls = 0
    original_render = direct_sv_emitter.emit

    def counted_render(emitted_module, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return original_render(emitted_module, **kwargs)

    monkeypatch.setattr(direct_sv_emitter, "emit", counted_render)
    monkeypatch.setattr(
        cli_module, "compile_file_snapshot", lambda *_args, **_kwargs: compilation
    )
    cli_rtl = tmp_path / f"{TOP}.sv"
    cli_manifest = tmp_path / f"{TOP}.artifact.json"
    assert cli_module.main([
        str(SOURCE),
        "--top", TOP,
        "--systemverilog", str(cli_rtl),
        "--implementation-manifest", str(cli_manifest),
    ]) == 0
    assert render_calls == 1
    assert cli_rtl.read_text() == first.text
    assert json.loads(cli_manifest.read_text())["artifact_hash"] == first.artifact_hash
    bindings = {item.semantic_signal_id: item for item in first.bindings}
    for semantic_id in (
        "port:command.payload.rate",
        "port:command.payload.length",
        "port:psdu.payload.data",
        "port:output.payload.data.re",
        "port:output.payload.data.im",
        "port:output.payload.meta.rate",
    ):
        assert bindings[semantic_id].physical_available
    if shutil.which("verilator") is not None:
        with tempfile.TemporaryDirectory() as temporary:
            rtl = Path(temporary) / f"{TOP}.sv"
            rtl.write_text(first.text)
            lint_with_verilator((rtl,), TOP)


def _signed(value: int, width: int) -> int:
    return value - (1 << width) if value & (1 << (width - 1)) else value


def _unpack_output(value: int) -> dict[str, object]:
    data = (value >> 19) & 0xFFFFFFFF
    meta = (value >> 2) & 0x1FFFF
    return {
        "data": {
            "re": _signed((data >> 16) & 0xFFFF, 16),
            "im": _signed(data & 0xFFFF, 16),
        },
        "meta": {
            "rate": (meta >> 14) & 0x7,
            "symbol_index": (meta >> 2) & 0xFFF,
            "symbol_first": (meta >> 1) & 1,
            "symbol_last": meta & 1,
        },
        "first": (value >> 1) & 1,
        "last": value & 1,
    }


def _run_full_rtl_cycle_trace(
    rtl: Path | Sequence[Path],
    *,
    direct: bool,
    work: Path,
    rate: int,
    payload: bytes,
) -> tuple[list[int], list[dict[str, object]]]:
    rtl_sources = (
        (rtl,)
        if isinstance(rtl, Path)
        else tuple(sorted((Path(item) for item in rtl), key=lambda item: item.as_posix()))
    )
    assert rtl_sources, "Verilator requires at least one generated RTL source"
    rtl_arguments = tuple(str(item) for item in rtl_sources)
    rtl_working_directory = rtl_sources[0].parent
    psdu = _psdu_beats(payload)[0]
    cycle_count = 903
    work.mkdir(parents=True, exist_ok=True)
    bench_lines = [
        "module tb;",
        "logic clk=0; logic rst;",
        "integer cycle;",
        "logic [2:0] command_payload_rate; logic [11:0] command_payload_length; "
        "logic command_valid; wire command_ready;",
        "logic [23:0] psdu_payload_data; logic [1:0] psdu_payload_meta; "
        "logic psdu_payload_first; logic psdu_payload_last; "
        "logic psdu_valid; wire psdu_ready;",
        "wire [23:0] signal_payload; wire signal_valid; logic signal_ready;",
        "wire signed [15:0] output_payload_data_re; "
        "wire signed [15:0] output_payload_data_im;",
        "wire [2:0] output_payload_meta_rate; "
        "wire [11:0] output_payload_meta_symbol_index;",
        "wire output_payload_meta_symbol_first; wire output_payload_meta_symbol_last; "
        "wire output_payload_first; wire output_payload_last;",
        "wire [50:0] output_payload = {output_payload_data_re, "
        "output_payload_data_im, output_payload_meta_rate, "
        "output_payload_meta_symbol_index, output_payload_meta_symbol_first, "
        "output_payload_meta_symbol_last, output_payload_first, output_payload_last};",
        "wire output_valid; logic output_ready;",
        f"{TOP} dut(.*);",
        "initial begin",
        f"command_payload_rate=3'd{rate}; command_payload_length=12'd{len(payload)};",
        f"psdu_payload_data=24'h{int(psdu['data']):06x}; "
        f"psdu_payload_meta=2'd{int(psdu['meta'])}; "
        f"psdu_payload_first={int(psdu['first'])}; psdu_payload_last={int(psdu['last'])};",
        "signal_ready=1; output_ready=1;",
        f"for (cycle=0; cycle<{cycle_count}; cycle=cycle+1) begin",
        "  rst=(cycle==0); command_valid=(cycle==1); psdu_valid=(cycle==2); #1;",
        '  $display("REC %0d %0d %0d %0d %06x %0d %013x", cycle, '
        "command_ready, psdu_ready, signal_valid, signal_payload, "
        "output_valid, output_payload);",
        "  #1 clk=1; #1 clk=0;",
        "end",
        "$finish;",
        "end",
        "endmodule",
    ]
    bench = work / "tb.sv"
    bench.write_text("\n".join(bench_lines))
    obj = work / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    warning_waivers = (
        ("-Wno-fatal",)
        if direct
        else ("-Wno-WIDTHTRUNC",)
    )
    build = subprocess.run(
        (
            "verilator",
            "--binary",
            "--timing",
            "-j",
            "8",
            "-CFLAGS",
            "-O0",
            *warning_waivers,
            "--Mdir",
            str(obj),
            "--top-module",
            "tb",
            *rtl_arguments,
            str(bench),
        ),
        cwd=work,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr or build.stdout
    run = subprocess.run(
        (str(obj / "Vtb"),),
        cwd=rtl_working_directory,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout

    pattern = re.compile(
        r"REC (\d+) (\d+) (\d+) (\d+) ([0-9a-fA-F]+) (\d+) ([0-9a-fA-F]+)"
    )
    records = [
        match.groups()
        for line in run.stdout.splitlines()
        if (match := pattern.fullmatch(line)) is not None
    ]
    assert len(records) == cycle_count
    signals = [
        int(signal, 16)
        for _index, _command_ready, _psdu_ready, signal_valid, signal, _valid, _output in records
        if int(signal_valid)
    ]
    outputs = [
        _unpack_output(int(output, 16))
        for _index, _command_ready, _psdu_ready, _signal_valid, _signal, valid, output in records
        if int(valid)
    ]
    return signals, outputs


def test_full_ieee_chain_direct_sv_cycle_trace() -> None:
    if shutil.which("verilator") is None:
        pytest.skip("Verilator is unavailable")
    rate = 1
    payload = b"\x55"
    module = compile_file(SOURCE, top=TOP).ir
    artifact = emit_sv_artifact(module)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / f"{TOP}.sv"
        rtl.write_text(artifact.text)
        # The exact IFFT stages consume content-addressed twiddle ROM images.
        # Emitted RTL and its companion bundle form one BackendArtifact; running
        # only ``artifact.text`` leaves $readmemb without its semantic contents
        # and can turn the difference paths into misleading zeroes.
        published = publish_companion_bundle(artifact.companions, root)
        assert len(published) == 6
        signals, outputs = _run_full_rtl_cycle_trace(
            rtl,
            direct=True,
            work=root / "run",
            rate=rate,
            payload=payload,
        )
    assert signals == [_signal_header(rate, len(payload))]
    assert outputs == _expected_time_packet(rate, payload)
