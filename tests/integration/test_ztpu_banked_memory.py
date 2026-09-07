from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.clash import emit_artifact as emit_clash_artifact
from zlang.backend.manifest import BackendArtifact
from zlang.backend.naming import module_rtl_names
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.formal import build_recursive_formal_design
from zlang.opt import lower, restore
from zlang.semantic import SemanticError
from zlang.simulate import simulate_cycles
from zlang.toolchain import find_clash_executable, generate_verilog, lint_with_verilator


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "examples" / "ztpu_banked_memory.zhl").read_text()
TOP = "ZtpuBankedMemory"
CLASH = find_clash_executable()
VERILATOR = shutil.which("verilator")


def _cycle(
    read0: int,
    read1: int,
    *,
    write: int = 0,
    address: int = 0,
    data: int = 0,
    mask: int = 0,
) -> dict[str, int]:
    return {
        "read0_address": read0,
        "read1_address": read1,
        "write_enable": write,
        "write_address": address,
        "write_data": data,
        "write_mask": mask,
    }


CYCLES = (
    _cycle(0, 0),
    _cycle(0, 0, write=1, address=0, data=0x11223344, mask=0xF),
    _cycle(0, 0),
    _cycle(5, 0, write=1, address=5, data=0xAABBCCDD, mask=0x5),
    _cycle(5, 5),
    _cycle(5, 5, write=1, address=5, data=0xAABBCCDD, mask=0xA),
    _cycle(5, 5),
    _cycle(2, 5, write=1, address=2, data=0xDEADBEEF, mask=0xF),
    _cycle(0, 2),
    # Reset suppresses this full write and preserves all eight physical copies.
    _cycle(0, 2, write=1, address=0, data=0xFFFFFFFF, mask=0xF),
    _cycle(0, 2),
    # Four individual byte lanes, followed by an explicit zero-mask no-op.
    _cycle(0x13, 0x13, write=1, address=0x13, data=0x00000011, mask=0x1),
    _cycle(0x13, 0x13, write=1, address=0x13, data=0x00002200, mask=0x2),
    _cycle(0x13, 0x13, write=1, address=0x13, data=0x00330000, mask=0x4),
    _cycle(0x13, 0x13, write=1, address=0x13, data=0x44000000, mask=0x8),
    _cycle(0x13, 0x13, write=1, address=0x13, data=0xFFFFFFFF, mask=0x0),
    _cycle(0x13, 0x13),
    # Addresses 0 and 4 select the same bank but distinct rows.
    _cycle(0, 4, write=1, address=4, data=0x55667788, mask=0xF),
    _cycle(0, 4),
)
RESETS = (True, False, False, False, False, False, False, False, False,
          True, False, False, False, False, False, False, False, False, False)


def _merge(old: int, new: int, mask: int) -> int:
    result = old
    for lane in range(4):
        if (mask >> lane) & 1:
            lane_mask = 0xFF << (lane * 8)
            result = (result & ~lane_mask) | (new & lane_mask)
    return result & 0xFFFFFFFF


def _oracle() -> tuple[tuple[int, int], ...]:
    cells = [0] * 256
    trace: list[tuple[int, int]] = []
    for cycle, reset in zip(CYCLES, RESETS, strict=True):
        trace.append(
            (cells[cycle["read0_address"]], cells[cycle["read1_address"]])
        )
        if not reset and cycle["write_enable"]:
            address = cycle["write_address"]
            cells[address] = _merge(
                cells[address], cycle["write_data"], cycle["write_mask"]
            )
    return tuple(trace)


EXPECTED = _oracle()


def test_ztpu_banked_memory_elaboration_identity_and_canonical_round_trip() -> None:
    module = compile_source(SOURCE, top=TOP, include_clash=False).ir
    assert restore(lower(module)) == module

    banked, = module.children
    assert banked.name.startswith("ReplicatedBanked2R1W")
    assert [item.instance.name for item in banked.elaborated_instances] == [
        *(f"read0[{index}]" for index in range(4)),
        *(f"read1[{index}]" for index in range(4)),
    ]
    assert len({
        item.specialization_identity for item in banked.elaborated_instances
    }) == 1
    assert all(len(child.memories) == 1 for child in banked.children)
    assert all(child.memories[0].depth == 64 for child in banked.children)

    recursive = build_recursive_formal_design(module)
    sv_first = emit_sv_artifact(module, recursive_design=recursive)
    sv_second = emit_sv_artifact(module, recursive_design=recursive)
    clash = emit_clash_artifact(module, recursive_design=recursive)
    assert sv_first.text == sv_second.text
    assert sv_first.artifact_hash == sv_second.artifact_hash
    assert BackendArtifact.from_json(sv_first.to_json()).to_json() == sv_first.to_json()
    paths = tuple(item.physical_instance_path for item in sv_first.instances)
    assert paths == tuple(item.physical_instance_path for item in clash.instances)
    assert paths == (
        (TOP,),
        (TOP, "storage"),
        *((TOP, "storage", f"read0[{index}]") for index in range(4)),
        *((TOP, "storage", f"read1[{index}]") for index in range(4)),
    )

    replica = re.search(
        r"module (ZtpuMemoryReplica_s[0-9a-f]+) \(", sv_first.text
    )
    assert replica is not None
    bank_names = module_rtl_names(banked)
    for item in banked.elaborated_instances:
        assert sv_first.text.count(
            f"{replica.group(1)} {bank_names.instance(item.instance.name)} ("
        ) == 1
    assert "logic [31:0] zlang_table_cells [0:63];" in sv_first.text
    assert "for (zlang_memory_reset_index" in sv_first.text
    assert "< 64" in sv_first.text

    with pytest.raises(SemanticError, match="parameter constraint is not satisfied"):
        compile_source(
            SOURCE.replace("BANKS=4,ROWS=64,BW=2", "BANKS=4,ROWS=64,BW=1"),
            top=TOP,
            include_clash=False,
        )


