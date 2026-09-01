"""Scheduled memory and ready/valid share one resolved state transition.

This is deliberately a mixed-feature witness: it protects the backend feature
inventory from accepting RTL that emits the memory while dropping the holding
register/rules (or vice versa).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit as emit_clash
from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.opt.lowering import lower, restore
from zlang.simulate import simulate_cycles
from zlang.toolchain import find_clash_executable, generate_verilog


SOURCE = r"""
module ScheduledMemoryRv {
    clock clk
    reset rst
    in rx : rv<u2>
    out tx : rv<u8>
    in write_enable : bit
    in write_address : u2
    in write_data : u8

    memory table : mem<u8,4> {
        read_latency 1
        collision write_first
    }
    reg pending : bit = 0

    rx.ready = (pending == 0) | tx.ready
    tx.valid = pending
    tx.payload = table.read_data

    rule fetch when rx.transfer {
        table.read(rx.payload)
        pending <- 1
    }
    rule retire when tx.transfer { pending <- 0 }
    rule store when write_enable {
        table.write(write_address, write_data)
    }
    priority fetch > retire
}
"""

BENCH = r"""
module tb;
  logic clk=0, rst=1;
  logic [1:0] rx_payload=0;
  logic rx_valid=0, rx_ready;
  logic [7:0] tx_payload;
  logic tx_valid, tx_ready=0;
  logic write_enable=0;
  logic [1:0] write_address=0;
  logic [7:0] write_data=0;
  ScheduledMemoryRv dut(.*);
  task tick; begin #1 clk=1; #1 clk=0; end endtask
  initial begin
    tick; rst=0;
    write_enable=1; write_address=2; write_data=8'h2a; tick;
    write_enable=0; rx_payload=2; rx_valid=1;
    if (!rx_ready) $fatal(1,"initial request was not ready");
    tick; rx_valid=0;
    if (!tx_valid || tx_payload != 8'h2a) $fatal(1,"read response missing");
    tick;
    if (!tx_valid || tx_payload != 8'h2a) $fatal(1,"stalled response changed");
    tick;
    if (!tx_valid || tx_payload != 8'h2a) $fatal(1,"second stall changed");
    tx_ready=1; rx_payload=2; rx_valid=1; tick;
    if (!tx_valid || tx_payload != 8'h2a) $fatal(1,"replacement request lost");
    rx_valid=0; tick;
    if (tx_valid) $fatal(1,"response did not retire");
    tx_ready=0; rst=1; tick;
    if (tx_valid) $fatal(1,"reset did not clear pending response");
    rst=0; rx_payload=2; rx_valid=1; tick; rx_valid=0;
    if (!tx_valid || tx_payload != 0) $fatal(1,"reset did not clear memory epoch");
    $finish;
  end
endmodule
"""


def _cycles() -> tuple[dict[str, object], ...]:
    def cycle(
        *,
        payload: int = 0,
        valid: int = 0,
        ready: int = 0,
        write_enable: int = 0,
        write_address: int = 0,
        write_data: int = 0,
    ) -> dict[str, object]:
        return {
            "rx": {"payload": payload, "valid": valid},
            "tx": {"ready": ready},
            "write_enable": write_enable,
            "write_address": write_address,
            "write_data": write_data,
        }

    return (
        cycle(),
        cycle(write_enable=1, write_address=2, write_data=0x2A),
        cycle(payload=2, valid=1),
        cycle(),
        cycle(),
        cycle(payload=2, valid=1, ready=1),
        cycle(ready=1),
        cycle(),
        cycle(payload=2, valid=1),
        cycle(),
    )


def test_semantic_canonical_and_simulator_preserve_the_mixed_transition() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    assert module.memories[0].scheduled
    assert tuple(register.name for register in module.registers) == ("pending",)
    assert tuple(rule.name for rule in module.rules) == (
        "fetch",
        "retire",
        "store",
    )
    assert module.resolved_transition is not None
    assert restore(lower(module)) == module

    trace = simulate_cycles(
        module,
        _cycles(),
        reset=(True, False, False, False, False, False, False, True, False, False),
    )
    assert [item["tx"]["valid"] for item in trace] == [
        0, 0, 0, 1, 1, 1, 1, 0, 0, 1
    ]
    assert trace[3]["tx"]["payload"] == 0x2A
    assert trace[4]["tx"]["payload"] == 0x2A
    assert trace[5]["tx"]["transfer"] == 1
    assert trace[5]["rx"]["transfer"] == 1
    assert trace[6]["tx"]["transfer"] == 1
    # Reset starts a new storage epoch, so the next read returns the cleared cell.
    assert trace[9]["tx"]["payload"] == 0


def test_both_artifacts_are_deterministic_and_publish_the_public_ports() -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    assert BackendArtifact.from_json(first.to_json()).to_json() == first.to_json()
    semantic_ids = {binding.semantic_signal_id for binding in first.bindings}
    assert {"port:rx", "port:tx"} <= {
        identity.split(".", 1)[0] for identity in semantic_ids
    } or {
        "port:rx.payload",
        "port:rx.valid",
        "port:rx.ready",
        "port:tx.payload",
        "port:tx.valid",
        "port:tx.ready",
    } <= semantic_ids
    clash_first = emit_clash(module)
    assert emit_clash(module) == clash_first


def _lint(
    files: tuple[Path, ...] | list[Path],
    top: str,
    *,
    clash_generated: bool = False,
) -> None:
    subprocess.run(
        (
            "verilator",
            "--lint-only",
            "--top-module",
            top,
            "-Wall",
            "-Wno-DECLFILENAME",
            "-Wno-UNUSEDSIGNAL",
            "-Wno-UNUSEDPARAM",
            *(('-Wno-PROCASSINIT',) if clash_generated else ()),
            *map(str, files),
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def _simulate_rtl(
    files: tuple[Path, ...] | list[Path],
    root: Path,
    *,
    clash_generated: bool = False,
) -> None:
    bench = root / "tb.sv"
    bench.write_text(BENCH)
    object_dir = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    command = [
        "verilator",
        "--binary",
        "--timing",
        "--top-module",
        "tb",
        "-Wall",
        "-Wno-DECLFILENAME",
        "-Wno-UNUSEDSIGNAL",
        "-Wno-UNUSEDPARAM",
    ]
    if clash_generated:
        command.append("-Wno-PROCASSINIT")
    command.extend((*map(str, files), str(bench), "--Mdir", str(object_dir)))
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        (str(object_dir / "Vtb"),),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator unavailable")
def test_direct_sv_is_strict_lint_clean(tmp_path: Path) -> None:
    artifact = emit_sv_artifact(compile_source(SOURCE, include_clash=False).ir)
    rtl = tmp_path / "ScheduledMemoryRv.sv"
    rtl.write_text(artifact.text)
    _lint([rtl], "ScheduledMemoryRv")
    _simulate_rtl([rtl], tmp_path)


@pytest.mark.skipif(
    shutil.which("verilator") is None or find_clash_executable() is None,
    reason="Clash or Verilator unavailable",
)
def test_real_clash_is_strict_lint_clean(tmp_path: Path) -> None:
    module = compile_source(SOURCE, include_clash=False).ir
    files = generate_verilog(
        emit_clash(module),
        module.name,
        tmp_path / "clash",
        find_clash_executable(),
    )
    _lint(files, module.name, clash_generated=True)
    _simulate_rtl(files, tmp_path, clash_generated=True)
