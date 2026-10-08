from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from zlang.backend.manifest import BackendArtifact
from zlang.backend.systemverilog import emit_artifact as emit_sv_artifact
from zlang.compiler import compile_source
from zlang.toolchain import lint_with_verilator


SOURCE = """
union Message { Idle Data { value : s8 } Error { code : bits<4> } }
module UnionState {
    clock clk
    reset rst
    in load : bit
    in kind : u2
    in data : s8
    in error_code : bits<4>
    out raw : bits<8>
    reg message : Message = Message.Idle
    when load {
        message <- switch kind {
            0 => Message.Idle
            1 => Message.Data { value = data }
            else => Message.Error { code = error_code }
        }
    }
    selected : s8 = match message {
        Message.Idle => 0
        Message.Data { value } => value
        Message.Error { code } => bitcast<s8>(extend<8>(code))
    }
    raw = bitcast<bits<8>>(selected)
}
"""


HIERARCHY_SOURCE = """
union Message { Idle Data { value : u8 } }
module UnionChild {
    in message : Message
    out forwarded : Message
    forwarded = message
}
module UnionHierarchy {
    in data : u8
    out value : u8
    message : Message = Message.Data { value = data }
    inst child : UnionChild { message }
    value = match child.forwarded {
        Message.Idle => 0
        Message.Data { value } => value
    }
}
"""


UNION_OUTPUT_SOURCE = """
union Message { Idle Data { value : u8 } }
module UnionOutput {
    in data : u8
    out current : Message
    current = Message.Data { value = data }
}
"""


UNION_BOUNDARY_SOURCE = """
union Message { Idle Data { value : u8 } }
module UnionBoundary {
    in message : Message
    out forwarded : Message
    forwarded = message
}
"""


HARNESS = r'''
#include "VUnionState.h"
#include "verilated.h"

static void tick(VUnionState &dut) {
  dut.clk = 0; dut.eval();
  dut.clk = 1; dut.eval();
}

int main(int argc, char **argv) {
  Verilated::commandArgs(argc, argv);
  VUnionState dut;
  dut.rst = 1; dut.load = 0; dut.kind = 0; dut.data = 0; dut.error_code = 0; tick(dut);
  if (dut.raw != 0) return 1;
  dut.rst = 0; dut.load = 1; dut.kind = 1; dut.data = 0xf9; tick(dut);
  if (dut.raw != 0xf9) return 2;
  dut.load = 1; dut.kind = 2; dut.error_code = 0xd; tick(dut);
  if (dut.raw != 0x0d) return 3;
  dut.load = 0; dut.data = 0; dut.error_code = 0; tick(dut);
  if (dut.raw != 0x0d) return 4;
  dut.rst = 1; tick(dut);
  if (dut.raw != 0) return 5;
  return 0;
}
'''


def _run(tmp_path: Path, rtl: tuple[Path, ...], suffix: str) -> None:
    harness = tmp_path / f"union_{suffix}.cpp"
    harness.write_text(HARNESS)
    object_dir = tmp_path / f"obj_{suffix}"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    completed = subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build",
            "-Wno-DECLFILENAME", "-Wno-UNUSED", "-Wno-UNDRIVEN",
            "--Mdir", str(object_dir), "--top-module", "UnionState",
            *(str(path) for path in rtl), str(harness),
        ),
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    run = subprocess.run((str(object_dir / "VUnionState"),), capture_output=True, text=True)
    assert run.returncode == 0, run.stderr or run.stdout


def test_direct_sv_union_artifact_is_deterministic_and_bound() -> None:
    module = compile_source(SOURCE, top="UnionState").ir
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)
    assert first.text == second.text
    assert first.artifact_hash == second.artifact_hash
    restored = BackendArtifact.from_json(first.to_json())
    assert restored.bindings == first.bindings
    raw = next(item for item in restored.bindings if item.semantic_signal_id == "port:raw")
    assert raw.canonical_type == "bits<8>"
    union_artifact = emit_sv_artifact(
        compile_source(
            UNION_OUTPUT_SOURCE, top="UnionOutput"
        ).ir
    )
    current = next(
        item for item in union_artifact.bindings
        if item.semantic_signal_id == "port:current"
    )
    assert current.canonical_type.startswith("union<Message:")
    assert "::union::Message>" in current.canonical_type
    assert "logic [9:0] message" in first.text


def test_direct_sv_union_boundary_uses_named_packed_type() -> None:
    module = compile_source(UNION_BOUNDARY_SOURCE, top="UnionBoundary").ir
    first = emit_sv_artifact(module)
    second = emit_sv_artifact(module)

    assert first.text == second.text
    match = re.search(
        r"typedef struct packed \{\n"
        r"  logic tag;\n"
        r"  logic \[7:0\] payload;\n"
        r"\} (zlang_tagged_union_Message_[0-9a-f]{10}_t);",
        first.text,
    )
    assert match is not None
    assert first.text.count("typedef struct packed {") == 1
    type_name = match.group(1)
    assert f"input wire {type_name} message" in first.text
    assert f"output {type_name} forwarded" in first.text
    assert "input wire logic [8:0] message" not in first.text
    assert "output logic [8:0] forwarded" not in first.text


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("iverilog", "verilator", "yosys")),
    reason="Icarus, Verilator, and Yosys are required",
)
def test_direct_sv_union_boundary_lints_synthesizes_and_preserves_bits(
    tmp_path: Path,
) -> None:
    module = compile_source(UNION_BOUNDARY_SOURCE, top="UnionBoundary").ir
    rtl = tmp_path / "UnionBoundary.sv"
    rtl.write_text(emit_sv_artifact(module).text)
    bench = tmp_path / "tb.sv"
    bench.write_text(r"""
module tb;
  logic [8:0] message;
  wire [8:0] forwarded;
  UnionBoundary dut(.message(message), .forwarded(forwarded));
  initial begin
    message = {1'b1, 8'ha5};
    #1;
    if (forwarded !== message) $fatal(1, "tagged-union boundary mismatch");
    $finish;
  end
endmodule
""")
    object_dir = tmp_path / "obj_boundary"
    completed = subprocess.run(
        (
            "verilator", "--binary", "--timing", "-Wno-fatal",
            "--top-module", "tb", "--Mdir", str(object_dir),
            str(rtl), str(bench),
        ),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "CCACHE_DISABLE": "1"},
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    executed = subprocess.run(
        (str(object_dir / "Vtb"),), capture_output=True, text=True, check=False,
    )
    assert executed.returncode == 0, executed.stdout + executed.stderr
    synthesized = subprocess.run(
        (
            "yosys", "-Q", "-p",
            f"read_verilog -sv {rtl}; hierarchy -top UnionBoundary; "
            "proc; opt; check",
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert synthesized.returncode == 0, synthesized.stdout + synthesized.stderr
    compiled = subprocess.run(
        (
            "iverilog", "-g2012", "-s", "UnionBoundary",
            "-o", str(tmp_path / "union_boundary.vvp"), str(rtl),
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr




@pytest.mark.skipif(shutil.which("verilator") is None, reason="Verilator required")
def test_direct_sv_union_state_lints_and_runs(tmp_path: Path) -> None:
    module = compile_source(SOURCE, top="UnionState").ir
    rtl = tmp_path / "UnionState.sv"
    rtl.write_text(emit_sv_artifact(module).text)
    lint_with_verilator((rtl,), "UnionState")
    _run(tmp_path, (rtl,), "sv")