def test_ztpu_banked_memory_simulator_matches_independent_oracle_and_mutation() -> None:
    module = compile_source(SOURCE, top=TOP, include_clash=False).ir
    trace = simulate_cycles(module, CYCLES, reset=RESETS)
    observed = tuple(
        (int(item["read0_data"]), int(item["read1_data"])) for item in trace
    )
    assert observed == EXPECTED
    assert observed[3] == (0, 0x11223344)
    assert observed[4] == (0x00BB00DD, 0x00BB00DD)
    assert observed[6] == (0xAABBCCDD, 0xAABBCCDD)
    assert observed[8] == (0x11223344, 0xDEADBEEF)
    assert observed[9] == observed[10] == (0x11223344, 0xDEADBEEF)
    assert observed[-3] == (0x44332211, 0x44332211)
    assert observed[-1] == (0x11223344, 0x55667788)

    mutated_source = SOURCE.replace(
        "read1[i].write_enable = write_enable & (write_bank == i)",
        "read1[i].write_enable = 0",
    )
    assert mutated_source != SOURCE
    mutated = compile_source(mutated_source, top=TOP, include_clash=False).ir
    mutation_trace = simulate_cycles(
        mutated,
        (
            _cycle(0, 0),
            _cycle(0, 0, write=1, address=0, data=0x89ABCDEF, mask=0xF),
            _cycle(0, 0),
        ),
        reset=(True, False, False),
    )
    assert mutation_trace[-1] == {
        "read0_data": 0x89ABCDEF,
        "read1_data": 0,
    }


def _harness() -> str:
    calls = []
    for index, (cycle, reset, expected) in enumerate(
        zip(CYCLES, RESETS, EXPECTED, strict=True), start=1
    ):
        calls.append(
            "  if (cycle(d, "
            f"{int(reset)}, {cycle['read0_address']}, {cycle['read1_address']}, "
            f"{cycle['write_enable']}, {cycle['write_address']}, "
            f"0x{cycle['write_data']:08x}u, 0x{cycle['write_mask']:x}u, "
            f"0x{expected[0]:08x}u, 0x{expected[1]:08x}u)) return {index};"
        )
    return r'''#include "VZtpuBankedMemory.h"
#include <cstdint>

static bool cycle(
    VZtpuBankedMemory &d,
    unsigned reset,
    unsigned read0,
    unsigned read1,
    unsigned write_enable,
    unsigned write_address,
    std::uint32_t write_data,
    unsigned write_mask,
    std::uint32_t expected0,
    std::uint32_t expected1) {
  d.clk = 0;
  d.rst = reset;
  d.read0_address = read0;
  d.read1_address = read1;
  d.write_enable = write_enable;
  d.write_address = write_address;
  d.write_data = write_data;
  d.write_mask = write_mask;
  d.eval();
  if (d.read0_data != expected0 || d.read1_data != expected1) return true;
  d.clk = 1;
  d.eval();
  d.clk = 0;
  d.eval();
  return false;
}

int main() {
  VZtpuBankedMemory d;
''' + "\n".join(calls) + r'''
  return 0;
}
'''


def _build_and_run(files: tuple[Path, ...], tmp_path: Path, label: str) -> None:
    harness = tmp_path / f"{label}_harness.cpp"
    harness.write_text(_harness())
    obj = tmp_path / f"{label}_obj"
    environment = dict(os.environ)
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "--top-module", TOP, "--Mdir", str(obj), "-o", "banked_sim",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            *(str(path) for path in files), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run(
        (str(obj / "banked_sim"),),
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr or run.stdout


@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_ztpu_banked_memory_direct_sv_is_strict_and_cycle_identical(
    tmp_path: Path,
) -> None:
    module = compile_source(SOURCE, top=TOP, include_clash=False).ir
    artifact = emit_sv_artifact(module)
    rtl = tmp_path / f"{TOP}.sv"
    rtl.write_text(artifact.text)
    lint_with_verilator((rtl,), TOP)
    _build_and_run((rtl,), tmp_path, "direct")


@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="real Clash 1.11 and Verilator are required",
)
def test_ztpu_banked_memory_real_clash_is_strict_and_cycle_identical(
    tmp_path: Path,
) -> None:
    compilation = compile_source(SOURCE, top=TOP)
    # The reusable sequential child returns both read ports as one exact
    # product.  This is the generic ABI regression that the ZTPU composition
    # exposed; neither result captures a parent signal.
    assert (
        "Signal ZLangSystem (Unsigned 32), Signal ZLangSystem (Unsigned 32)"
        in compilation.clash
    )
    assert "replicatedBanked2R1W" in compilation.clash
    rtl = tuple(
        generate_verilog(
            compilation.clash,
            TOP,
            tmp_path / "clash_rtl",
            CLASH,
        )
    )
    lint_with_verilator(rtl, TOP)
    _build_and_run(rtl, tmp_path, "clash")
