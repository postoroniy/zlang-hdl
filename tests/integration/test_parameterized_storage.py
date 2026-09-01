import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest

from zlang.backend.systemverilog import emit_experimental
from zlang.compiler import compile_source
from zlang.toolchain import find_clash_executable, generate_verilog


VERILATOR = shutil.which("verilator")
CLASH = find_clash_executable()


def fifo_source(depth: int) -> str:
    return f"""
module ParamFifo{depth}<DEPTH={depth}> {{
  clock clk
  reset rst
  in rx:rv<u8>
  out tx:rv<u8>
  fifo q:fifo<u8,DEPTH>
  q.data=rx.payload
  q.push=rx.transfer
  q.pop=tx.transfer
  rx.ready=q.ready
  tx.payload=q.front
  tx.valid=q.valid
}}
"""


def harness(module_name: str, depth: int) -> str:
    pushes = "".join(
        f"d.rx_payload={index + 1}; tick(d); "
        f"if (!d.tx_valid || d.tx_payload != 1) return {10 + index}; "
        for index in range(depth)
    )
    return (
        f'#include "V{module_name}.h"\n'
        f"static void tick(V{module_name}& d) {{ d.clk=0; d.eval(); d.clk=1; "
        "d.eval(); d.clk=0; d.eval(); }\n"
        f"int main() {{ V{module_name} d; d.rx_payload=0; d.rx_valid=0; "
        "d.tx_ready=0; d.rst=1; tick(d); d.rst=0; d.rx_valid=1; "
        f"{pushes} d.eval(); if (d.rx_ready) return 40; d.rx_valid=0; "
        "d.tx_ready=1; "
        f"for (int expected=1; expected<={depth}; ++expected) {{ d.eval(); "
        "if (!d.tx_valid || d.tx_payload != expected) return 50+expected; tick(d); } "
        "d.eval(); return d.tx_valid ? 90 : 0; }\n"
    )


def run_verilator(files: list[Path], root: Path, module_name: str, depth: int) -> None:
    test = root / "fifo_test.cpp"
    test.write_text(harness(module_name, depth))
    obj = root / "obj"
    environment = os.environ.copy()
    environment["CCACHE_DISABLE"] = "1"
    subprocess.run(
        (
            "verilator", "--cc", "--exe", "--build", "--top-module",
            module_name, "--Mdir", str(obj), "-o", "fifo_sim",
            *map(str, files), str(test),
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    subprocess.run(
        (str(obj / "fifo_sim"),), check=True, capture_output=True, text=True
    )


@pytest.mark.parametrize("depth", (2, 8))
@pytest.mark.skipif(VERILATOR is None, reason="Verilator unavailable")
def test_parameterized_fifo_direct_sv_is_concrete_and_simulates(depth: int) -> None:
    compilation = compile_source(fifo_source(depth))
    text = emit_experimental(compilation.ir)
    assert "DEPTH" not in text
    assert f"[0:{depth - 1}]" in text
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rtl = root / "fifo.sv"
        rtl.write_text(text)
        run_verilator([rtl], root, compilation.ir.name, depth)


@pytest.mark.parametrize("depth", (2, 8))
@pytest.mark.skipif(
    CLASH is None or VERILATOR is None,
    reason="Clash or Verilator unavailable",
)
def test_parameterized_fifo_clash_is_concrete_and_simulates(depth: int) -> None:
    compilation = compile_source(fifo_source(depth))
    assert "DEPTH" not in compilation.clash
    assert f"Vec {depth} (Unsigned 8)" in compilation.clash
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        files = list(generate_verilog(
            compilation.clash, compilation.ir.name, root / "rtl", CLASH
        ))
        run_verilator(files, root, compilation.ir.name, depth)
